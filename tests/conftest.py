"""공용 fixture — 녹화 fixture 의 로더(응답 · 요청 키 · at) · replay transport(요청 정합 검사) · 가짜 transport(명시 라우트) · 격리된 env ·
물리 단어 목록 · 스냅샷 비교. 규약은 .claude/rules/python-tests.md — tests/infra/test_conventions.py 가 지킨다."""
from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Protocol, Sequence, TypeVar, cast

import pytest

from obs.core import JSON, Client, Config, Evidence, TransportError, TransportTimeout, Window, load_config
from tests.record import FIXTURES, ROOT, read_fixture, response_of, strip_physical
from tests.replay import Call, FixtureSpec, Loaded, ReplayTransport, load_call, load_fixture_calls

__all__ = ["FIXTURES", "ROOT", "Call", "Loaded", "load_call", "_no_physical", "_vector", "_stat"]   # 재수출 · 공용 헬퍼 — 테스트가 여기서 가져온다


# ── 물리 비노출 — 단어 목록은 여기 한 곳 ─────────────────────────────────────────

PHYSICAL: tuple[str, ...] = (
    # test_core · test_cli · test_metrics 에 각각 있던 목록의 합집합. 낱말 `prometheus` · `jyo-liar` 는 녹화 데이터(서비스 이름
    # monitoring-kube-prometheus-* · span 의 net.host.name careerhub.jyo-liar.com)에도 나오므로 구체형(uid/… · …com/api)으로 접었다.
    "obs.mg", "jyo-liar.com/api", "datasources/proxy", "uid/prometheus", "loki-careerhub", "tempo-careerhub",
)
"""agent 출력 · 오류 문구 · fixture 파일 어디에도 없어야 하는 물리 문자열(호스트 · 프록시 접두 · 데이터소스 uid)."""


def _no_physical(text: str) -> None:
    """text 에 PHYSICAL 의 단어가 하나도 없다."""
    for word in PHYSICAL:
        assert word not in text, "물리 정보가 실렸다: %s" % word


# ── 손 입력 — Prometheus 즉시 조회 응답 모양 ─────────────────────────────────────

AT: float = 100000.0
"""손 입력 테스트의 now — fixture replay 는 녹화 at(t.at)을 쓴다."""


def _vector(*items: tuple[dict[str, str], str]) -> dict[str, JSON]:
    """Prometheus 즉시 조회 응답을 손으로 — (레이블, 값) 목록. 비면 빈 vector."""
    result: list[JSON] = [{"metric": dict(labels), "value": [AT, v]} for labels, v in items]
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _stat(pod: str, stat: str, v: str) -> tuple[dict[str, str], str]:
    """환원 응답의 한 항목 — pod · stat 이름표와 값."""
    return {"pod": pod, "stat": stat}, v


# ── 녹화 fixture ─────────────────────────────────────────────────────────────────

UNCHECKED_SHARED_EMPTY: str = "공유 fixture — 다섯 테스트가 다른 질의로 같은 빈 응답을 쓴다(녹화 요청은 summarize 의 것)"
"""promql-instant-empty.json 을 다른 질의로 쓰는 테스트가 replay 검사를 끄는 이유 — 한 곳에 두고 가져다 쓴다."""


def load_response(name: str, path: str | None = None) -> JSON:
    """녹화 fixture(`fixtures/<name>` — tests/record.py 형식)에서 **응답 본문만** 꺼낸다. 요청 기록은 tests/record.py 의 read_fixture 로.
    한 파일에 호출이 여럿이면(traces: api/search → api/traces/<id>) `path`(네이티브 경로 조각)로 하나를 고른다."""
    return response_of(read_fixture(FIXTURES / name), path, name)


# ── FakeTransport — 명시 라우트 ──────────────────────────────────────────────────

RouteKey = tuple[str, str]
"""FakeTransport 라우트 키 — (논리 백엔드, 네이티브 경로). 나간 URL 에서 tests/record.py 의 strip_physical 이 뽑는 것과 **정확히** 같다.
query string 은 키에 들지 않는다 — 질의별로 다른 응답이 필요하면 그것은 replay 의 일이다."""


@dataclass
class FakeTransport:
    """(url, headers, timeout) → (status, body). `routes[(backend, path)]` 정확 일치로 응답을 고르고 부른 기록을 남긴다.
    등록되지 않은 요청은 404 가 아니라 AssertionError — 문구에 등록된 키를 나열한다.
    실환경이 만들 수 없는 조건(연결 실패 · 시간 초과 · 404 · 손으로 만든 응답 · k 초과 응답)용 — fixture replay 는 `replay` 로."""
    config: Config
    routes: dict[RouteKey, tuple[int, object]] = field(default_factory=dict[RouteKey, tuple[int, object]])
    calls: list[Call] = field(default_factory=list[Call])
    fail: str | None = None   # TransportError 문구 — 있으면 항상 실패(연결 실패)
    hang: bool = False        # True 면 읽기 시간 초과(TransportTimeout) — 닿았는데 답이 없는 모양

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(Call(url=url, headers=dict(headers), timeout=timeout))
        if self.fail is not None:
            raise TransportError(self.fail)
        if self.hang:
            raise TransportTimeout("The read operation timed out")
        rq = strip_physical(url, self.config, None)
        key: RouteKey = (rq.backend, rq.path)
        if key not in self.routes:
            raise AssertionError("FakeTransport 에 등록되지 않은 요청 %r — 등록된 키: %s" % (key, sorted(self.routes)))
        status, body = self.routes[key]
        raw = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
        return status, bytes(raw)


