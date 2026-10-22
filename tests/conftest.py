"""공용 fixture — 녹화 fixture 의 로더(응답 · 요청 키 · at) · replay transport(요청 정합 검사) · 가짜 transport(명시 라우트) · 격리된 env ·
물리 단어 목록 · 스냅샷 비교. 규약은 .claude/rules/python-tests.md — tests/infra/test_conventions.py 가 지킨다."""
from __future__ import annotations

import difflib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence, cast

import pytest

from obs.core import JSON, Client, Config, TransportError, TransportTimeout, load_config
from obs.logs import LogEntry
from tests.record import FIXTURES, ROOT, read_fixture, response_of, strip_physical
from tests.replay import Call, FixtureSpec, Loaded, ReplayTransport, load_call, load_fixture_calls

__all__ = ["FIXTURES", "ROOT", "Call", "Loaded", "load_call", "_no_physical", "_vector", "_stat", "_red_body", "AT", "MINUTE", "HOUR", "DAY", "WEEK"]   # 재수출 · 공용 헬퍼 — 테스트가 여기서 가져온다


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
"""손 입력 테스트의 now — 창 · 기준선의 기대값은 전부 이 값 기준의 상대로 쓴다. fixture replay 는 녹화 at(t.at)을 쓴다."""
MINUTE: float = 60.0
HOUR: float = 60 * MINUTE
DAY: float = 24 * HOUR
WEEK: float = 7 * DAY


def _vector(*items: tuple[dict[str, str], str]) -> dict[str, JSON]:
    """Prometheus 즉시 조회 응답을 손으로 — (레이블, 값) 목록. 비면 빈 vector."""
    result: list[JSON] = [{"metric": dict(labels), "value": [AT, v]} for labels, v in items]
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _stat(pod: str, stat: str, v: str) -> tuple[dict[str, str], str]:
    """환원 응답의 한 항목 — pod · stat 이름표와 값."""
    return {"pod": pod, "stat": stat}, v


RED_SERVICE: str = "svc"
"""red 손 입력의 서비스 이름 — 응답의 service_name 이름표."""
RED_FULL: dict[str, str] = {"calls_now": "200", "calls_base": "100", "errors_now": "10", "errors_base": "5", "p95_now": "0.4", "p95_base": "0.2"}
"""지금 · 기준선 모두 호출 · 오류 · 분위수가 있는 red 응답 — R ×2 · E ×1 · D ×2."""
RED_BASE_ONLY: dict[str, str] = {k: v for k, v in RED_FULL.items() if k.endswith("_base")}
RED_NOW_ONLY: dict[str, str] = {k: v for k, v in RED_FULL.items() if k.endswith("_now")}


def _red_body(**stats: str) -> dict[str, JSON]:
    """red 질의의 응답을 손으로 — stat 이름(RED_STATS) → 값 문자열. 빠진 이름은 그 시계열의 부재, "NaN" 은 값 없는 시계열."""
    return _vector(*(({"service_name": RED_SERVICE, "stat": stat}, v) for stat, v in stats.items()))


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


# ── FakeLoki — 로그 API 를 Loki 처럼 흉내 내는 대역 ───────────────────────────────────

@dataclass
class FakeLoki:
    """`(url, headers, timeout) → (status, body)`. 창 안의 로그 줄(`entries`)과 전체 건수(`count`)를 받아, 로그 API(`loki/api/v1/query_range`)에는
    요청의 `start` · `end` · `direction` · `limit` 을 Loki 규칙대로 적용한 스트림 응답을, count API(`loki/api/v1/query`)에는 `count` 의 vector 를 낸다.
    정적 본문의 FakeTransport 로는 "창 안의 세상" 과 "상한에 잘린 응답" 을 나눠 적을 수 없는 자리(around)용 — Given 은 세상만 말하고 잘림은 대역이 만든다.
    Loki 규칙: `[start, end)` 안의 줄만 · backward 는 end 에 가까운 순 · forward 는 start 에 가까운 순 · `limit` 은 스트림 합산 · 0 건이면 count 는 빈 vector."""
    config: Config
    entries: list[LogEntry] = field(default_factory=list[LogEntry])
    count: int = 0
    calls: list[Call] = field(default_factory=list[Call])

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(Call(url=url, headers=dict(headers), timeout=timeout))
        rq = strip_physical(url, self.config, None)
        if (rq.backend, rq.path) == ("logs", "loki/api/v1/query"):
            result: list[JSON] = [] if self.count == 0 else [{"metric": {}, "value": [AT, str(self.count)]}]
            return 200, json.dumps({"status": "success", "data": {"resultType": "vector", "result": result}}).encode("utf-8")
        if (rq.backend, rq.path) != ("logs", "loki/api/v1/query_range"):
            raise AssertionError("FakeLoki 는 loki/api/v1/query · query_range 만 안다 — 받은 요청 %r" % ((rq.backend, rq.path),))
        start, end = int(str(rq.params.get("start", "0"))), int(str(rq.params.get("end", "0")))
        backward = str(rq.params.get("direction", "backward")) == "backward"
        limit = int(str(rq.params.get("limit", "100")))
        inside = sorted((e for e in self.entries if start <= e.ts_ns < end), key=lambda e: e.ts_ns, reverse=backward)[:limit]
        streams: list[JSON] = []
        for e in inside:
            for st in streams:
                st_obj = st if isinstance(st, dict) else {}
                if st_obj.get("stream") == e.labels:
                    values = st_obj.get("values")
                    if isinstance(values, list):
                        values.append([str(e.ts_ns), e.line])
                    break
            else:
                streams.append({"stream": dict(e.labels), "values": [[str(e.ts_ns), e.line]]})
        return 200, json.dumps({"status": "success", "data": {"resultType": "streams", "result": streams}}).encode("utf-8")


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


MainFn = Callable[[Sequence[str], Mapping[str, str], float, Callable[[], Client]], None]
"""여섯 *_main(promql · metrics · logql · logs · traceql · traces)의 꼴 — argv · env · at · client(Client 를 만드는 함수)."""


def no_client() -> Client:
    """--help · 인자 오류의 client 대역 — 불리면 그 자체가 실패다(parse 뒤에만 client() 를 부른다는 약속)."""
    raise AssertionError("Client 를 만들면 안 된다 — --help · 인자 오류에서는 client() 를 부르지 않는다")


class MainRunner(Protocol):
    def __call__(self, main: MainFn, argv: Sequence[str], client: Client | None = None, *,
                 at: float = AT, env: Mapping[str, str] | None = None) -> None: ...


@pytest.fixture
def run_main(base_env: dict[str, str]) -> MainRunner:
    """*_main 을 인자 넷으로 부른다 — env 는 base_env(격리), at 은 AT, client 는 준 Client 를 돌려주는 함수(없으면 no_client)."""
    def _run(main: MainFn, argv: Sequence[str], client: Client | None = None, *,
             at: float = AT, env: Mapping[str, str] | None = None) -> None:
        def factory() -> Client:
            if client is None:
                return no_client()
            return client
        main(argv, base_env if env is None else env, at, factory)
    return _run


class LokiFactory(Protocol):
    def __call__(self, entries: Sequence[LogEntry] = (), count: int = 0) -> tuple[Client, FakeLoki]: ...


@pytest.fixture
def make_loki(config: Config) -> LokiFactory:
    """Loki 처럼 창 · 방향 · limit 을 적용하는 로그 API 대역의 Client — around 의 손 입력용."""
    def _make(entries: Sequence[LogEntry] = (), count: int = 0) -> tuple[Client, FakeLoki]:
        t = FakeLoki(config=config, entries=list(entries), count=count)
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
