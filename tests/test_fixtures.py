"""fixture 규약 — 모든 fixture 가 녹화 형식(요청 · 응답 분리)이고, 물리 정보가 없으며, 표시 없는 파일은 녹화기가 만든 것이다.
녹화기(tests/record.py) 자체는 FakeTransport 로 검사한다 — 실환경 토큰 없이 형식 계약이 CI 에서 산다."""
from __future__ import annotations

from pathlib import Path

import pytest

from obs.core import Client, Config
from tests import record
from tests.conftest import FIXTURES, FakeTransport
from tests.record import DERIVED, RECIPES, RECORDED_BY, RECORDED_MANUAL, Fixture, Recorder, fixture_names, read_fixture, response_of, write_fixture
from tests.test_cli import PHYSICAL   # `_no_physical` 의 단어 목록 — fixture 전체에 같은 규약

FIXTURE_FILES: list[Path] = sorted(FIXTURES.glob("*"))
NAMES: list[str] = [p.name for p in FIXTURE_FILES]
TOKEN_MARKS: tuple[str, ...] = ("Authorization", "Bearer ", "glsa_")


def _no_physical(text: str) -> None:
    for word in PHYSICAL:
        assert word not in text, "fixture 에 물리 정보: %s" % word


def _fixture(name: str) -> Fixture:
    return read_fixture(FIXTURES / name)


# ── 파일 전부 — 형식 · 물리 비노출 · 출처 표시 ────────────────────────────────────

def test_every_fixture_has_a_recipe_and_vice_versa() -> None:
    """fixtures/ 의 파일 집합과 녹화기가 만드는 이름(실행 + 파생)의 집합이 같다 — 레시피 없는 fixture 도, fixture 없는 레시피도 없다(13개).
    파생은 traceql-search.json 하나 — traceql-trace.json 실행의 첫 호출."""
    assert NAMES == fixture_names() and len(NAMES) == 13 and not set(RECIPES) & set(DERIVED)
    assert DERIVED == {"traceql-search.json": ("traceql-trace.json", 0)}


@pytest.mark.parametrize("name", NAMES)
def test_fixture_is_recorded_format_with_request_and_response(name: str) -> None:
    """AC 1 — 파일 하나를 열면 무엇을 불러 받은 응답인지(백엔드 · 경로 · query string · 파라미터 · at)가 응답과 구분되어 있고, 각각 기계로 꺼내진다.
    at 은 녹화기 파일에서 float 이고 RFC3339 표기와 맞는다(manual 은 불명이면 None)."""
    fx = _fixture(name)
    assert fx.calls, name
    for c in fx.calls:
        rq = c.request
        assert rq.backend in ("metrics", "logs", "traces") and rq.path and not rq.path.startswith("/") and "?" not in rq.path
        assert rq.path.startswith(("api/", "loki/api/")) and isinstance(rq.params, dict)
        assert c.response.status == 200 and c.response.body is not None
        if not fx.manual:
            assert rq.at is not None and rq.at_rfc3339 is not None and rq.at_rfc3339 == record.fmt_rfc3339(rq.at)
    assert response_of(fx, fx.calls[-1].request.path, name) == fx.calls[-1].response.body


