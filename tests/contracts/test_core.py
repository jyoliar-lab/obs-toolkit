"""코어 — 시간 창 · 근거 · spill · 예산 · Client.get · 오류 문구 · 직렬화 · doctor."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from obs.core import (
    Budget, Config, Lines, BudgetExceeded, Client, Evidence, ObsError, Response, Window, doctor, fmt_rfc3339,
    http_error_message, load_config, parse_duration, parse_time, prune, spill, spill_ref, spill_root, to_dict, to_json,
)
from tests.conftest import PHYSICAL, ClientFactory, FakeTransport, RouteKey


# ── 시간 ────────────────────────────────────────────────────────────────────────

def test_parse_duration() -> None:
    """parse_duration 은 Prometheus 기간 표기(30s · 5m · 1.5h · 2d · 500ms)를 초로 바꾸고 표기가 아니면 ObsError 다."""
    assert parse_duration("30s") == 30
    assert parse_duration("5m") == 300
    assert parse_duration("1.5h") == 5400
    assert parse_duration("2d") == 172800
    assert parse_duration("500ms") == 0.5
    with pytest.raises(ObsError):
        parse_duration("yesterday")


def test_parse_time() -> None:
    """parse_time 은 None · "now" 를 기준 시각으로, "-1h" 를 상대로, 숫자를 unix 초로, RFC3339 를 UTC 로 읽고 그 밖은 ObsError 다."""
    assert parse_time(None, 100.0) == 100.0
    assert parse_time("now", 100.0) == 100.0
    assert parse_time("-1h", 10000.0) == 6400.0
    assert parse_time("1234", 0.0) == 1234.0
    assert parse_time("2026-09-08T00:00:00Z", 0.0) == 1788825600.0
    with pytest.raises(ObsError):
        parse_time("tomorrow", 0.0)


def test_window_last_builds_since_and_end() -> None:
    """Window.last 는 since · end 표기로 창을 만든다 — 길이와 양 끝."""
    w = Window.last("1h", "-30m", at=10000.0)
    assert (w.start, w.end, w.seconds) == (4600.0, 8200.0, 3600.0)


def test_window_shifted_moves_both_ends() -> None:
    """shifted 는 창의 양 끝을 같은 만큼 옮긴다."""
    assert Window(4600.0, 8200.0).shifted(60) == Window(4660.0, 8260.0)


def test_window_with_baseline_keeps_own_window_and_attaches_shifted_copy() -> None:
    """with_baseline 은 본 창을 그대로 둔 채 같은 창을 offset 만큼 앞으로 옮겨 기준선으로 단다."""
    w = Window(4600.0, 8200.0)
    b = w.with_baseline("1d")
    assert (b.start, b.end) == (w.start, w.end)
    assert b.baseline is not None
    assert (b.baseline.start, b.baseline.end) == (w.start - 86400, w.end - 86400)


def test_window_label_is_two_rfc3339_times() -> None:
    """label 은 시작 ~ 끝을 RFC3339 로 말한다."""
    assert Window(4600.0, 8200.0).label() == "%s ~ %s" % (fmt_rfc3339(4600.0), fmt_rfc3339(8200.0))


def test_window_to_dict_nests_baseline() -> None:
    """to_dict 는 기준선을 중첩된 dict 로 푼다 — 기준선의 기준선은 None."""
    d = to_dict(Window(4600.0, 8200.0).with_baseline("1d"))
    assert isinstance(d, dict)
    bl = d["baseline"]
    assert isinstance(bl, dict)
    assert bl["start"] == 4600.0 - 86400
    assert bl["baseline"] is None


# ── 직렬화 ──────────────────────────────────────────────────────────────────────

def test_dataclasses_are_json_serializable(config: Config) -> None:
    """코어의 dataclass 는 전부 to_json 으로 직렬화되고 중첩은 dict 로 풀리며, 옮길 수 없는 값은 TypeError 다."""
    ev = Evidence(backend="metrics", query="up", window=Window(1.0, 2.0), value=Lines(["1 {}"]), raw_ref="tmp/obs/x.json")
    for obj in (ev, config, Budget(), Response("metrics", "api/v1/query", 200, {"a": 1})):
        json.loads(to_json(obj))
    d = to_dict(ev)
    assert isinstance(d, dict)
    assert d["window"] == {"start": 1.0, "end": 2.0, "baseline": None}
    assert d["value"] == {"lines": ["1 {}"]}
    with pytest.raises(TypeError):
        to_dict(object())


# ── spill ───────────────────────────────────────────────────────────────────────

def test_spill_root_prefers_project_dir(tmp_path: Path) -> None:
    """spill 자리는 CLAUDE_PROJECT_DIR 아래 tmp/obs 이고, 없으면 cwd 의 tmp/obs 다."""
    assert spill_root({"CLAUDE_PROJECT_DIR": str(tmp_path)}) == tmp_path / "tmp" / "obs"
    assert spill_root({}, cwd=tmp_path / "cwd") == tmp_path / "cwd" / "tmp" / "obs"


def test_spill_writes_and_prunes(tmp_path: Path) -> None:
    """spill 은 호출마다 새 디렉터리에 raw.json · summary.txt 를 쓰고 keep 개를 넘으면 오래된 것부터 지우며 프로젝트 기준 상대 경로를 돌려준다."""
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    paths = [spill("k", {"i": i}, ["요약 %d" % i, "둘째 줄"], env, keep=3) for i in range(5)]
    root = spill_root(env)
    assert all(p.parent == root and p.name.endswith("-k") for p in paths)
    assert all(p.is_dir() for p in paths[2:])
    assert len(set(paths)) == 5                                                        # 이름 재사용 없음 — 경로는 그 호출만 가리킨다
    assert set(root.iterdir()) == set(paths[2:])                                       # 최근 3개만 남는다
    assert all(not p.exists() for p in paths[:2])                                     # 오래된 2개는 지워졌다
    assert [json.loads((p / "raw.json").read_text()) for p in paths[2:]] == [{"i": 2}, {"i": 3}, {"i": 4}]   # 원본도 제자리
    assert [(p / "summary.txt").read_text() for p in paths[2:]] == ["요약 %d\n둘째 줄\n" % i for i in (2, 3, 4)]  # 요약 전체
    assert sorted(q.name for q in paths[-1].iterdir()) == ["raw.json", "summary.txt"]
    assert spill_ref(paths[-1], env) == "tmp/obs/" + paths[-1].name


def test_prune_oldest_first(tmp_path: Path) -> None:
    """prune 은 mtime 이 오래된 항목(디렉터리 · 낱개 파일)부터 지우고 지운 개수를 돌려준다."""
    for i in range(4):
        p = tmp_path / ("%d" % i)
        if i % 2:
            p.mkdir(); (p / "raw.json").write_text("{}")
        else:
            p = p.with_suffix(".json"); p.write_text("{}")
        os.utime(p, (1000 + i, 1000 + i))
    assert prune(tmp_path, keep=2) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2.json", "3"]


def test_prune_protects_just_written_file(tmp_path: Path) -> None:
    """prune 은 protect 로 받은 항목을 mtime 과 무관하게 남기고 나머지에서 keep-1 개만 남긴다."""
    files = [tmp_path / ("%d" % i) for i in range(4)]
    for i, p in enumerate(files):
        p.mkdir(); (p / "raw.json").write_text("{}")
        os.utime(p, (1000 + i, 1000 + i))
    os.utime(files[3], (900, 900))                       # 방금 쓴 디렉터리인데 mtime 은 가장 오래됨
    assert prune(tmp_path, keep=2, protect=files[3]) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2", "3"]


# ── 예산 ────────────────────────────────────────────────────────────────────────

def test_budget_queries(make_client: ClientFactory) -> None:
    """질의 횟수가 상한을 넘기면 transport 를 부르기 전에 BudgetExceeded 다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, {"status": "success", "data": {}})})
    client.budget = Budget(max_queries=2)
    client.get("metrics", "api/v1/query"); client.get("metrics", "api/v1/query")
    with pytest.raises(BudgetExceeded):
        client.get("metrics", "api/v1/query")
    assert len(t.calls) == 2


