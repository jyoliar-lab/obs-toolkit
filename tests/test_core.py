"""코어 — 시간 창 · 근거 · spill · 예산 · Client.get · 오류 문구 · 직렬화 · doctor."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from obs.core import (
    Budget, Config, Lines, BudgetExceeded, Evidence, ObsError, Response, Window, doctor, fmt_rfc3339, http_error_message,
    parse_duration, parse_time, prune, spill, spill_ref, spill_root, to_dict, to_json,
)
from tests.conftest import ClientFactory, FakeTransport

PHYSICAL: tuple[str, ...] = ("obs.mg", "jyo-liar", "prometheus", "loki-careerhub", "tempo-careerhub", "datasources/proxy")


# ── 시간 ────────────────────────────────────────────────────────────────────────

def test_parse_duration() -> None:
    assert parse_duration("30s") == 30 and parse_duration("5m") == 300 and parse_duration("1.5h") == 5400
    assert parse_duration("2d") == 172800 and parse_duration("500ms") == 0.5
    with pytest.raises(ObsError):
        parse_duration("yesterday")


def test_parse_time() -> None:
    assert parse_time(None, 100.0) == 100.0 and parse_time("now", 100.0) == 100.0
    assert parse_time("-1h", 10000.0) == 6400.0 and parse_time("1234", 0.0) == 1234.0
    assert parse_time("2026-09-08T00:00:00Z", 0.0) == 1788825600.0
    with pytest.raises(ObsError):
        parse_time("tomorrow", 0.0)


def test_window() -> None:
    w = Window.last("1h", "-30m", at=10000.0)
    assert (w.start, w.end, w.seconds) == (4600.0, 8200.0, 3600.0)
    b = w.with_baseline("1d")
    assert b.baseline is not None and b.baseline.end == w.end - 86400 and b.baseline.seconds == w.seconds
    assert w.label() == "%s ~ %s" % (fmt_rfc3339(4600.0), fmt_rfc3339(8200.0))
    d = to_dict(b)
    assert isinstance(d, dict)
    bl = d["baseline"]
    assert isinstance(bl, dict) and bl["start"] == 4600.0 - 86400 and bl["baseline"] is None


# ── 직렬화 ──────────────────────────────────────────────────────────────────────

def test_dataclasses_are_json_serializable(config: Config) -> None:
    ev = Evidence(backend="metrics", query="up", window=Window(1.0, 2.0), value=Lines(["1 {}"]), raw_ref="tmp/obs/x.json")
    for obj in (ev, config, Budget(), Response("metrics", "api/v1/query", 200, {"a": 1})):
        json.loads(to_json(obj))
    d = to_dict(ev)
    assert isinstance(d, dict) and d["window"] == {"start": 1.0, "end": 2.0, "baseline": None} and d["value"] == {"lines": ["1 {}"]}
    with pytest.raises(TypeError):
        to_dict(object())


# ── spill ───────────────────────────────────────────────────────────────────────

def test_spill_root_prefers_project_dir(tmp_path: Path) -> None:
    assert spill_root({"CLAUDE_PROJECT_DIR": str(tmp_path)}) == tmp_path / "tmp" / "obs"
    assert spill_root({}, cwd=tmp_path / "cwd") == tmp_path / "cwd" / "tmp" / "obs"


def test_spill_writes_and_prunes(tmp_path: Path) -> None:
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    paths = [spill("k", {"i": i}, env, keep=3) for i in range(5)]
    root = spill_root(env)
    assert all(p.parent == root for p in paths)
    remaining = sorted(root.glob("*.json"))
    assert len(remaining) == 3
    assert json.loads(paths[-1].read_text()) == {"i": 4} and paths[-1] in remaining
    assert spill_ref(paths[-1], env) == "tmp/obs/" + paths[-1].name


def test_prune_oldest_first(tmp_path: Path) -> None:
    for i in range(4):
        p = tmp_path / ("%d.json" % i)
        p.write_text("{}")
        os.utime(p, (1000 + i, 1000 + i))
    assert prune(tmp_path, keep=2) == 2
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["2.json", "3.json"]


# ── 예산 ────────────────────────────────────────────────────────────────────────

def test_budget_queries(make_client: ClientFactory) -> None:
    client, t = make_client({"api/v1/query": (200, {"status": "success", "data": {}})})
    client.budget = Budget(max_queries=2)
    client.get("metrics", "api/v1/query"); client.get("metrics", "api/v1/query")
    with pytest.raises(BudgetExceeded):
        client.get("metrics", "api/v1/query")
    assert len(t.calls) == 2


def test_budget_bytes(make_client: ClientFactory) -> None:
    client, _ = make_client({"api/v1/query": (200, {"pad": "x" * 100})})
    client.budget = Budget(max_response_bytes=150)
    client.get("metrics", "api/v1/query")
    with pytest.raises(BudgetExceeded):
        client.get("metrics", "api/v1/query")


# ── Client.get ──────────────────────────────────────────────────────────────────

def test_get_builds_proxy_url_and_bearer(make_client: ClientFactory, config: Config) -> None:
    client, t = make_client({"api/v1/query": (200, {"status": "success"})})
    r = client.get("metrics", "/api/v1/query", {"query": "up", "time": 1.5, "empty": "", "none": None})
    assert r == Response("metrics", "/api/v1/query", 200, {"status": "success"})
    call = t.calls[0]
    assert call.url == config.proxy_url("metrics", "api/v1/query") + "?query=up&time=1.5"
    assert call.headers["Authorization"] == "Bearer test-token-not-real"
    assert call.headers["Accept"] == "application/json"


def test_get_logical_names_only(make_client: ClientFactory) -> None:
    client, _ = make_client()
    with pytest.raises(ObsError):
        client.get("prometheus", "api/v1/query")


def test_get_non_json_body(make_client: ClientFactory) -> None:
    client, _ = make_client({"api/echo": (200, b"echo")})
    assert client.get("traces", "api/echo").body == "echo"


@pytest.mark.parametrize("status,needle", [(401, "인증 실패"), (403, "거부"), (404, "거부"), (500, "오류"), (429, "속도 제한")])
def test_http_errors_are_logical(make_client: ClientFactory, status: int, needle: str) -> None:
    client, _ = make_client({"loki/api/v1/query_range": (status, {"message": "secret host obs.mg.jyo-liar.com"})})
    with pytest.raises(ObsError) as ei:
        client.get("logs", "loki/api/v1/query_range")
    msg = str(ei.value)
    assert msg.startswith("logs ") and needle in msg
    for word in PHYSICAL:
        assert word not in msg, msg


def test_query_error_passes_backend_message(make_client: ClientFactory) -> None:
    client, _ = make_client({"api/v1/query": (400, {"status": "error", "error": 'parse error: unexpected "}"'})})
    with pytest.raises(ObsError) as ei:
        client.get("metrics", "api/v1/query", {"query": "up}"})
    assert 'parse error: unexpected "}"' in str(ei.value) and "질의를 고쳐" in str(ei.value)
    assert http_error_message("logs", 422, "plain text reason") == "logs 백엔드가 질의를 거부했다(422): plain text reason — 질의를 고쳐 다시."


def test_transport_failure_is_logical(make_client: ClientFactory) -> None:
    client, _ = make_client(fail="[Errno 8] nodename nor servname provided")
    with pytest.raises(ObsError) as ei:
        client.get("traces", "api/search")
    msg = str(ei.value)
    assert msg.startswith("traces 백엔드에 닿지 못했다") and "obs-doctor" in msg
    for word in PHYSICAL:
        assert word not in msg


def test_error_messages_never_carry_token(make_client: ClientFactory) -> None:
    client, _ = make_client({"api/v1/query": (401, {"message": "bad token test-token-not-real"})})
    with pytest.raises(ObsError) as ei:
        client.get("metrics", "api/v1/query")
    assert "test-token-not-real" not in str(ei.value)


# ── doctor ──────────────────────────────────────────────────────────────────────

def test_doctor_shows_physical_and_probes(config: Config, base_env: dict[str, str]) -> None:
    t = FakeTransport(routes={"buildinfo": (200, {}), "labels": (200, {}), "api/echo": (401, b"")})
    rep = doctor(config, "test-token-not-real", env=base_env, transport=t)
    assert rep.grafana_url.value.startswith("https://") and rep.token_present and not rep.ok
    by = {p.backend: p for p in rep.probes}
    assert by["metrics"].ok and by["logs"].ok and not by["traces"].ok
    assert by["traces"].detail.startswith("traces 백엔드 인증 실패")
    assert all(p.url.startswith(config.grafana_url.value) for p in rep.probes)
    assert rep.spill_dir.endswith(os.path.join("tmp", "obs"))
    assert "test-token-not-real" not in to_json(rep)


def test_doctor_without_token_skips_probes(config: Config, base_env: dict[str, str]) -> None:
    t = FakeTransport()
    rep = doctor(config, "", env=base_env, transport=t)
    assert not rep.token_present and not rep.ok and not t.calls
    assert all(not p.ok and "토큰 없음" in p.detail for p in rep.probes)
