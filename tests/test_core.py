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
    """기간 표기 파서 — Prometheus/Loki 와 같은 표기(30s·5m·1.5h·2d·500ms)를 초로 바꾸고, 표기가 아니면 ObsError."""
    assert parse_duration("30s") == 30 and parse_duration("5m") == 300 and parse_duration("1.5h") == 5400
    assert parse_duration("2d") == 172800 and parse_duration("500ms") == 0.5
    with pytest.raises(ObsError):
        parse_duration("yesterday")


def test_parse_time() -> None:
    """시각 표기 파서 — None/"now" 는 기준 시각, "-1h" 는 기준 대비 상대, 숫자는 unix 초, RFC3339 는 UTC 로 해석. 그 밖은 ObsError."""
    assert parse_time(None, 100.0) == 100.0 and parse_time("now", 100.0) == 100.0
    assert parse_time("-1h", 10000.0) == 6400.0 and parse_time("1234", 0.0) == 1234.0
    assert parse_time("2026-09-08T00:00:00Z", 0.0) == 1788825600.0
    with pytest.raises(ObsError):
        parse_time("tomorrow", 0.0)


def test_window() -> None:
    """Window — since/end 로 창을 만들고(길이·경계), shifted 가 창을 옮기며, with_baseline 은 본 창을 그대로 둔 채
    같은 창을 offset 만큼 앞으로 옮겨 기준선으로 달고(start·end 둘 다 명시 검증), label 과 to_dict(중첩 baseline 포함)가 맞는지."""
    w = Window.last("1h", "-30m", at=10000.0)
    assert (w.start, w.end, w.seconds) == (4600.0, 8200.0, 3600.0)
    assert w.shifted(60) == Window(4660.0, 8260.0)
    b = w.with_baseline("1d")
    assert (b.start, b.end) == (w.start, w.end)                                   # 본 창은 그대로
    assert b.baseline is not None
    assert (b.baseline.start, b.baseline.end) == (w.start - 86400, w.end - 86400)  # 하루 앞의 같은 창
    assert w.label() == "%s ~ %s" % (fmt_rfc3339(4600.0), fmt_rfc3339(8200.0))
    d = to_dict(b)
    assert isinstance(d, dict)
    bl = d["baseline"]
    assert isinstance(bl, dict) and bl["start"] == 4600.0 - 86400 and bl["baseline"] is None


# ── 직렬화 ──────────────────────────────────────────────────────────────────────

def test_dataclasses_are_json_serializable(config: Config) -> None:
    """어댑터 경계 요구 — 코어의 dataclass(Evidence·Config·Budget·Response)가 전부 to_json 으로 직렬화되고, 중첩(window·value)이 dict 로 풀린다.
    JSON 으로 옮길 수 없는 값(object())은 조용히 통과시키지 않고 TypeError."""
    ev = Evidence(backend="metrics", query="up", window=Window(1.0, 2.0), value=Lines(["1 {}"]), raw_ref="tmp/obs/x.json")
    for obj in (ev, config, Budget(), Response("metrics", "api/v1/query", 200, {"a": 1})):
        json.loads(to_json(obj))
    d = to_dict(ev)
    assert isinstance(d, dict) and d["window"] == {"start": 1.0, "end": 2.0, "baseline": None} and d["value"] == {"lines": ["1 {}"]}
    with pytest.raises(TypeError):
        to_dict(object())


# ── spill ───────────────────────────────────────────────────────────────────────

def test_spill_root_prefers_project_dir(tmp_path: Path) -> None:
    """spill 자리 — CLAUDE_PROJECT_DIR 이 있으면 그 아래 tmp/obs, 없으면 cwd 의 tmp/obs. 플러그인 캐시 디렉터리를 가리키던 #90 의 parents[4] 산술을 대체한 규칙이다."""
    assert spill_root({"CLAUDE_PROJECT_DIR": str(tmp_path)}) == tmp_path / "tmp" / "obs"
    assert spill_root({}, cwd=tmp_path / "cwd") == tmp_path / "cwd" / "tmp" / "obs"