def test_budget_bytes(make_client: ClientFactory) -> None:
    """받은 응답 바이트 누적이 상한을 넘기면 BudgetExceeded 다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, {"pad": "x" * 100})})
    client.budget = Budget(max_response_bytes=150)
    client.get("metrics", "api/v1/query")
    with pytest.raises(BudgetExceeded):
        client.get("metrics", "api/v1/query")


# ── Client.get ──────────────────────────────────────────────────────────────────

def test_get_builds_proxy_url_and_bearer(make_client: ClientFactory, config: Config) -> None:
    """Client.get 은 논리 이름을 proxy_url 로 풀고 None · "" 파라미터를 버리며 Bearer · Accept 헤더를 붙이고, Response 에는 URL 이 없다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, {"status": "success"})})
    r = client.get("metrics", "/api/v1/query", {"query": "up", "time": 1.5, "empty": "", "none": None})
    assert r == Response("metrics", "/api/v1/query", 200, {"status": "success"})
    call = t.calls[0]
    assert call.url == config.proxy_url("metrics", "api/v1/query") + "?query=up&time=1.5"
    assert call.headers["Authorization"] == "Bearer test-token-not-real"
    assert call.headers["Accept"] == "application/json"


def test_get_logical_names_only(make_client: ClientFactory) -> None:
    """backend 자리에 uid 를 넣으면 거부한다 — 논리 이름만 받는다."""
    client, _ = make_client()
    with pytest.raises(ObsError):
        client.get("prometheus", "api/v1/query")


