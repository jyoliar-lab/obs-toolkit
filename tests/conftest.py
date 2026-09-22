"""공용 fixture — 녹화 fixture 의 로더(응답 · 요청 키 · at) · replay transport(요청 정합 검사) · 가짜 transport · 격리된 env."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping, ParamSpec, Protocol, cast

import pytest

from obs.adapters import cli
from obs.core import JSON, Client, Config, DoctorReport, TransportError, TransportTimeout, load_config
from tests.record import FIXTURES, ROOT, read_fixture, response_of
from tests.replay import Call, FixtureSpec, Loaded, ReplayTransport, load_call, load_fixture_calls

__all__ = ["FIXTURES", "ROOT", "Call", "Loaded", "load_call"]   # 재수출 — 테스트가 여기서 가져온다


def load_response(name: str, path: str | None = None) -> JSON:
    """녹화 fixture(`fixtures/<name>` — tests/record.py 형식)에서 **응답 본문만** 꺼낸다. 요청 기록은 tests/record.py 의 read_fixture 로.
    한 파일에 호출이 여럿이면(traces: api/search → api/traces/<id>) `path`(네이티브 경로 조각)로 하나를 고른다."""
    return response_of(read_fixture(FIXTURES / name), path, name)


@dataclass
class FakeTransport:
    """(url, headers, timeout) → (status, body). 경로 조각으로 응답을 고른다. 부른 기록을 남긴다.
    실환경이 만들 수 없는 조건(연결 실패 · 시간 초과 · 404 · 손으로 만든 응답 · k 초과 응답)용 — fixture replay 는 `replay` 로."""
    routes: dict[str, tuple[int, object]] = field(default_factory=dict[str, tuple[int, object]])
    calls: list[Call] = field(default_factory=list[Call])
    fail: str | None = None   # TransportError 문구 — 있으면 항상 실패(연결 실패)
    hang: bool = False        # True 면 읽기 시간 초과(TransportTimeout) — 닿았는데 답이 없는 모양

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(Call(url=url, headers=dict(headers), timeout=timeout))
        if self.fail is not None:
            raise TransportError(self.fail)
        if self.hang:
            raise TransportTimeout("The read operation timed out")
        for frag, (status, body) in self.routes.items():
            if frag in url:
                raw = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode("utf-8")
                return status, bytes(raw)
        return 404, b'{"message":"no route"}'


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
    def __call__(self, routes: dict[str, tuple[int, object]] | None = None, fail: str | None = None, hang: bool = False) -> tuple[Client, FakeTransport]: ...


@pytest.fixture
def make_client(config: Config) -> ClientFactory:
    def _make(routes: dict[str, tuple[int, object]] | None = None, fail: str | None = None, hang: bool = False) -> tuple[Client, FakeTransport]:
        t = FakeTransport(routes=routes or {}, fail=fail, hang=hang)
        return Client(config=config, token="test-token-not-real", transport=t), t
    return _make


class ReplayFactory(Protocol):
    def __call__(self, *specs: FixtureSpec, unchecked: str | None = None) -> tuple[Client, ReplayTransport]: ...


@pytest.fixture
def replay(config: Config) -> ReplayFactory:
    """fixture replay — 녹화 호출을 순서대로 돌려주고 나간 요청을 녹화 요청과 비교하는 Client. `t.at` 이 녹화 시점이다:
    `client, t = replay("promql-instant-up.json"); promql_run(client, …, at=t.at)`.
    spec 은 파일 이름(호출 전부 — traces 는 search → traces/<id> 둘) 또는 (파일 이름, 경로 조각)(그 호출 하나).
    `unchecked=<이유>` 만이 검사를 끈다 — 공유 fixture(promql-instant-empty.json)처럼 녹화 요청이 이 테스트의 질의와 다를 때."""
    def _make(*specs: FixtureSpec, unchecked: str | None = None) -> tuple[Client, ReplayTransport]:
        expected: list[Loaded] = []
        for spec in specs:
            expected += load_fixture_calls(spec) if isinstance(spec, str) else [load_call(spec)]
        t = ReplayTransport(expected=expected, config=config, unchecked=unchecked)
        return Client(config=config, token="test-token-not-real", transport=t), t
    return _make


# ── 출력 덤프 — 테스트가 받은 Evidence 를 사람이 파일로 본다 ─────────────────────

P = ParamSpec("P")


@dataclass(frozen=True)
class Block:
    """덤프 파일의 한 블록 — 제목 줄과 본문 줄."""
    title: str
    lines: list[str]


def _recording(fn: Callable[P, cli.RunResult], sink: list[Block]) -> Callable[P, cli.RunResult]:
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> cli.RunResult:
        result = fn(*args, **kwargs)
        e = result[0]
        sink.append(Block("# %s  %s  %s" % (e.backend, e.query, e.window.label() if e.window else "-"), e.value.lines))
        return result
    return wrapped


def _recording_doctor(fn: Callable[[DoctorReport], list[str]], sink: list[Block]) -> Callable[[DoctorReport], list[str]]:
    def wrapped(rep: DoctorReport) -> list[str]:
        lines = fn(rep)
        sink.append(Block("# doctor", lines))
        return lines
    return wrapped


@pytest.fixture(autouse=True)
def dump_evidence(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """OBS_TEST_DUMP=1 이면 각 테스트가 promql_run·logql_run·traceql_run·metrics_run 에서 받은 Evidence.value.lines 와
    doctor_lines 의 반환을 tmp/test-output/<테스트 이름>.txt 로 쓴다. 테스트 본문은 건드리지 않는다 —
    cli 모듈의 함수를 감싸서 가로챈다. env 가 없으면 아무것도 하지 않는다(CI · 평소 실행)."""
    if not os.environ.get("OBS_TEST_DUMP"):
        yield
        return
    captured: list[Block] = []
    monkeypatch.setattr(cli, "promql_run", _recording(cli.promql_run, captured))
    monkeypatch.setattr(cli, "logql_run", _recording(cli.logql_run, captured))
    monkeypatch.setattr(cli, "traceql_run", _recording(cli.traceql_run, captured))
    monkeypatch.setattr(cli, "metrics_run", _recording(cli.metrics_run, captured))
    monkeypatch.setattr(cli, "doctor_lines", _recording_doctor(cli.doctor_lines, captured))
    yield
    if not captured:
        return
    node = cast(pytest.Item, getattr(request, "node"))   # FixtureRequest.node 는 pytest stub 에 타입이 없다
    out = ROOT / "tmp" / "test-output" / (node.name + ".txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n\n".join("%s\n%s" % (b.title, "\n".join(b.lines)) for b in captured) + "\n", encoding="utf-8")