# ── 격리된 env · Client 공장 ─────────────────────────────────────────────────────

@pytest.fixture
def base_env(tmp_path: Path) -> dict[str, str]:
    """설정 파일 후보가 tmp 를 보게 하는 격리 env. 토큰은 가짜."""
    return {
        "OBS_GRAFANA_TOKEN": "test-token-not-real",
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "plugin-data"),
        "CLAUDE_PROJECT_DIR": str(tmp_path / "project"),
        "HOME": str(tmp_path / "home"),
    }


@pytest.fixture
def config(base_env: dict[str, str], tmp_path: Path) -> Config:
    return load_config(base_env, home=tmp_path / "home")


class ClientFactory(Protocol):
    def __call__(self, routes: dict[RouteKey, tuple[int, object]] | None = None, fail: str | None = None, hang: bool = False) -> tuple[Client, FakeTransport]: ...


@pytest.fixture
def make_client(config: Config) -> ClientFactory:
    def _make(routes: dict[RouteKey, tuple[int, object]] | None = None, fail: str | None = None, hang: bool = False) -> tuple[Client, FakeTransport]:
        t = FakeTransport(config=config, routes=routes or {}, fail=fail, hang=hang)
        return Client(config=config, token="test-token-not-real", transport=t), t
    return _make


class ReplayFactory(Protocol):
    def __call__(self, *specs: FixtureSpec, unchecked: str | None = None) -> tuple[Client, ReplayTransport]: ...


@pytest.fixture
def replay(config: Config) -> ReplayFactory:
    """fixture replay — 녹화 호출을 순서대로 돌려주고 나간 요청을 녹화 요청과 비교하는 Client. `t.at` 이 녹화 시점이다:
    `client, t = replay("promql-instant-up.json"); promql_run(client, …, at=t.at)`.
    spec 은 파일 이름(호출 전부 — traces 는 search → traces/<id> 둘) 또는 (파일 이름, 경로 조각)(그 호출 하나).
    `unchecked=<이유>` 만이 검사를 끈다 — 이유가 비어 있으면 ReplayTransport 가 거부한다. 공유 fixture 의 이유는 UNCHECKED_SHARED_EMPTY."""
    def _make(*specs: FixtureSpec, unchecked: str | None = None) -> tuple[Client, ReplayTransport]:
        expected: list[Loaded] = []
        for spec in specs:
            expected += load_fixture_calls(spec) if isinstance(spec, str) else [load_call(spec)]
        t = ReplayTransport(expected=expected, config=config, unchecked=unchecked)
        return Client(config=config, token="test-token-not-real", transport=t), t
    return _make


# ── Evidence 의 창 ───────────────────────────────────────────────────────────────

V = TypeVar("V")


def window_of(ev: Evidence[V]) -> Window:
    """렌더링 함수에 넘길 Evidence 의 창 — `ev.window or Window(0, 0)` 대신. 창 없는 Evidence 는 테스트가 만들지 않는다."""
    assert ev.window is not None, "Evidence 에 창이 없다"
    return ev.window


# ── 스냅샷 — format 티어의 비교 장치 ─────────────────────────────────────────────

SNAPSHOTS: Path = ROOT / "tests" / "format" / "snapshots"
UPDATE_ENV: str = "OBS_TEST_UPDATE"


def assert_snapshot(request: pytest.FixtureRequest, lines: Sequence[str]) -> None:
    """`lines` 를 tests/format/snapshots/<테스트 이름>.txt 와 비교한다. 파일 이름은 request.node.name(파라미터 포함) 그대로.
    파일이 없거나 OBS_TEST_UPDATE=1 이면 쓴다(그 실행은 통과 — 새 파일은 커밋한다). 그 외에는 다르면 unified diff 를 실패 문구로 낸다."""
    node = cast(pytest.Item, getattr(request, "node"))   # FixtureRequest.node 는 pytest stub 에 타입이 없다
    path = SNAPSHOTS / (node.name + ".txt")
    actual = "\n".join(lines) + "\n"
    if not path.is_file() or os.environ.get(UPDATE_ENV):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(actual, encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8")
    if expected == actual:
        return
    shown = path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path)
    diff = "".join(difflib.unified_diff(expected.splitlines(True), actual.splitlines(True), fromfile=shown, tofile="이번 실행"))
    raise AssertionError("스냅샷과 다르다 — 의도한 변경이면 %s=1 로 갱신한다\n%s" % (UPDATE_ENV, diff))