def test_spill_writes_and_prunes(tmp_path: Path) -> None:
    """spill — 원본이 JSON 파일로 떨어지고(이름은 재사용되지 않는다), keep 개를 넘으면 오래된 것부터 지워 정확히 최근 keep 개(어느 파일인지 · 내용까지)만 남으며,
    spill_ref 가 프로젝트 기준 상대 경로를 돌려준다."""
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    paths = [spill("k", {"i": i}, env, keep=3) for i in range(5)]
    root = spill_root(env)
    assert all(p.parent == root for p in paths)
    assert len(set(paths)) == 5                                                        # 이름 재사용 없음 — raw_ref 는 그 응답만 가리킨다
    assert set(root.glob("*.json")) == set(paths[2:])                                  # 최근 3개만 남는다
    assert all(not p.exists() for p in paths[:2])                                     # 오래된 2개는 지워졌다
    assert [json.loads(p.read_text()) for p in paths[2:]] == [{"i": 2}, {"i": 3}, {"i": 4}]   # 내용도 제자리
    assert spill_ref(paths[-1], env) == "tmp/obs/" + paths[-1].name


def test_prune_oldest_first(tmp_path: Path) -> None:
    """prune 단독 — mtime 이 오래된 파일부터 지우고 지운 개수를 돌려준다."""
    for i in range(4):
        p = tmp_path / ("%d.json" % i)
        p.write_text("{}")
        os.utime(p, (1000 + i, 1000 + i))
    assert prune(tmp_path, keep=2) == 2
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["2.json", "3.json"]


def test_prune_protects_just_written_file(tmp_path: Path) -> None:
    """prune 의 protect — 방금 쓴 파일은 mtime 이 가장 오래돼 보여도 지우지 않고, 나머지에서 keep-1 개만 남긴다.
    같은 초 안에 여러 번 spill 할 때 이름 순서(-2, -3 접미)가 mtime 동률을 잘못 가르는 경우를 구조로 막는다."""
    files = [tmp_path / ("%d.json" % i) for i in range(4)]
    for i, p in enumerate(files):
        p.write_text("{}")
        os.utime(p, (1000 + i, 1000 + i))
    os.utime(files[3], (900, 900))                       # 방금 쓴 파일인데 mtime 은 가장 오래됨
    assert prune(tmp_path, keep=2, protect=files[3]) == 2
    assert sorted(p.name for p in tmp_path.glob("*.json")) == ["2.json", "3.json"]


# ── 예산 ────────────────────────────────────────────────────────────────────────

def test_budget_queries(make_client: ClientFactory) -> None:
    """예산 — 질의 횟수 상한을 넘기면 transport 를 부르기 전에 BudgetExceeded. 실제로 나간 호출 수가 상한과 같음을 FakeTransport 기록으로 확인한다."""
    client, t = make_client({"api/v1/query": (200, {"status": "success", "data": {}})})
    client.budget = Budget(max_queries=2)
    client.get("metrics", "api/v1/query"); client.get("metrics", "api/v1/query")
    with pytest.raises(BudgetExceeded):
        client.get("metrics", "api/v1/query")
    assert len(t.calls) == 2


def test_budget_bytes(make_client: ClientFactory) -> None:
    """예산 — 받은 응답 바이트 누적이 상한을 넘기면 BudgetExceeded. 한 조사가 원본을 무한정 끌어오지 못하게 하는 겹이다."""
    client, _ = make_client({"api/v1/query": (200, {"pad": "x" * 100})})
    client.budget = Budget(max_response_bytes=150)
    client.get("metrics", "api/v1/query")
    with pytest.raises(BudgetExceeded):
        client.get("metrics", "api/v1/query")


# ── Client.get ──────────────────────────────────────────────────────────────────

def test_get_builds_proxy_url_and_bearer(make_client: ClientFactory, config: Config) -> None:
    """Client.get 의 물리 조립 — 논리 이름을 Config 의 proxy_url 로 풀고, None·"" 인 파라미터는 버리며, Bearer 토큰과 Accept 헤더를 붙인다.
    돌려주는 Response 에는 URL 이 없다(backend·path·status·body 만)."""
    client, t = make_client({"api/v1/query": (200, {"status": "success"})})
    r = client.get("metrics", "/api/v1/query", {"query": "up", "time": 1.5, "empty": "", "none": None})
    assert r == Response("metrics", "/api/v1/query", 200, {"status": "success"})
    call = t.calls[0]
    assert call.url == config.proxy_url("metrics", "api/v1/query") + "?query=up&time=1.5"
    assert call.headers["Authorization"] == "Bearer test-token-not-real"
    assert call.headers["Accept"] == "application/json"


