"""fixture 규약 — 모든 fixture 가 녹화 형식(요청 · 응답 분리)이고, 물리 정보가 없으며, 표시 없는 파일은 녹화기가 만든 것이다.
녹화기(tests/record.py) 자체는 FakeTransport 로 검사한다 — 실환경 토큰 없이 형식 계약이 CI 에서 산다."""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pytest

from obs.core import Client, Config
from tests import record
from tests.conftest import FIXTURES, FakeTransport, _no_physical
from tests.record import DERIVED, RECIPES, RECORDED_BY, RECORDED_MANUAL, Fixture, Recorder, fixture_names, read_fixture, response_of, write_fixture

FIXTURE_FILES: list[Path] = sorted(FIXTURES.glob("*"))
NAMES: list[str] = [p.name for p in FIXTURE_FILES]
TOKEN_MARKS: tuple[str, ...] = ("Authorization", "Bearer ", "glsa_")


def _fixture(name: str) -> Fixture:
    return read_fixture(FIXTURES / name)


# ── 파일 전부 — 형식 · 물리 비노출 · 출처 표시 ────────────────────────────────────

def test_every_fixture_has_a_recipe_and_vice_versa() -> None:
    """fixtures/ 의 파일 집합과 녹화기가 만드는 이름(실행 + 파생)의 집합이 같다."""
    assert NAMES == fixture_names()
    assert len(NAMES) == 28
    assert not set(RECIPES) & set(DERIVED)
    assert DERIVED == {"traceql-search.json": ("traceql-trace.json", 0),
                       "logql-first-seen-careerhub-prod.json": ("logql-templates-careerhub-prod.json", 1)}


@pytest.mark.parametrize("name", NAMES)
def test_fixture_is_recorded_format_with_request_and_response(name: str) -> None:
    """모든 fixture 는 호출마다 요청(백엔드 · 경로 · query string · params · at)과 응답이 구분된 녹화 형식이다."""
    fx = _fixture(name)
    assert fx.calls, name
    for c in fx.calls:
        rq = c.request
        assert rq.backend in ("metrics", "logs", "traces")
        assert rq.path
        assert not rq.path.startswith("/")
        assert "?" not in rq.path
        assert rq.path.startswith(("api/", "loki/api/"))
        assert isinstance(rq.params, dict)
        assert c.response.status == 200
        assert c.response.body is not None
        if not fx.manual:
            assert rq.at is not None
            assert rq.at_rfc3339 is not None
            assert rq.at_rfc3339 == record.fmt_rfc3339(rq.at)
    paths = [c.request.path for c in fx.calls]
    if paths.count(paths[-1]) == 1:   # 같은 경로의 호출이 여럿인 실행(around · templates)은 경로로 하나를 고를 수 없다 — 그것은 replay 가 순서로 돌려준다
        assert response_of(fx, paths[-1], name) == fx.calls[-1].response.body


def test_unmarked_fixtures_come_from_recorder_and_manual_ones_say_why() -> None:
    """recorded 는 tests/record.py 또는 manual 뿐이고 manual 은 reason 을 스스로 말한다."""
    manual: list[str] = []
    for name in NAMES:
        fx = _fixture(name)
        assert fx.recorded in (RECORDED_BY, RECORDED_MANUAL), (name, fx.recorded)
        if fx.manual:
            assert fx.reason, name
            manual.append(name)
        else:
            assert fx.reason is None
    assert manual == ["traceql-trace-error.json"]   # 오류 trace 는 주문할 수 없다 — 검색 상한(168h) 안에 없으면 손 기록


# ── 내용 — 티켓이 못 박은 것 ─────────────────────────────────────────────────────