def test_get_non_json_body(make_client: ClientFactory) -> None:
    """JSON 이 아닌 응답 본문은 str 로 실린다 — 파싱 실패를 오류로 만들지 않는다."""
    client, _ = make_client({("traces", "api/echo"): (200, b"echo")})
    assert client.get("traces", "api/echo").body == "echo"


@pytest.mark.parametrize("status,needle", [(401, "인증 실패"), (403, "거부"), (405, "거부"), (500, "오류"), (429, "속도 제한")])
def test_http_errors_are_logical(make_client: ClientFactory, status: int, needle: str) -> None:
    """401 · 403 · 405 · 500 · 429 의 문구는 논리 이름으로 시작하고 다음 행동을 말하며 물리 문자열이 없다."""
    client, _ = make_client({("logs", "loki/api/v1/query_range"): (status, {"message": "secret host obs.mg.jyo-liar.com"})})
    with pytest.raises(ObsError) as ei:
        client.get("logs", "loki/api/v1/query_range")
    msg = str(ei.value)
    assert msg.startswith("logs ")
    assert needle in msg
    for word in PHYSICAL:
        assert word not in msg, msg


def test_query_error_passes_backend_message(make_client: ClientFactory) -> None:
    """질의 오류(400 · 422)는 백엔드 메시지를 그대로 통과시키고 "질의를 고쳐 다시" 로 끝난다."""
    client, _ = make_client({("metrics", "api/v1/query"): (400, {"status": "error", "error": 'parse error: unexpected "}"'})})
    with pytest.raises(ObsError) as ei:
        client.get("metrics", "api/v1/query", {"query": "up}"})
    assert 'parse error: unexpected "}"' in str(ei.value)
    assert "질의를 고쳐" in str(ei.value)
    assert http_error_message("logs", 422, "plain text reason") == "logs 백엔드가 질의를 거부했다(422): plain text reason — 질의를 고쳐 다시."


def test_not_found_is_agent_fixable(make_client: ClientFactory) -> None:
    """404 는 백엔드 메시지를 통과시켜 "다시" 로 끝내고, "사람에게" 는 그 뒤에만 온다."""
    client, _ = make_client({("traces", "api/traces/deadbeef"): (404, {"error": "trace not found"})})
    with pytest.raises(ObsError) as ei:
        client.get("traces", "api/traces/deadbeef")
    msg = str(ei.value)
    assert msg.startswith("traces 백엔드에 그 자원이 없다(404): trace not found")
    assert "다시" in msg
    assert "사람에게" in msg.split("다시", 1)[1]   # 반복되는 404 만 사람에게 — 순서가 그렇다