@pytest.mark.parametrize("name", NAMES)
def test_fixture_carries_no_physical_or_token(name: str) -> None:
    """AC 7 — 커밋되는 파일이다: `_no_physical` 의 단어 목록(호스트 · 프록시 경로 · uid)과 토큰 자국이 파일 전체에 없다."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    _no_physical(text)
    for mark in TOKEN_MARKS:
        assert mark not in text, "%s: %s" % (name, mark)


def test_unmarked_fixtures_come_from_recorder_and_manual_ones_say_why() -> None:
    """AC 5 — recorded 는 tests/record.py 또는 manual 뿐이고, manual 은 재현 불가 이유(reason)를 스스로 말한다."""
    manual: list[str] = []
    for name in NAMES:
        fx = _fixture(name)
        assert fx.recorded in (RECORDED_BY, RECORDED_MANUAL), (name, fx.recorded)
        if fx.manual:
            assert fx.reason, name
            manual.append(name)
        else:
            assert fx.reason is None
    assert manual == ["traceql-trace-error.json"]   # 오류 trace 는 주문할 수 없다 — 재녹화 시점(2026-09-22)에 168h 안에 없었다


# ── 내용 — 티켓이 못 박은 것 ─────────────────────────────────────────────────────

def test_trace_fixture_records_search_then_trace_in_order() -> None:
    """AC 3 — traceql-trace.json 에 api/search 와 api/traces/<id> 두 호출이 순서대로 있고, 둘째의 id 는 첫째 응답의 첫 hit 이다."""
    fx = _fixture("traceql-trace.json")
    paths = [c.request.path for c in fx.calls]
    assert len(paths) == 2 and paths[0] == "api/search" and paths[1].startswith("api/traces/")
    body = fx.calls[0].response.body
    assert isinstance(body, dict)
    first = body["traces"]
    assert isinstance(first, list) and isinstance(first[0], dict)
    assert paths[1] == "api/traces/%s" % first[0]["traceID"]
    assert "limit" in fx.calls[0].request.params and fx.calls[0].request.at == fx.calls[1].request.at
    # traceql-search.json 은 같은 실행의 첫 호출 — 그래서 test_cli 가 search 첫 줄에서 뽑는 id 가 trace fixture 의 id 와 같다
    search = _fixture("traceql-search.json")
    assert len(search.calls) == 1 and search.calls[0] == fx.calls[0]


def test_metrics_fixtures_record_assembled_queries() -> None:
    """AC 6 — PR #4 의 promql-instant-* 5개에 obs/metrics.py 가 조립한 실제 질의가 있다: label_replace(…) or … · offset · topk(…)."""
    def query_of(name: str) -> str:
        fx = _fixture(name)
        assert fx.calls[0].request.path == "api/v1/query" and "time" in fx.calls[0].request.params
        q = fx.calls[0].request.params["query"]
        assert isinstance(q, str)
        return q

    q = query_of("promql-instant-summarize-cpu-by-pod.json")
    assert q.count("label_replace(") == 7 and q.count(" or ") == 6 and "count_over_time(" in q and "deriv(" in q and record.CPU_QUERY in q
    q = query_of("promql-instant-compare-cpu-by-pod-1d.json")
    assert q.count("label_replace(") == 2 and "offset 86400s" in q and '"stat", "now"' in q and '"stat", "base"' in q
    q = query_of("promql-instant-compare-cpu-by-pod-no-baseline.json")
    assert "offset 5184000s" in q and record.CPU_QUERY in q
    q = query_of("promql-instant-empty.json")
    assert q.count("label_replace(") == 7 and record.EMPTY_QUERY in q and record.CPU_QUERY not in q
    q = query_of("promql-instant-topk-memory-by-pod.json")
    assert q == "topk(10, sum by (pod) (container_memory_working_set_bytes))"


def test_at_is_explicit_and_not_derived_from_url() -> None:
    """at 은 명시 필드 — 백엔드마다 URL 의 단위가 다르다(PromQL 초 · LogQL 나노초 · TraceQL 정수 초). 세 fixture 의 at 이 각 단위와 맞는다."""
    prom = _fixture("promql-instant-up.json").calls[0].request
    loki = _fixture("logql-streams-careerhub-prod.json").calls[0].request
    tempo = _fixture("traceql-search.json").calls[0].request
    assert prom.at is not None and prom.params["time"] == str(prom.at)
    assert loki.at is not None and loki.params["end"] == str(int(loki.at * 1e9))
    assert tempo.at is not None and tempo.params["end"] == str(int(tempo.at))


# ── 녹화기 — FakeTransport 로. 실환경 없이 형식 계약을 검사한다 ─────────────────────

