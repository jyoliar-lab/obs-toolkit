"""공용 fixture — 녹화 응답 로더 · 가짜 transport · 격리된 env."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping, ParamSpec, Protocol, cast

import pytest

from obs.adapters import cli
from obs.core import JSON, Client, Config, Evidence, Lines, TransportError, load_config

ROOT: Path = Path(__file__).resolve().parents[1]
FIXTURES: Path = ROOT / "fixtures"


def load_fixture(name: str) -> JSON:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class Call:
    url: str
    headers: dict[str, str]
    timeout: float


@dataclass
class FakeTransport:
    """(url, headers, timeout) → (status, body). 경로 조각으로 응답을 고른다. 부른 기록을 남긴다."""
    routes: dict[str, tuple[int, object]] = field(default_factory=dict[str, tuple[int, object]])
    calls: list[Call] = field(default_factory=list[Call])
    fail: str | None = None   # TransportError 문구 — 있으면 항상 실패

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        self.calls.append(Call(url=url, headers=dict(headers), timeout=timeout))
        if self.fail is not None:
            raise TransportError(self.fail)
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
    def __call__(self, routes: dict[str, tuple[int, object]] | None = None, fail: str | None = None) -> tuple[Client, FakeTransport]: ...


@pytest.fixture
def make_client(config: Config) -> ClientFactory:
    def _make(routes: dict[str, tuple[int, object]] | None = None, fail: str | None = None) -> tuple[Client, FakeTransport]:
        t = FakeTransport(routes=routes or {}, fail=fail)
        return Client(config=config, token="test-token-not-real", transport=t), t
    return _make


# ── 출력 덤프 — 테스트가 받은 Evidence 를 사람이 파일로 본다 ─────────────────────

P = ParamSpec("P")


def _recording(fn: Callable[P, cli.RunResult], sink: list[Evidence[Lines]]) -> Callable[P, cli.RunResult]:
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> cli.RunResult:
        result = fn(*args, **kwargs)
        sink.append(result[0])
        return result
    return wrapped


@pytest.fixture(autouse=True)
def dump_evidence(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """OBS_TEST_DUMP=1 이면 각 테스트가 promql_run·logql_run·traceql_run 에서 받은 Evidence.value.lines 를
    tmp/test-output/<테스트 이름>.txt 로 쓴다. 테스트 본문은 건드리지 않는다 — cli 모듈의 함수를 감싸서 가로챈다.
    env 가 없으면 아무것도 하지 않는다(CI · 평소 실행)."""
    if not os.environ.get("OBS_TEST_DUMP"):
        yield
        return
    captured: list[Evidence[Lines]] = []
    monkeypatch.setattr(cli, "promql_run", _recording(cli.promql_run, captured))
    monkeypatch.setattr(cli, "logql_run", _recording(cli.logql_run, captured))
    monkeypatch.setattr(cli, "traceql_run", _recording(cli.traceql_run, captured))
    yield
    if not captured:
        return
    node = cast(pytest.Item, getattr(request, "node"))   # FixtureRequest.node 는 pytest stub 에 타입이 없다
    out = ROOT / "tmp" / "test-output" / (node.name + ".txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    blocks = ["# %s  %s  %s\n%s" % (e.backend, e.query, e.window.label() if e.window else "-", "\n".join(e.value.lines))
              for e in captured]
    out.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
