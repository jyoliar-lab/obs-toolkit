"""공용 fixture — 녹화 응답 로더 · 가짜 transport · 격리된 env."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

from obs.core import Client, Config, TransportError, load_config

ROOT: Path = Path(__file__).resolve().parents[1]
FIXTURES: Path = ROOT / "fixtures"


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class Call:
    url: str
    headers: dict[str, str]
    timeout: float


@dataclass
class FakeTransport:
    """(url, headers, timeout) → (status, body). 경로 조각으로 응답을 고른다. 부른 기록을 남긴다."""
    routes: dict[str, tuple[int, Any]] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
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


@pytest.fixture
def make_client(config: Config) -> Callable[..., tuple[Client, FakeTransport]]:
    def _make(routes: dict[str, tuple[int, Any]] | None = None, fail: str | None = None) -> tuple[Client, FakeTransport]:
        t = FakeTransport(routes=routes or {}, fail=fail)
        return Client(config=config, token="test-token-not-real", transport=t), t
    return _make