def test_trace_fixture_records_search_then_trace_in_order() -> None:
    """traceql-trace.json 은 api/search → api/traces/<id> 순이고 둘째의 id 는 첫째 응답의 첫 hit 이며 traceql-search.json 은 그 첫 호출이다."""
    fx = _fixture("traceql-trace.json")
    paths = [c.request.path for c in fx.calls]
    assert len(paths) == 2
    assert paths[0] == "api/search"
    assert paths[1].startswith("api/traces/")
    body = fx.calls[0].response.body
    assert isinstance(body, dict)
    first = body["traces"]
    assert isinstance(first, list)
    assert isinstance(first[0], dict)
    assert paths[1] == "api/traces/%s" % first[0]["traceID"]
    assert "limit" in fx.calls[0].request.params
    assert fx.calls[0].request.at == fx.calls[1].request.at
    # traceql-search.json 은 같은 실행의 첫 호출 — 그래서 test_cli 가 search 첫 줄에서 뽑는 id 가 trace fixture 의 id 와 같다
    search = _fixture("traceql-search.json")
    assert len(search.calls) == 1
    assert search.calls[0] == fx.calls[0]


def test_metrics_fixtures_record_assembled_queries() -> None:
    """promql-instant-* fixture 의 요청에는 obs/metrics.py 가 조립한 실제 질의가 있다."""
    def query_of(name: str) -> str:
        fx = _fixture(name)
        assert fx.calls[0].request.path == "api/v1/query"
        assert "time" in fx.calls[0].request.params
        q = fx.calls[0].request.params["query"]
        assert isinstance(q, str)
        return q

    q = query_of("promql-instant-summarize-cpu-by-pod.json")
    assert q.count("label_replace(") == 7
    assert q.count(" or ") == 6
    assert "count_over_time(" in q
    assert "deriv(" in q
    assert record.CPU_QUERY in q
    q = query_of("promql-instant-compare-cpu-by-pod-1d.json")
    assert q.count("label_replace(") == 2
    assert "offset 86400s" in q
    assert '"stat", "now"' in q
    assert '"stat", "base"' in q
    q = query_of("promql-instant-compare-cpu-by-pod-no-baseline.json")
    assert "offset 5184000s" in q
    assert record.CPU_QUERY in q
    q = query_of("promql-instant-empty.json")
    assert q.count("label_replace(") == 7
    assert record.EMPTY_QUERY in q
    assert record.CPU_QUERY not in q
    q = query_of("promql-instant-topk-memory-by-pod.json")
    assert q == "topk(10, sum by (pod) (container_memory_working_set_bytes))"


def test_at_is_explicit_and_not_derived_from_url() -> None:
    """at 은 명시 필드이고 백엔드마다 다른 URL 단위(초 · 나노초 · 정수 초)와 맞는다."""
    prom = _fixture("promql-instant-up.json").calls[0].request
    loki = _fixture("logql-streams-careerhub-prod.json").calls[0].request
    tempo = _fixture("traceql-search.json").calls[0].request
    assert prom.at is not None
    assert prom.params["time"] == str(prom.at)
    assert loki.at is not None
    assert loki.params["end"] == str(int(loki.at * 1e9))
    assert tempo.at is not None
    assert tempo.params["end"] == str(int(tempo.at))


# ── 녹화기 — FakeTransport 로. 실환경 없이 형식 계약을 검사한다 ─────────────────────

def _recorded_session(config: Config, tmp_path: Path) -> tuple[FakeTransport, Path]:
    """Recorder 를 끼운 Client 로 호출 둘을 보내고 파일로 쓴다 — (안쪽 transport, 쓴 파일)."""
    inner = FakeTransport(config, routes={("metrics", "api/v1/query"): (200, {"status": "success", "data": {"resultType": "vector", "result": []}}),
                                          ("traces", "api/traces/abc"): (200, {"batches": []})})
    rec = Recorder(inner=inner, config=config, at=1000.0)
    client = Client(config=config, token="test-token-not-real", transport=rec)
    r1 = client.get("metrics", "api/v1/query", {"query": "up", "time": 1000.0, "match[]": "a"})
    r2 = client.get("traces", "api/traces/abc")
    assert r1.status == 200
    assert r2.status == 200
    return inner, rec.save(tmp_path / "x.json")


def test_recorder_passes_calls_through(config: Config, tmp_path: Path) -> None:
    """Recorder 는 래퍼다 — 안쪽 transport 가 평소처럼 호출을 전부 받는다."""
    inner, _ = _recorded_session(config, tmp_path)
    assert len(inner.calls) == 2