def test_get_logical_names_only(make_client: ClientFactory) -> None:
    """agent 인터페이스에 물리가 없다 — uid("prometheus")를 backend 자리에 넣으면 거부된다. 논리 이름만 받는다."""
    client, _ = make_client()
    with pytest.raises(ObsError):
        client.get("prometheus", "api/v1/query")


def test_get_non_json_body(make_client: ClientFactory) -> None:
    """JSON 이 아닌 응답(Tempo /api/echo 의 "echo")은 str 로 body 에 실린다 — 파싱 실패를 오류로 만들지 않는다."""
    client, _ = make_client({"api/echo": (200, b"echo")})
    assert client.get("traces", "api/echo").body == "echo"


@pytest.mark.parametrize("status,needle", [(401, "인증 실패"), (403, "거부"), (404, "거부"), (500, "오류"), (429, "속도 제한")])
def test_http_errors_are_logical(make_client: ClientFactory, status: int, needle: str) -> None:
    """오류 문구 규약 — 401·403·404·500·429 가 논리 이름으로 시작하고 상태별 다음 행동을 말하며, 백엔드가 본문에 호스트를 흘려도 문구에 물리 문자열이 없다."""
    client, _ = make_client({"loki/api/v1/query_range": (status, {"message": "secret host obs.mg.jyo-liar.com"})})
    with pytest.raises(ObsError) as ei:
        client.get("logs", "loki/api/v1/query_range")
    msg = str(ei.value)
    assert msg.startswith("logs ") and needle in msg
    for word in PHYSICAL:
        assert word not in msg, msg


def test_query_error_passes_backend_message(make_client: ClientFactory) -> None:
    """예외 규약 — 질의 오류(400·422)는 agent 가 고칠 수 있는 것이라 백엔드 메시지(파서 오류 등)를 그대로 통과시키고 "질의를 고쳐 다시" 로 끝난다."""
    client, _ = make_client({"api/v1/query": (400, {"status": "error", "error": 'parse error: unexpected "}"'})})
    with pytest.raises(ObsError) as ei:
        client.get("metrics", "api/v1/query", {"query": "up}"})
    assert 'parse error: unexpected "}"' in str(ei.value) and "질의를 고쳐" in str(ei.value)
    assert http_error_message("logs", 422, "plain text reason") == "logs 백엔드가 질의를 거부했다(422): plain text reason — 질의를 고쳐 다시."


def test_transport_failure_is_logical(make_client: ClientFactory) -> None:
    """연결 실패(DNS·VPN) — 논리 이름으로 "닿지 못했다" 와 obs-doctor 안내만 말하고 호스트·uid 는 없다."""
    client, _ = make_client(fail="[Errno 8] nodename nor servname provided")
    with pytest.raises(ObsError) as ei:
        client.get("traces", "api/search")
    msg = str(ei.value)
    assert msg.startswith("traces 백엔드에 닿지 못했다") and "obs-doctor" in msg
    for word in PHYSICAL:
        assert word not in msg


def test_error_messages_never_carry_token(make_client: ClientFactory) -> None:
    """백엔드가 오류 본문에 토큰을 되돌려 보내도 agent 가 보는 문구에는 토큰이 없다 — 401 은 본문을 인용하지 않는 상태 코드다."""
    client, _ = make_client({"api/v1/query": (401, {"message": "bad token test-token-not-real"})})
    with pytest.raises(ObsError) as ei:
        client.get("metrics", "api/v1/query")
    assert "test-token-not-real" not in str(ei.value)


# ── doctor ──────────────────────────────────────────────────────────────────────

def test_doctor_shows_physical_and_probes(config: Config, base_env: dict[str, str]) -> None:
    """doctor 는 사람용 — 물리 URL·uid·출처·spill 자리를 보이고 백엔드마다 probe 결과(ok/HTTP/논리 오류 문구)를 담되, 토큰 값은 직렬화해도 나오지 않는다.
    한 백엔드가 401 이면 보고서 전체가 ok=False."""
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
    """토큰이 없으면 doctor 는 연결 시도를 하지 않고(transport 호출 0) 각 probe 에 "토큰 없음" 을 적는다 — 없는 토큰으로 401 을 만들어 보이지 않는다."""
    t = FakeTransport()
    rep = doctor(config, "", env=base_env, transport=t)
    assert not rep.token_present and not rep.ok and not t.calls
    assert all(not p.ok and "토큰 없음" in p.detail for p in rep.probes)