def test_recorder_strips_physical_keeps_order_and_writes_envelope(config: Config, tmp_path: Path) -> None:
    """Recorder 는 Client.transport 래퍼다 — 나간 물리 URL 에서 접두(호스트 · 프록시 경로 · uid)를 잘라 논리 이름 · 네이티브 경로 · query string 만,
    응답은 status · body 만, 헤더는 기록하지 않는다. 호출 순서가 보존되고 같은 at 이 각 요청에 실린다. 파일 전체에 토큰 · 물리 문자열이 없다."""
    inner = FakeTransport(routes={"api/v1/query": (200, {"status": "success", "data": {"resultType": "vector", "result": []}}),
                                  "api/traces/": (200, {"batches": []})})
    rec = Recorder(inner=inner, config=config, at=1000.0)
    client = Client(config=config, token="test-token-not-real", transport=rec)
    r1 = client.get("metrics", "api/v1/query", {"query": "up", "time": 1000.0, "match[]": "a"})
    r2 = client.get("traces", "api/traces/abc")
    assert r1.status == 200 and r2.status == 200 and len(inner.calls) == 2    # 래퍼는 통과시킨다 — 실행은 평소와 같다
    path = rec.save(tmp_path / "x.json")
    text = path.read_text(encoding="utf-8")
    assert "test-token-not-real" not in text and "Authorization" not in text
    _no_physical(text)
    fx = read_fixture(path)
    assert fx.recorded == RECORDED_BY and fx.reason is None and not fx.manual
    assert [(c.request.backend, c.request.path) for c in fx.calls] == [("metrics", "api/v1/query"), ("traces", "api/traces/abc")]
    q = fx.calls[0].request
    assert q.at == 1000.0 and q.at_rfc3339 == "1970-01-01T00:16:40Z" and q.query_string == "query=up&time=1000.0&match%5B%5D=a"
    assert q.params == {"query": "up", "time": "1000.0", "match[]": "a"}
    assert fx.calls[1].request.query_string == "" and fx.calls[1].request.params == {} and fx.calls[1].request.at == 1000.0
    assert fx.calls[0].response.body == {"status": "success", "data": {"resultType": "vector", "result": []}}
    assert response_of(fx, "api/traces/", "x") == {"batches": []}
    with pytest.raises(ValueError):
        response_of(fx, None, "x")             # 둘 중 하나를 골라야 한다
    with pytest.raises(ValueError):
        response_of(fx, "loki/", "x")          # 없는 경로


def test_recorder_refuses_unknown_physical_prefix(config: Config) -> None:
    """접두를 모르는 URL 은 기록하지 않는다 — 호스트가 fixture 에 실리는 길을 구조로 막는다."""
    rec = Recorder(inner=FakeTransport(routes={"x": (200, {})}), config=config, at=1.0)
    with pytest.raises(ValueError):
        rec("https://elsewhere.example/x", {}, 1.0)
    assert rec.calls == []


def test_write_fixture_rules(tmp_path: Path) -> None:
    """빈 기록은 쓰지 않고, manual 은 reason 없이 쓸 수 없다. 옛 형식(응답 본문만)은 로더가 거부한다."""
    with pytest.raises(ValueError):
        write_fixture(tmp_path / "empty.json", [])
    with pytest.raises(ValueError):
        write_fixture(tmp_path / "m.json", [record.RecordedCall(
            record.Request(None, None, "traces", "api/traces/x", "", {}), record.Reply(200, {}))], recorded=RECORDED_MANUAL)
    old = tmp_path / "old.json"
    old.write_text('{"status": "success", "data": {}}', encoding="utf-8")
    with pytest.raises(ValueError):
        read_fixture(old)


def test_query_string_parsing_keeps_repeats_and_blanks() -> None:
    assert record.parse_params("a=1&b=&a=2&c=x%20y") == {"a": ["1", "2"], "b": "", "c": "x y"}
    assert record.decode_body(b"") == {} and record.decode_body(b"echo") == "echo" and record.decode_body(b'{"a":1}') == {"a": 1}


def test_recipes_send_what_tests_send() -> None:
    """레시피의 질의가 테스트가 보내는 것과 같다 — 후속 정합 검사의 전제."""
    from tests.test_metrics import CPU_QUERY
    assert record.CPU_QUERY == CPU_QUERY