def test_recorded_file_carries_no_token_or_physical(config: Config, tmp_path: Path) -> None:
    """쓴 파일 전체에 토큰 · Authorization · 물리 문자열이 없다."""
    _, path = _recorded_session(config, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "test-token-not-real" not in text
    assert "Authorization" not in text
    _no_physical(text)


def test_recorder_writes_envelope_with_logical_requests_in_order(config: Config, tmp_path: Path) -> None:
    """봉투에는 호출이 순서대로, 논리 이름 · 네이티브 경로 · query string · params · 같은 at 으로 실리고 응답은 status · body 만이다."""
    _, path = _recorded_session(config, tmp_path)
    fx = read_fixture(path)
    assert fx.recorded == RECORDED_BY
    assert fx.reason is None
    assert not fx.manual
    assert [(c.request.backend, c.request.path) for c in fx.calls] == [("metrics", "api/v1/query"), ("traces", "api/traces/abc")]
    q = fx.calls[0].request
    assert q.at == 1000.0
    assert q.at_rfc3339 == "1970-01-01T00:16:40Z"
    assert q.query_string == "query=up&time=1000.0&match%5B%5D=a"
    assert q.params == {"query": "up", "time": "1000.0", "match[]": "a"}
    assert fx.calls[1].request.query_string == ""
    assert fx.calls[1].request.params == {}
    assert fx.calls[1].request.at == 1000.0
    assert fx.calls[0].response.body == {"status": "success", "data": {"resultType": "vector", "result": []}}


def test_response_of_requires_a_unique_path_match(config: Config, tmp_path: Path) -> None:
    """response_of 는 경로 조각으로 호출 하나를 고른다 — 조각 없음 · 없는 경로는 ValueError."""
    _, path = _recorded_session(config, tmp_path)
    fx = read_fixture(path)
    assert response_of(fx, "api/traces/", "x") == {"batches": []}
    with pytest.raises(ValueError):
        response_of(fx, None, "x")
    with pytest.raises(ValueError):
        response_of(fx, "loki/", "x")


def _always_ok(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
    return 200, b"{}"


def test_recorder_refuses_unknown_physical_prefix(config: Config) -> None:
    """접두를 모르는 URL 은 Recorder 가 기록하지 않는다."""
    rec = Recorder(inner=_always_ok, config=config, at=1.0)   # inner 가 통과시켜도 Recorder 가 거부한다
    with pytest.raises(ValueError):
        rec("https://elsewhere.example/x", {}, 1.0)
    assert rec.calls == []


def test_write_fixture_refuses_empty_calls(tmp_path: Path) -> None:
    """빈 기록은 쓰지 않는다."""
    with pytest.raises(ValueError):
        write_fixture(tmp_path / "empty.json", [])


def test_write_fixture_manual_requires_reason(tmp_path: Path) -> None:
    """manual 기록은 reason 없이 쓸 수 없다."""
    with pytest.raises(ValueError):
        write_fixture(tmp_path / "m.json", [record.RecordedCall(
            record.Request(None, None, "traces", "api/traces/x", "", {}), record.Reply(200, {}))], recorded=RECORDED_MANUAL)


def test_read_fixture_rejects_old_format(tmp_path: Path) -> None:
    """옛 형식(응답 본문만)은 로더가 거부한다."""
    old = tmp_path / "old.json"
    old.write_text('{"status": "success", "data": {}}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_fixture(old)


def test_parse_params_keeps_repeats_and_blanks() -> None:
    """parse_params 는 같은 키의 반복을 목록으로, 빈 값을 빈 문자열로 남긴다."""
    assert record.parse_params("a=1&b=&a=2&c=x%20y") == {"a": ["1", "2"], "b": "", "c": "x y"}


def test_decode_body_matches_client_get_rules() -> None:
    """decode_body 는 Client.get 과 같은 규칙 — 빈 본문은 {}, JSON 이 아니면 str."""
    assert record.decode_body(b"") == {}
    assert record.decode_body(b"echo") == "echo"
    assert record.decode_body(b'{"a":1}') == {"a": 1}