@pytest.mark.parametrize("status", [502, 503, 504])
def test_gateway_errors_are_agent_fixable(make_client: ClientFactory, status: int) -> None:
    """502 · 503 · 504 는 "제때 답하지 못했다" 와 "좁혀 다시" 가 먼저 오고 물리 문자열이 없다."""
    client, _ = make_client({("logs", "loki/api/v1/query_range"): (status, b"<html>bad gateway obs.mg.jyo-liar.com</html>")})
    with pytest.raises(ObsError) as ei:
        client.get("logs", "loki/api/v1/query_range")
    msg = str(ei.value)
    assert msg.startswith("logs 백엔드가 제때 답하지 못했다(%d)" % status)
    assert "좁혀 다시" in msg
    assert "사람에게" in msg.split("다시", 1)[1]
    for word in PHYSICAL:
        assert word not in msg


def test_read_timeout_is_agent_fixable(make_client: ClientFactory) -> None:
    """읽기 시간 초과는 제한 시간을 말하고 "좁혀 다시" 로 보내며 "닿지 못했다" 라고 하지 않는다."""
    client, _ = make_client(hang=True)
    with pytest.raises(ObsError) as ei:
        client.get("logs", "loki/api/v1/query_range")
    msg = str(ei.value)
    assert msg.startswith("logs 백엔드가 30초 안에 답하지 않았다")
    assert "좁혀 다시" in msg
    assert "닿지 못했다" not in msg
    assert "사람에게" in msg.split("다시", 1)[1]


def test_urllib_read_timeout_maps_to_transport_timeout(base_env: dict[str, str], slow_server: LocalServer) -> None:
    """실제 urllib 경로에서 응답이 늦으면 TransportError 가 아니라 TransportTimeout 으로 분류된다."""
    client = Client(config=load_config({**base_env, "OBS_GRAFANA_URL": slow_server.base}), token="t", timeout=0.3)
    with pytest.raises(ObsError) as ei:
        client.get("logs", "loki/api/v1/query_range", {"query": "{a=\"b\"}"})
    assert "안에 답하지 않았다" in str(ei.value)
    assert "닿지 못했다" not in str(ei.value)


def test_transport_failure_is_logical(make_client: ClientFactory) -> None:
    """연결 실패는 논리 이름으로 "닿지 못했다" 와 obs-doctor.py 안내만 말하고 물리 문자열이 없다."""
    client, _ = make_client(fail="[Errno 8] nodename nor servname provided")
    with pytest.raises(ObsError) as ei:
        client.get("traces", "api/search")
    msg = str(ei.value)
    assert msg.startswith("traces 백엔드에 닿지 못했다")
    assert "obs-doctor.py" in msg
    for word in PHYSICAL:
        assert word not in msg


def test_error_messages_never_carry_token(make_client: ClientFactory) -> None:
    """백엔드가 오류 본문에 토큰을 되돌려도 agent 가 보는 문구에는 토큰이 없다."""
    client, _ = make_client({("metrics", "api/v1/query"): (401, {"message": "bad token test-token-not-real"})})
    with pytest.raises(ObsError) as ei:
        client.get("metrics", "api/v1/query")
    assert "test-token-not-real" not in str(ei.value)


# ── 실제 transport — 로컬 HTTP 서버로 프록시 행위를 본다 ─────────────────────────

@dataclass
class LocalServer:
    """요청 경로를 `seen` 에 기록하고 200 JSON 을 돌려주는 로컬 HTTP 서버."""
    base: str
    seen: list[str] = field(default_factory=list[str])


def _serve(delay: float) -> Iterator[LocalServer]:
    seen: list[str] = []

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.path)
            if delay:
                time.sleep(delay)
            body = b'{"status":"success","data":{}}'
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:   # noqa: A002 — BaseHTTPRequestHandler 시그니처
            return

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield LocalServer(base="http://127.0.0.1:%d" % srv.server_port, seen=seen)
    finally:
        srv.shutdown(); srv.server_close()


@pytest.fixture
def local_server() -> Iterator[LocalServer]:
    yield from _serve(0)


@pytest.fixture
def slow_server() -> Iterator[LocalServer]:
    """연결은 받되 응답을 1초 미룬다 — 읽기 시간 초과를 만든다."""
    yield from _serve(1.0)


def test_transport_never_consults_system_proxy(monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], local_server: LocalServer) -> None:
    """코어의 transport 는 시스템 프록시(getproxies · macOS sysconf)를 조회하지 않는다."""
    def forbidden(*_: object, **__: object) -> dict[str, str]:
        raise AssertionError("시스템 프록시를 조회했다 — env 프록시만 봐야 한다")
    monkeypatch.setattr(urllib.request, "getproxies", forbidden)
    monkeypatch.setattr(urllib.request, "getproxies_macosx_sysconf", forbidden, raising=False)
    monkeypatch.setattr(urllib.request, "proxy_bypass_macosx_sysconf", forbidden, raising=False)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(k, raising=False)
    client = Client(config=load_config({**base_env, "OBS_GRAFANA_URL": local_server.base}), token="t")
    r = client.get("metrics", "api/v1/query", {"query": "up"})
    assert r.status == 200
    assert local_server.seen == ["/api/datasources/proxy/uid/prometheus/api/v1/query?query=up"]


def test_transport_honors_env_proxy(monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str], local_server: LocalServer) -> None:
    """env 프록시(HTTP_PROXY)는 따른다 — 요청이 프록시에 절대 URI 로 닿는다."""
    monkeypatch.setenv("HTTP_PROXY", local_server.base)
    monkeypatch.delenv("NO_PROXY", raising=False); monkeypatch.delenv("no_proxy", raising=False)
    client = Client(config=load_config({**base_env, "OBS_GRAFANA_URL": "http://example.invalid"}), token="t")
    r = client.get("metrics", "api/v1/query", {"query": "up"})
    assert r.status == 200
    assert local_server.seen == ["http://example.invalid/api/datasources/proxy/uid/prometheus/api/v1/query?query=up"]


# ── doctor ──────────────────────────────────────────────────────────────────────

def _doctor_routes(traces_status: int) -> dict[RouteKey, tuple[int, object]]:
    """doctor 의 probe 세 개 — traces 만 상태를 바꿔 준다."""
    return {("metrics", "api/v1/status/buildinfo"): (200, {}), ("logs", "loki/api/v1/labels"): (200, {}), ("traces", "api/echo"): (traces_status, b"")}


def test_doctor_reports_physical_and_per_backend_probes(config: Config, base_env: dict[str, str]) -> None:
    """doctor 는 물리 URL · spill 자리를 보이고 백엔드마다 probe 결과를 담는다 — 한 백엔드가 401 이면 보고서 전체가 ok=False."""
    rep = doctor(config, "test-token-not-real", env=base_env, transport=FakeTransport(config, routes=_doctor_routes(401)))
    assert rep.grafana_url.value.startswith("https://")
    assert not rep.ok
    by = {p.backend: p for p in rep.probes}
    assert by["metrics"].ok
    assert by["logs"].ok
    assert not by["traces"].ok
    assert by["traces"].detail.startswith("traces 백엔드 인증 실패")
    assert all(p.url.startswith(config.grafana_url.value) for p in rep.probes)
    assert rep.spill_dir.endswith(os.path.join("tmp", "obs"))


def test_doctor_report_never_serializes_token(config: Config, base_env: dict[str, str]) -> None:
    """doctor 보고서를 직렬화해도 토큰 값은 나오지 않는다 — 있음/없음(token_present)뿐."""
    rep = doctor(config, "test-token-not-real", env=base_env, transport=FakeTransport(config, routes=_doctor_routes(200)))
    assert rep.token_present
    assert "test-token-not-real" not in to_json(rep)


def test_doctor_without_token_skips_probes(config: Config, base_env: dict[str, str]) -> None:
    """토큰이 없으면 doctor 는 연결 시도 없이 각 probe 에 "토큰 없음" 을 적는다."""
    t = FakeTransport(config)
    rep = doctor(config, "", env=base_env, transport=t)
    assert not rep.token_present
    assert not rep.ok
    assert not t.calls
    assert all(not p.ok and "토큰 없음" in p.detail for p in rep.probes)
