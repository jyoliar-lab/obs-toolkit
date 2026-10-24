"""트레이스 환원 연산의 약속 — 파서 · 질의 조립 · 임계 폴백 · 분류 · CRISP · 엣지 조인 · 짝짓기 · 나간 요청 · 디스패치.
손 입력(OTLP · 검색 · vector 응답, 명시 라우트 FakeTransport)과 성질 단언만. 렌더링의 모양은 format/test_traces_lines.py."""
from __future__ import annotations

import json
import urllib.parse

import pytest

from obs import traces
from obs.adapters import cli
from obs.core import JSON, ObsError, Window, to_dict, to_json
from obs.metrics import parse_metric
from obs.traces import (
    THRESHOLD_BASELINE, THRESHOLD_SAMPLE, THRESHOLD_WINDOW, Bucket, ClientRow, CriticalPath, Edge, Endpoint, EndpointQuantiles, Exemplar,
    Exemplars, PathSpan, ServiceGraph, Span, SpanDelta, SpanMatch, SpanTime, Threshold, Trace, TraceDiff,
)
from tests.conftest import AT, ClientFactory, _no_physical, _vector
from tests.replay import Call

W: Window = Window.last("1h", at=AT)
INSTANT: str = "api/v1/query"
SEARCH: str = "api/search"
SELECT: str = "select(name, status, resource.service.name, resource.k8s.pod.name)"
MS: int = 1_000_000


def _params(call: Call) -> dict[str, str]:
    """나간 URL 의 query 파라미터 — 값은 문자열 그대로."""
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call.url).query, keep_blank_values=True))


def _path(call: Call) -> str:
    return urllib.parse.urlsplit(call.url).path


def _trace_path(trace_id: str) -> str:
    return "api/traces/%s" % trace_id


# ── 손 입력 — Tempo 검색(spanSets) · OTLP · SpanMatch · 분위수 · 임계 ─────────────────

def _attr(key: str, value: str) -> JSON:
    return {"key": key, "value": {"stringValue": value}}


def _hit_span(name: str, duration_ms: float, status: str | None = "unset", service: str | None = "svc", pod: str | None = "p",
              span_id: str = "s1", start_ns: int = 1_000_000_000) -> JSON:
    """검색 응답 spanSets[].spans[] 의 매치 span 하나 — None 인 속성은 싣지 않는다."""
    attrs: list[JSON] = []
    if service is not None:
        attrs.append(_attr("service.name", service))
    if pod is not None:
        attrs.append(_attr("k8s.pod.name", pod))
    if status is not None:
        attrs.append(_attr("status", status))
    return {"spanID": span_id, "name": name, "startTimeUnixNano": str(start_ns), "durationNanos": str(int(duration_ms * MS)), "attributes": attrs}


def _search(*hits: tuple[str, list[JSON]], legacy: bool = False) -> dict[str, JSON]:
    """검색 응답 — (traceID, 매치 span 들) 목록. legacy 면 옛 `spanSet` 하나로 싣는다."""
    out: list[JSON] = []
    for trace_id, spans in hits:
        item: dict[str, JSON] = {"traceID": trace_id, "rootServiceName": "svc", "rootTraceName": "root", "startTimeUnixNano": "1000000000", "durationMs": 1}
        if legacy:
            item["spanSet"] = {"spans": spans, "matched": len(spans)}
        else:
            item["spanSets"] = [{"spans": spans, "matched": len(spans)}]
        out.append(item)
    return {"traces": out, "metrics": {}}


def _span(name: str, span_id: str, parent: str = "", start_ms: float = 0.0, end_ms: float = 1.0, service: str = "svc", error: bool = False) -> Span:
    return Span(service=service, name=name, span_id=span_id, parent_id=parent, start_ns=int(round(start_ms * MS)), end_ns=int(round(end_ms * MS)),
                status_code="STATUS_CODE_ERROR" if error else "")


def _otlp(spans: list[Span]) -> dict[str, JSON]:
    """Span 목록을 OTLP(batches) 본문으로 — 서비스마다 batch 하나."""
    batches: list[JSON] = []
    for service in sorted({s.service for s in spans}):
        items: list[JSON] = [{"name": s.name, "spanId": s.span_id, "parentSpanId": s.parent_id, "startTimeUnixNano": str(s.start_ns),
                              "endTimeUnixNano": str(s.end_ns), "status": {"code": s.status_code}, "attributes": []} for s in spans if s.service == service]
        batches.append({"resource": {"attributes": [_attr("service.name", service)]}, "scopeSpans": [{"spans": items}]})
    return {"batches": batches}


def _sm(trace_id: str, name: str, duration_ms: float, status: str = "unset", service: str = "svc", span_id: str = "s", pod: str = "p") -> SpanMatch:
    return SpanMatch(trace_id=trace_id, span_id=span_id, name=name, start_ns=1_000_000_000, duration_ns=int(duration_ms * MS), status=status, service=service, pod=pod)


def _eq(service: str, name: str, base: float | None, now: float | None) -> EndpointQuantiles:
    return EndpointQuantiles(endpoint=Endpoint(service, name), base=base, now=now)


def _th(seconds: float, source: str = THRESHOLD_BASELINE) -> Threshold:
    return Threshold(seconds=seconds, source=source)


def _stat_series(service: str, name: str, stat: str, value: str) -> tuple[dict[str, str], str]:
    return {"service_name": service, "span_name": name, "stat": stat}, value


def _row(caller: str, endpoint: str, calls: int, errors: int = 0, now: float | None = None, base: float | None = None) -> ClientRow:
    return ClientRow(caller=caller, endpoint=endpoint, calls=calls, errors=errors, now=now, base=base)


def _ids(items: list[Exemplar]) -> list[str]:
    return [e.trace_id for e in items]


# ── parse_span_matches ───────────────────────────────────────────────────────────

def test_parse_span_matches_reads_every_span_of_every_span_set() -> None:
    """트레이스 둘 × spanSets 하나 × span 둘이면 매치 넷이고, 건마다 trace_id · span_id · name · start_ns · duration_ns · status · service · pod 가 응답의 것이다 — 응답 순서 그대로."""
    # given — 트레이스 둘, 각각 매치 span 둘
    body = _search(("t1", [_hit_span("a", 10, span_id="x1", start_ns=1_000_000_000), _hit_span("b", 20, status="error", service="svc2", pod="p2", span_id="x2", start_ns=1_005_000_000)]),
                   ("t2", [_hit_span("c", 30, span_id="y1", start_ns=2_000_000_000), _hit_span("d", 40, span_id="y2", start_ns=2_010_000_000)]))
    # when
    matches = traces.parse_span_matches(body)
    # then
    assert matches == [
        SpanMatch(trace_id="t1", span_id="x1", name="a", start_ns=1_000_000_000, duration_ns=10 * MS, status="unset", service="svc", pod="p"),
        SpanMatch(trace_id="t1", span_id="x2", name="b", start_ns=1_005_000_000, duration_ns=20 * MS, status="error", service="svc2", pod="p2"),
        SpanMatch(trace_id="t2", span_id="y1", name="c", start_ns=2_000_000_000, duration_ns=30 * MS, status="unset", service="svc", pod="p"),
        SpanMatch(trace_id="t2", span_id="y2", name="d", start_ns=2_010_000_000, duration_ns=40 * MS, status="unset", service="svc", pod="p"),
    ]


def test_parse_span_matches_missing_attributes_are_empty_strings() -> None:
    """service · pod · status 속성이 없는 span 은 빠지지 않고 그 필드가 빈 문자열이다."""
    # given
    body = _search(("t1", [_hit_span("a", 10, status=None, service=None, pod=None)]))
    # when
    matches = traces.parse_span_matches(body)
    # then
    assert len(matches) == 1
    assert matches[0].service == ""
    assert matches[0].pod == ""
    assert matches[0].status == ""


def test_parse_span_matches_falls_back_to_span_set() -> None:
    """spanSets 가 없고 옛 spanSet 만 있어도 그 span 을 읽는다."""
    # given
    body = _search(("t1", [_hit_span("a", 10)]), legacy=True)
    # when
    matches = traces.parse_span_matches(body)
    # then
    assert [m.name for m in matches] == ["a"]


def test_parse_span_matches_skips_trace_without_span_sets() -> None:
    """spanSets 도 spanSet 도 없는 빈 껍데기 트레이스는 건너뛴다 — 매치는 다른 트레이스의 것뿐이고 예외는 없다."""
    # given — t1 은 매치 하나 · t2 는 spanSets · spanSet 둘 다 없다
    body = _search(("t1", [_hit_span("a", 10)]))
    hollow: JSON = {"traceID": "t2", "rootServiceName": "svc", "rootTraceName": "root", "startTimeUnixNano": "1000000000", "durationMs": 1}
    items = body["traces"]
    assert isinstance(items, list)
    items.append(hollow)
    # when
    matches = traces.parse_span_matches(body)
    # then
    assert [m.trace_id for m in matches] == ["t1"]


def test_parse_span_matches_empty_response_is_empty() -> None:
    """traces 가 비면 빈 목록이다."""
    # given · when
    matches = traces.parse_span_matches(_search())
    # then
    assert matches == []


# ── threshold_query · candidates_query ──────────────────────────────────────────────

def test_threshold_query_tags_baseline_and_window_in_one_query() -> None:
    """threshold_query 는 base(평소 — [baseline_range] offset 창 길이) 와 now(조사 창 — [창 길이]) 두 서버 분위수를 label_replace 로 한 질의에 담고 서비스 필터가 없다."""
    # given · when
    q = traces.threshold_query(0.95, W, "7d")
    # then
    base, now = q.split(" or ")
    assert base.startswith('label_replace(histogram_quantile(0.95, sum by (service_name, span_name) (rate(traces_span_metrics_duration_seconds{span_kind="SPAN_KIND_SERVER"}[7d] offset 3600s))), "stat", "base", "", "")')
    assert now == 'label_replace(histogram_quantile(0.95, sum by (service_name, span_name) (rate(traces_span_metrics_duration_seconds{span_kind="SPAN_KIND_SERVER"}[3600s]))), "stat", "now", "", "")'
    assert "service_name=" not in q


def test_candidates_query_appends_server_kind_and_select() -> None:
    """candidates_query 는 조건식에 `&& kind = server` 를 붙이고 이름 · status · 서비스 · 파드를 select 한다."""
    # given · when
    q = traces.candidates_query('resource.service.name = "a"')
    # then
    assert q == '{ resource.service.name = "a" && kind = server } | ' + SELECT


def test_threshold_query_carries_quantile_literal() -> None:
    """분위수는 리터럴로 질의에 들어간다 — 0.999 는 histogram_quantile(0.999, …) 다."""
    # given · when
    q = traces.threshold_query(0.999, W, "7d")
    # then
    assert q.count("histogram_quantile(0.999, ") == 2


def test_candidates_query_without_filter_is_all_server_spans() -> None:
    """빈 조건식(공백뿐)은 서버 span 전부다."""
    # given · when
    q = traces.candidates_query("  ")
    # then
    assert q == "{ kind = server } | " + SELECT


def test_candidates_query_strips_surrounding_braces() -> None:
    """중괄호째 온 조건식은 벗겨서 쓴다 — 중괄호가 겹치지 않는다."""
    # given · when
    q = traces.candidates_query('{ resource.service.name = "a" }')
    # then
    assert q == '{ resource.service.name = "a" && kind = server } | ' + SELECT


@pytest.mark.parametrize("quantile", [0.0, 1.0, 1.5], ids=["zero", "one", "above"])
def test_threshold_query_rejects_quantile_outside_unit_interval(quantile: float) -> None:
    """분위수가 (0, 1) 밖이면 ObsError 다."""
    # given · when · then
    with pytest.raises(ObsError):
        traces.threshold_query(quantile, W, "7d")


def test_threshold_query_rejects_bad_baseline_range() -> None:
    """baseline_range 가 기간 표기가 아니면 ObsError 다."""
    # given · when · then
    with pytest.raises(ObsError):
        traces.threshold_query(0.95, W, "week")


# ── parse_quantiles ─────────────────────────────────────────────────────────────────

def test_parse_quantiles_folds_base_and_now_per_endpoint() -> None:
    """(a, x) 의 base · now 두 시계열은 EndpointQuantiles 하나로 접힌다."""
    # given
    res = parse_metric(_vector(_stat_series("a", "x", "base", "0.01"), _stat_series("a", "x", "now", "0.04")))
    # when
    out = traces.parse_quantiles(res)
    # then
    assert out == [EndpointQuantiles(endpoint=Endpoint("a", "x"), base=0.01, now=0.04)]


def test_parse_quantiles_missing_stat_is_none() -> None:
    """now 만 있는 엔드포인트는 base 가 None 이다."""
    # given
    res = parse_metric(_vector(_stat_series("a", "x", "now", "0.04")))
    # when
    out = traces.parse_quantiles(res)
    # then
    assert out[0].base is None
    assert out[0].now == 0.04


def test_parse_quantiles_non_finite_is_none() -> None:
    """NaN · +Inf 는 값이 아니라 부재다 — 그 자리는 None."""
    # given
    res = parse_metric(_vector(_stat_series("a", "x", "base", "NaN"), _stat_series("a", "x", "now", "0.04"), _stat_series("b", "y", "now", "+Inf")))
    # when
    out = {q.endpoint: q for q in traces.parse_quantiles(res)}
    # then
    assert out[Endpoint("a", "x")].base is None
    assert out[Endpoint("b", "y")].now is None


# ── thresholds_of ───────────────────────────────────────────────────────────────────

def test_thresholds_prefer_baseline_p95() -> None:
    """base 와 now 가 둘 다 있으면 임계는 base 이고 출처는 baseline 이다."""
    # given
    quantiles = [_eq("a", "x", base=0.01, now=0.04)]
    sample = [_sm("t1", "x", 50, service="a")]
    # when
    out = traces.thresholds_of(quantiles, sample)
    # then
    assert out[Endpoint("a", "x")].seconds == 0.01
    assert out[Endpoint("a", "x")].source == THRESHOLD_BASELINE


def test_thresholds_fall_back_to_window_p95() -> None:
    """base 가 없으면 조사 창 p95(now) 이고 출처는 window 다."""
    # given
    quantiles = [_eq("a", "x", base=None, now=0.04)]
    sample = [_sm("t1", "x", 50, service="a")]
    # when
    out = traces.thresholds_of(quantiles, sample)
    # then
    assert out[Endpoint("a", "x")].seconds == 0.04
    assert out[Endpoint("a", "x")].source == THRESHOLD_WINDOW


def test_thresholds_fall_back_to_sample_p95() -> None:
    """metrics 에 없는 엔드포인트는 표본의 p95(nearest-rank — 다섯이면 가장 큰 값) 이고 출처는 sample 이다."""
    # given — 그 엔드포인트의 건 다섯, 지속시간 제각각
    sample = [_sm("t%d" % i, "x", ms, service="a") for i, ms in enumerate([30, 10, 50, 20, 40])]
    # when
    out = traces.thresholds_of([], sample)
    # then
    assert out[Endpoint("a", "x")].seconds == pytest.approx(0.050)
    assert out[Endpoint("a", "x")].source == THRESHOLD_SAMPLE


def test_thresholds_sample_fallback_follows_quantile() -> None:
    """quantile 0.5 면 표본 폴백은 중앙값(nearest-rank — 다섯이면 세 번째) 이고 출처는 sample 이다 — 표본 폴백도 quantile 을 따른다."""
    # given — 1 · 2 · 3 · 4 · 5ms
    sample = [_sm("t%d" % i, "x", ms, service="a") for i, ms in enumerate([4, 1, 5, 2, 3])]
    # when
    out = traces.thresholds_of([], sample, quantile=0.5)
    # then
    assert out[Endpoint("a", "x")] == _th(0.003, THRESHOLD_SAMPLE)


def test_thresholds_single_sample_is_its_own_p95() -> None:
    """표본에만 있고 건이 하나면 임계는 그 건의 지속시간이다."""
    # given
    sample = [_sm("t1", "x", 12, service="a")]
    # when
    out = traces.thresholds_of([], sample)
    # then
    assert out[Endpoint("a", "x")].seconds == pytest.approx(0.012)
    assert out[Endpoint("a", "x")].source == THRESHOLD_SAMPLE


def test_thresholds_cover_exactly_the_sample_endpoints() -> None:
    """키 집합은 표본의 Endpoint 집합이다 — metrics 에만 있는 엔드포인트는 없고 표본의 엔드포인트는 전부 있다."""
    # given — metrics 에만 (a, only-in-metrics) · 표본에만 (b, y) · 둘 다 (a, x)
    quantiles = [_eq("a", "x", 0.01, 0.02), _eq("a", "only-in-metrics", 0.01, 0.02)]
    sample = [_sm("t1", "x", 5, service="a"), _sm("t2", "y", 5, service="b")]
    # when
    out = traces.thresholds_of(quantiles, sample)
    # then
    assert set(out) == {Endpoint("a", "x"), Endpoint("b", "y")}


@pytest.mark.parametrize("quantiles,expected", [(_eq("a", "x", base=0.0, now=0.02), _th(0.02, THRESHOLD_WINDOW)),
                                                (_eq("a", "x", base=0.0, now=0.0), _th(0.005, THRESHOLD_SAMPLE))], ids=["base_zero", "both_zero"])
def test_thresholds_skip_non_positive_quantile_values(quantiles: EndpointQuantiles, expected: Threshold) -> None:
    """base 가 0 이면 now 로, now 도 0 이면 표본 p95 로 — 0 이하인 출처는 없는 것으로 보고 다음 폴백으로 간다(0 으로 나눌 임계를 만들지 않는다)."""
    # given — 그 엔드포인트의 건 셋, 전부 5ms
    sample = [_sm("t%d" % i, "x", 5, service="a") for i in range(3)]
    # when
    out = traces.thresholds_of([quantiles], sample)
    # then
    assert out[Endpoint("a", "x")] == expected


def test_thresholds_reject_duplicate_endpoint_quantiles() -> None:
    """같은 엔드포인트의 EndpointQuantiles 가 둘이면 ObsError 이고 문구에 그 엔드포인트가 있다 — 뒤의 것이 조용히 이기지 않는다."""
    # given
    quantiles = [_eq("a", "dup-ep", 0.01, 0.02), _eq("a", "dup-ep", 0.02, 0.02)]
    sample = [_sm("t1", "dup-ep", 5, service="a")]
    # when · then
    with pytest.raises(ObsError) as ei:
        traces.thresholds_of(quantiles, sample)
    assert "dup-ep" in str(ei.value)


# ── exemplars_of ────────────────────────────────────────────────────────────────────

def test_exemplars_of_errors_go_to_error_bucket_regardless_of_duration() -> None:
    """status 가 error 인 건은 임계보다 빨라도 error 묶음이고 slow · normal 에는 없다."""
    # given
    sample = [_sm("t1", "x", 5, status="error", service="a")]
    th = {Endpoint("a", "x"): _th(0.010)}
    # when
    out = traces.exemplars_of(sample, th, k=3, quantile=0.95, truncated=False)
    # then
    assert _ids(out.error.items) == ["t1"]
    assert out.slow.items == []
    assert out.normal.items == []


def test_exemplars_of_splits_slow_and_normal_by_pair_threshold() -> None:
    """임계 10ms 에서 50ms 는 slow(임계 10 · 출처 baseline · 배율 5), 5ms 는 normal(같은 임계 · 배율 0.5) 이고 건의 모든 필드가 표본에서 옮겨진다."""
    # given
    sample = [_sm("t1", "x", 50, service="a", span_id="s1", pod="p1"), _sm("t2", "x", 5, service="a", span_id="s2", pod="p2")]
    th = {Endpoint("a", "x"): _th(0.010)}
    # when
    out = traces.exemplars_of(sample, th, k=3, quantile=0.95, truncated=False)
    # then
    assert out.slow.items == [Exemplar(trace_id="t1", span_id="s1", service="a", endpoint="x", pod="p1", duration_ms=50.0, start_ts=1.0,
                                       threshold_ms=10.0, threshold_source=THRESHOLD_BASELINE, ratio=5.0)]
    assert out.normal.items == [Exemplar(trace_id="t2", span_id="s2", service="a", endpoint="x", pod="p2", duration_ms=5.0, start_ts=1.0,
                                         threshold_ms=10.0, threshold_source=THRESHOLD_BASELINE, ratio=0.5)]
    assert out.error.items == []


def test_exemplars_of_slow_endpoint_normal_does_not_displace_fast_endpoint_slow() -> None:
    """임계가 열 배 다른 두 엔드포인트가 섞여도 느린 엔드포인트의 정상 건(90ms < 100ms)은 빠른 엔드포인트의 느린 건(50ms > 10ms)을 밀어내지 않는다."""
    # given — x 임계 10ms · y 임계 100ms
    sample = [_sm("fast-slow", "x", 50, service="a"), _sm("slow-normal", "y", 90, service="a")]
    th = {Endpoint("a", "x"): _th(0.010), Endpoint("a", "y"): _th(0.100)}
    # when
    out = traces.exemplars_of(sample, th, k=1, quantile=0.95, truncated=False)
    # then
    assert _ids(out.slow.items) == ["fast-slow"]
    assert _ids(out.normal.items) == ["slow-normal"]


def test_exemplars_of_slow_bucket_orders_by_ratio_then_trace_id_and_cuts_at_k() -> None:
    """slow 묶음은 배율 내림차순, 동률은 trace_id 오름차순이고 k 로 자르되 total 은 자르기 전 수다."""
    # given — 배율 5 · 3(b) · 3(a) · 2
    sample = [_sm("c", "x", 50, service="a"), _sm("b", "x", 30, service="a"), _sm("a", "x", 30, service="a"), _sm("d", "x", 20, service="a")]
    th = {Endpoint("a", "x"): _th(0.010)}
    # when
    out = traces.exemplars_of(sample, th, k=3, quantile=0.95, truncated=False)
    # then
    assert _ids(out.slow.items) == ["c", "a", "b"]
    assert out.slow.total == 4
    assert out.k == 3


def test_exemplars_of_error_and_normal_buckets_order_by_duration_then_trace_id() -> None:
    """error · normal 묶음은 지속시간 내림차순, 동률은 trace_id 오름차순이다."""
    # given
    errors = [_sm("e2", "x", 7, status="error", service="a"), _sm("e1", "x", 7, status="error", service="a"), _sm("e3", "x", 9, status="error", service="a")]
    normals = [_sm("n2", "x", 3, service="a"), _sm("n1", "x", 3, service="a"), _sm("n3", "x", 4, service="a")]
    th = {Endpoint("a", "x"): _th(0.010)}
    # when
    out = traces.exemplars_of([*errors, *normals], th, k=5, quantile=0.95, truncated=False)
    # then
    assert _ids(out.error.items) == ["e3", "e1", "e2"]
    assert _ids(out.normal.items) == ["n3", "n1", "n2"]


def test_exemplars_of_empty_bucket_is_zero_not_missing() -> None:
    """정상 건만 있으면 slow · error 는 total 0 · items [] 이고 sample 은 0 이 아니다 — 0 건이지 데이터 없음이 아니다."""
    # given
    sample = [_sm("t1", "x", 5, service="a")]
    th = {Endpoint("a", "x"): _th(0.010)}
    # when
    out = traces.exemplars_of(sample, th, k=3, quantile=0.95, truncated=False)
    # then
    assert out.sample == 1
    assert out.slow.total == 0
    assert out.slow.items == []
    assert out.error.total == 0
    assert out.error.items == []


def test_exemplars_of_empty_sample_is_no_data() -> None:
    """표본이 비면 sample 0 이고 세 묶음 total 이 전부 0 이다."""
    # given · when
    out = traces.exemplars_of([], {}, k=3, quantile=0.95, truncated=False)
    # then
    assert out.sample == 0
    assert (out.slow.total, out.error.total, out.normal.total) == (0, 0, 0)


def test_exemplars_of_duration_equal_to_threshold_is_normal() -> None:
    """지속시간이 임계와 같으면 normal 이다 — 초과만 느림이다."""
    # given
    sample = [_sm("t1", "x", 10, service="a")]
    th = {Endpoint("a", "x"): _th(0.010)}
    # when
    out = traces.exemplars_of(sample, th, k=3, quantile=0.95, truncated=False)
    # then
    assert _ids(out.normal.items) == ["t1"]
    assert out.slow.total == 0


def test_exemplars_of_rejects_sample_endpoint_without_threshold() -> None:
    """표본 건의 endpoint 가 thresholds 에 없으면 ObsError 다(thresholds_of 의 결과가 아닌 입력) — 조용히 normal 로 삼키지 않는다."""
    # given
    sample = [_sm("t1", "orphan", 10, service="a")]
    # when · then
    with pytest.raises(ObsError) as ei:
        traces.exemplars_of(sample, {}, k=3, quantile=0.95, truncated=False)
    assert "orphan" in str(ei.value)


def test_exemplars_of_rejects_non_positive_threshold() -> None:
    """그 엔드포인트의 Threshold.seconds 가 0 이면 ObsError 이고 문구에 그 엔드포인트가 있다 — ZeroDivisionError 가 아니다."""
    # given — 표본 전부 0ns 라 표본 p95 도 0 인 경우
    sample = [_sm("t1", "zero-ep", 0, service="a")]
    thresholds = {Endpoint("a", "zero-ep"): _th(0.0, THRESHOLD_SAMPLE)}
    # when · then
    with pytest.raises(ObsError) as ei:
        traces.exemplars_of(sample, thresholds, k=3, quantile=0.95, truncated=False)
    assert "zero-ep" in str(ei.value)


def test_exemplars_of_rejects_k_below_one() -> None:
    """k 가 0 이면 ObsError 다."""
    # given · when · then
    with pytest.raises(ObsError):
        traces.exemplars_of([], {}, k=0, quantile=0.95, truncated=False)


# ── exemplars — metrics 임계 → traces 후보 GET 2 회 ──────────────────────────────────

def _exemplar_routes(metrics_body: dict[str, JSON] | None = None, search_body: dict[str, JSON] | None = None) -> dict[tuple[str, str], tuple[int, object]]:
    return {("metrics", INSTANT): (200, metrics_body if metrics_body is not None else _vector()),
            ("traces", SEARCH): (200, search_body if search_body is not None else _search())}


def test_exemplars_sends_threshold_then_candidates(make_client: ClientFactory) -> None:
    """호출은 2 회 — metrics 즉시 조회(threshold_query · time = 창 끝) 뒤 traces 검색(candidates_query · 정수 초 창 · limit + 1) 순이다."""
    # given
    client, t = make_client(_exemplar_routes())
    # when
    traces.exemplars(client, 'resource.service.name = "a"', W, limit=100)
    # then
    assert len(t.calls) == 2
    assert _path(t.calls[0]).endswith(INSTANT)
    assert _params(t.calls[0])["time"] == str(W.end)
    assert _path(t.calls[1]).endswith(SEARCH)
    assert _params(t.calls[1])["start"] == str(int(W.start))
    assert _params(t.calls[1])["end"] == str(int(W.end))
    assert _params(t.calls[1])["limit"] == "101"


def test_exemplars_classifies_with_server_thresholds(make_client: ClientFactory) -> None:
    """metrics 응답의 base 10ms 로 검색의 50ms 건이 slow 가 되고 출처는 baseline, value 의 k · quantile 은 인자다."""
    # given
    client, _ = make_client(_exemplar_routes(_vector(_stat_series("a", "x", "base", "0.010")), _search(("t1", [_hit_span("x", 50, service="a")]))))
    # when
    ev = traces.exemplars(client, "", W, k=2, quantile=0.9)
    # then
    assert _ids(ev.value.slow.items) == ["t1"]
    assert ev.value.slow.items[0].threshold_source == THRESHOLD_BASELINE
    assert ev.value.k == 2
    assert ev.value.quantile == 0.9


@pytest.mark.parametrize("returned,truncated,sample", [(3, True, 2), (2, False, 2)], ids=["over", "at_limit"])
def test_exemplars_marks_truncated_only_when_more_than_limit_came_back(make_client: ClientFactory, returned: int, truncated: bool, sample: int) -> None:
    """limit + 1 개를 물어 limit 을 넘겨 돌아오면 truncated 이고 표본은 앞 limit 개 트레이스의 건만, 정확히 limit 개면 truncated 가 아니고 전부가 표본이다."""
    # given — 트레이스마다 건 하나
    hits = [("t%d" % i, [_hit_span("x", 5, service="a")]) for i in range(returned)]
    client, _ = make_client(_exemplar_routes(search_body=_search(*hits)))
    # when
    ev = traces.exemplars(client, "", W, limit=2)
    # then
    assert ev.value.truncated is truncated
    assert ev.value.sample == sample
    assert _ids(ev.value.normal.items) == ["t%d" % i for i in range(sample)]


def test_exemplars_defaults_baseline_range_to_seven_days(make_client: ClientFactory) -> None:
    """baseline_range 를 주지 않으면 metrics 질의의 평소 구간은 7d 이고 offset 은 창 길이다."""
    # given
    client, t = make_client(_exemplar_routes())
    # when
    traces.exemplars(client, "", W)
    # then
    assert "[7d] offset 3600s" in _params(t.calls[0])["query"]


@pytest.mark.parametrize("k,limit,quantile,baseline_range", [(0, 100, 0.95, "7d"), (3, 0, 0.95, "7d"), (3, 100, 1.0, "7d"), (3, 100, 0.95, "week")],
                         ids=["k", "limit", "quantile", "baseline_range"])
def test_exemplars_rejects_bad_arguments_before_any_request(make_client: ClientFactory, k: int, limit: int, quantile: float, baseline_range: str) -> None:
    """k · limit 이 1 미만, 분위수가 (0, 1) 밖, baseline_range 가 기간 표기가 아니면 요청 없이 ObsError 다."""
    # given
    client, t = make_client(_exemplar_routes())
    # when · then
    with pytest.raises(ObsError):
        traces.exemplars(client, "", W, k=k, limit=limit, quantile=quantile, baseline_range=baseline_range)
    assert t.calls == []


def test_exemplars_stops_on_metrics_query_error(make_client: ClientFactory) -> None:
    """metrics 응답이 status: error 면 ObsError(문구에 metrics) 이고 traces 검색은 나가지 않는다."""
    # given
    client, t = make_client(_exemplar_routes(metrics_body={"status": "error", "errorType": "bad_data", "error": "boom"}))
    # when · then
    with pytest.raises(ObsError) as ei:
        traces.exemplars(client, "", W)
    assert "metrics" in str(ei.value)
    assert len(t.calls) == 1


# ── critical_path_of — CRISP ────────────────────────────────────────────────────────

def _cp(spans: list[Span], trace_id: str = "t") -> CriticalPath:
    return traces.critical_path_of(Trace(trace_id, spans))


def _names(path: list[PathSpan]) -> list[str]:
    return [p.name for p in path]


def test_critical_path_bottleneck_is_the_grandchild_with_largest_self_time() -> None:
    """루트 800 · 자식 790 · 손자 780 이면 경로는 셋(깊이 0 · 1 · 2, 시간 순) · self 는 10 · 10 · 780 · 병목 순서는 손자 · 루트 · 자식(동률은 offset 순) 이다."""
    # given
    spans = [_span("root", "r", start_ms=0, end_ms=800), _span("child", "c", parent="r", start_ms=5, end_ms=795), _span("grand", "g", parent="c", start_ms=10, end_ms=790)]
    # when
    cp = _cp(spans)
    # then
    assert _names(cp.path) == ["root", "child", "grand"]
    assert [p.depth for p in cp.path] == [0, 1, 2]
    assert [p.self_ms for p in cp.path] == pytest.approx([10.0, 10.0, 780.0])
    assert _names(cp.bottlenecks) == ["grand", "root", "child"]
    assert cp.path[2].offset_ms == pytest.approx(10.0)
    assert cp.total_ms == pytest.approx(800.0)
    assert cp.spans == 3


def test_critical_path_includes_sequential_children_in_time_order() -> None:
    """순차 자식 A(0–300) · B(300–800) 는 둘 다 경로에 시간 순으로 오르고 루트 self 는 0 이다."""
    # given
    spans = [_span("root", "r", start_ms=0, end_ms=800), _span("A", "a", parent="r", start_ms=0, end_ms=300), _span("B", "b", parent="r", start_ms=300, end_ms=800)]
    # when
    cp = _cp(spans)
    # then
    assert _names(cp.path) == ["root", "A", "B"]
    assert cp.path[0].self_ms == pytest.approx(0.0)


def test_critical_path_ignores_siblings_overlapping_the_last_finishing_child() -> None:
    """A(0–500) 와 B(100–800) 가 겹치면 LFC 인 B 만 경로이고 A 는 없으며 루트 self 는 100 이다."""
    # given
    spans = [_span("root", "r", start_ms=0, end_ms=800), _span("A", "a", parent="r", start_ms=0, end_ms=500), _span("B", "b", parent="r", start_ms=100, end_ms=800)]
    # when
    cp = _cp(spans)
    # then
    assert _names(cp.path) == ["root", "B"]
    assert cp.path[0].self_ms == pytest.approx(100.0)


def test_critical_path_clips_fire_and_forget_child_at_parent_end() -> None:
    """부모(0–800)보다 늦게 끝나는 자식(700–1200)은 부모 끝에서 잘려 길이 100 · 넘친 길이 clipped_ms 400 이고 루트는 0 · self 700 이다."""
    # given
    spans = [_span("root", "r", start_ms=0, end_ms=800), _span("async", "a", parent="r", start_ms=700, end_ms=1200)]
    # when
    cp = _cp(spans)
    # then
    assert _names(cp.path) == ["root", "async"]
    assert cp.path[1].duration_ms == pytest.approx(100.0)
    assert cp.path[1].clipped_ms == pytest.approx(400.0)
    assert cp.path[0].clipped_ms == 0.0
    assert cp.path[0].self_ms == pytest.approx(700.0)


def test_critical_path_clips_child_starting_before_parent_at_parent_start() -> None:
    """부모(0–800)보다 먼저 시작한 자식(−200–500)은 부모 시작에서 잘려 offset 0 · 길이 500 · clipped_ms 200 이고 루트 self 는 300 이다 — 시작 쪽도 부모 경계로 자른다."""
    # given
    spans = [_span("root", "r", start_ms=0, end_ms=800), _span("early", "e", parent="r", start_ms=-200, end_ms=500)]
    # when
    cp = _cp(spans)
    # then
    assert _names(cp.path) == ["root", "early"]
    assert cp.path[1].offset_ms == pytest.approx(0.0)
    assert cp.path[1].duration_ms == pytest.approx(500.0)
    assert cp.path[1].clipped_ms == pytest.approx(200.0)
    assert cp.path[0].self_ms == pytest.approx(300.0)


def test_critical_path_tolerates_one_ms_clock_drift() -> None:
    """A 가 B 시작 뒤 0.5ms 에 끝나도(드리프트) A 는 경로에 있다 — happensBefore 를 1ms 로 완화한다."""
    # given
    spans = [_span("root", "r", start_ms=0, end_ms=800), _span("A", "a", parent="r", start_ms=0, end_ms=500.5), _span("B", "b", parent="r", start_ms=500, end_ms=800)]
    # when
    cp = _cp(spans)
    # then
    assert _names(cp.path) == ["root", "A", "B"]


def test_critical_path_drops_subtrees_entirely_outside_the_parent() -> None:
    """부모(0–800) 밖에서 통째로 도는 자식(900–1000)과 그 손자는 경로에 없고 dropped 가 1 이다."""
    # given
    spans = [_span("root", "r", start_ms=0, end_ms=800), _span("late", "l", parent="r", start_ms=900, end_ms=1000), _span("late-child", "lc", parent="l", start_ms=910, end_ms=990)]
    # when
    cp = _cp(spans)
    # then
    assert _names(cp.path) == ["root"]
    assert cp.dropped == 1


def test_critical_path_of_empty_trace_has_no_path() -> None:
    """span 이 없으면 spans 0 · path [] · bottlenecks [] 이다."""
    # given · when
    cp = _cp([])
    # then
    assert cp.spans == 0
    assert cp.path == []
    assert cp.bottlenecks == []


def test_critical_path_of_single_span_is_itself() -> None:
    """자식 없는 루트 하나면 경로는 그것뿐이고 self 가 길이와 같고 clipped_ms 는 0 이며 roots 는 1 이다."""
    # given · when
    cp = _cp([_span("root", "r", start_ms=0, end_ms=42)])
    # then
    assert _names(cp.path) == ["root"]
    assert cp.path[0].self_ms == pytest.approx(42.0)
    assert cp.path[0].clipped_ms == 0.0
    assert cp.roots == 1


def test_critical_path_picks_the_earliest_root_when_there_are_several() -> None:
    """parent 가 빈 span 이 둘이면(루트가 둘인 불완전한 트레이스) 먼저 시작한 것이 루트이고 roots 2 · orphans 0 이다 — 거부하지 않고 표시한다."""
    # given
    spans = [_span("late-root", "b", start_ms=50, end_ms=100), _span("early-root", "a", start_ms=0, end_ms=80)]
    # when
    cp = _cp(spans)
    # then
    assert cp.path[0].name == "early-root"
    assert cp.roots == 2
    assert cp.orphans == 0


def test_critical_path_rejects_trace_without_root() -> None:
    """span 둘이 서로를 부모로 가리키면(순환 — 루트 0 · 고아 0 인데 span 이 있다) ObsError 이고 문구에 trace_id 가 있다 — ValueError 가 아니다."""
    # given
    spans = [_span("a", "x", parent="y", start_ms=0, end_ms=10), _span("b", "y", parent="x", start_ms=0, end_ms=10)]
    # when · then
    with pytest.raises(ObsError) as ei:
        _cp(spans, trace_id="cyc-1")
    assert "cyc-1" in str(ei.value)


def test_critical_path_walks_from_the_root_not_from_an_earlier_orphan() -> None:
    """루트(parent 빔, 100–800) 와 그보다 먼저 시작한 고아(parent 가 없는 id, 0–50) 가 있으면 경로는 루트에서 시작하고 고아는 경로에 없으며 roots 1 · orphans 1 이다."""
    # given
    spans = [_span("orphan", "o", parent="missing", start_ms=0, end_ms=50), _span("root", "r", start_ms=100, end_ms=800)]
    # when
    cp = _cp(spans)
    # then
    assert _names(cp.path) == ["root"]
    assert cp.roots == 1
    assert cp.orphans == 1


def test_critical_path_without_root_has_no_path_and_counts_orphans() -> None:
    """parent 가 빈 span 이 없고 고아만 둘이면(루트 유실) path · bottlenecks 가 비고 roots 0 · orphans 2 다 — 예외가 아니고 고아를 루트로 승격하지 않는다."""
    # given
    spans = [_span("a", "x", parent="m1", start_ms=0, end_ms=10), _span("b", "y", parent="m2", start_ms=5, end_ms=20)]
    # when
    cp = _cp(spans)
    # then
    assert cp.path == []
    assert cp.bottlenecks == []
    assert cp.roots == 0
    assert cp.orphans == 2


# ── critical_path — GET 1 회 ───────────────────────────────────────────────────────

def test_critical_path_fetches_the_trace_once(make_client: ClientFactory) -> None:
    """api/traces/<id> 를 한 번 부르고 value 는 그 트레이스의 경로다."""
    # given
    body = _otlp([_span("root", "r", start_ms=0, end_ms=10), _span("child", "c", parent="r", start_ms=1, end_ms=9)])
    client, t = make_client({("traces", _trace_path("abc")): (200, body)})
    # when
    ev = traces.critical_path(client, "abc", W)
    # then
    assert len(t.calls) == 1
    assert _path(t.calls[0]).endswith(_trace_path("abc"))
    assert ev.value.trace_id == "abc"
    assert _names(ev.value.path) == ["root", "child"]


def test_critical_path_rejects_blank_trace_id(make_client: ClientFactory) -> None:
    """traceID 가 공백뿐이면 요청 없이 ObsError 다."""
    # given
    client, t = make_client({})
    # when · then
    with pytest.raises(ObsError):
        traces.critical_path(client, " ", W)
    assert t.calls == []


# ── edge_query ─────────────────────────────────────────────────────────────────────

def test_edge_query_tags_calls_errors_now_and_base_for_client_spans() -> None:
    """edge_query 는 CLIENT 행의 calls · errors(ERROR 만) · now([창]) · base([baseline_range] offset 창) 넷을 label_replace 로 한 질의에 담고 서비스 필터가 없다."""
    # given · when
    q = traces.edge_query(0.95, W, "7d")
    # then
    parts = q.split(" or ")
    assert len(parts) == 4
    assert parts[0] == 'label_replace(sum by (service_name, span_name) (increase(traces_span_metrics_calls_total{span_kind="SPAN_KIND_CLIENT"}[3600s])), "stat", "calls", "", "")'
    assert parts[1] == 'label_replace(sum by (service_name, span_name) (increase(traces_span_metrics_calls_total{span_kind="SPAN_KIND_CLIENT", status_code="STATUS_CODE_ERROR"}[3600s])), "stat", "errors", "", "")'
    assert parts[2] == 'label_replace(histogram_quantile(0.95, sum by (service_name, span_name) (rate(traces_span_metrics_duration_seconds{span_kind="SPAN_KIND_CLIENT"}[3600s]))), "stat", "now", "", "")'
    assert parts[3] == 'label_replace(histogram_quantile(0.95, sum by (service_name, span_name) (rate(traces_span_metrics_duration_seconds{span_kind="SPAN_KIND_CLIENT"}[7d] offset 3600s))), "stat", "base", "", "")'
    assert "service_name=" not in q


@pytest.mark.parametrize("quantile,baseline_range", [(1.0, "7d"), (0.95, "week")], ids=["quantile", "baseline_range"])
def test_edge_query_rejects_bad_arguments(quantile: float, baseline_range: str) -> None:
    """분위수가 (0, 1) 밖이거나 baseline_range 가 기간 표기가 아니면 ObsError 다."""
    # given · when · then
    with pytest.raises(ObsError):
        traces.edge_query(quantile, W, baseline_range)


# ── parse_edges ────────────────────────────────────────────────────────────────────

def test_parse_edges_folds_four_stats_per_caller_endpoint_and_rounds_counts() -> None:
    """(a, X) 의 calls · errors · now · base 네 시계열은 ClientRow 하나로 접히고 increase 의 소수는 정수로 반올림된다."""
    # given
    res = parse_metric(_vector(_stat_series("a", "X", "calls", "10.0003"), _stat_series("a", "X", "errors", "1.00004"),
                               _stat_series("a", "X", "now", "0.04"), _stat_series("a", "X", "base", "0.02")))
    # when
    rows = traces.parse_edges(res)
    # then
    assert len(rows) == 1
    assert rows[0] == ClientRow(caller="a", endpoint="X", calls=10, errors=1, now=0.04, base=0.02)


def test_parse_edges_missing_stats_default_to_zero_and_none() -> None:
    """calls 만 있는 행은 errors 0 · now None · base None 으로 남는다."""
    # given
    res = parse_metric(_vector(_stat_series("a", "X", "calls", "3")))
    # when
    rows = traces.parse_edges(res)
    # then
    assert rows == [ClientRow(caller="a", endpoint="X", calls=3, errors=0, now=None, base=None)]


def test_parse_edges_drops_rows_without_calls() -> None:
    """calls 시계열이 없는 (a, X) 는 행이 아니다 — 건수가 없으면 엣지가 아니다."""
    # given
    res = parse_metric(_vector(_stat_series("a", "X", "now", "0.04")))
    # when
    rows = traces.parse_edges(res)
    # then
    assert rows == []


# ── service_graph_of — 조인 · 배율 · 정렬 · 데이터 없음 vs 호출 0 ──────────────────────

def _sg(clients: list[ClientRow], servers: list[EndpointQuantiles]) -> ServiceGraph:
    return traces.service_graph_of(clients, servers, quantile=0.95, baseline_range="7d")


def test_service_graph_joins_client_row_with_server_row_by_span_name() -> None:
    """클라이언트 행(api-composer, X)은 같은 이름의 서버 행(posting-service, X)과 조인되어 callee · 건수 · 오류율 · 클라이언트/평소/서버 p95 · 배율이 있는 엣지가 된다."""
    # given · when
    g = _sg([_row("api-composer", "X", calls=10, errors=1, now=0.04, base=0.02)], [_eq("posting-service", "X", base=0.025, now=0.03)])
    # then
    assert g.edges == [Edge(caller="api-composer", callee="posting-service", endpoint="X", count=10, error_rate=0.1, client_p95_ms=40.0,
                            client_base_p95_ms=20.0, ratio=2.0, server_p95_ms=30.0)]
    assert g.idle == 0


def test_service_graph_splits_edges_per_endpoint_not_per_service_pair() -> None:
    """같은 caller · 같은 callee 라도 엔드포인트가 둘이면 엣지가 둘이고 각각 자기 p95 · 건수다 — 서비스 쌍으로 평균화하지 않는다."""
    # given
    g = _sg([_row("a", "X", calls=10, now=0.04, base=0.02), _row("a", "Y", calls=2, now=0.4, base=0.1)],
            [_eq("b", "X", 0.03, 0.03), _eq("b", "Y", 0.3, 0.3)])
    # when
    by_ep = {e.endpoint: e for e in g.edges}
    # then
    assert len(g.edges) == 2
    assert by_ep["X"].client_p95_ms == pytest.approx(40.0)
    assert by_ep["Y"].client_p95_ms == pytest.approx(400.0)
    assert by_ep["X"].count == 10
    assert by_ep["Y"].count == 2


def test_service_graph_orders_by_ratio_then_count_then_name() -> None:
    """엣지는 배율 내림차순, 동률은 건수 내림차순, 그래도 동률이면 (caller, callee, endpoint) 사전순, base 없는 것(ratio None)은 마지막이다."""
    # given — 배율 3 · 1.5(건수 20) · 1.5(건수 5) 셋(caller b · endpoint half-few2 · half-few) · base 없음 — 입력 순서는 기대와 다르다
    g = _sg([_row("a", "slow3", calls=1, now=0.03, base=0.01), _row("b", "half-few", calls=5, now=0.015, base=0.01),
             _row("a", "half-few2", calls=5, now=0.015, base=0.01), _row("a", "half-many", calls=20, now=0.015, base=0.01),
             _row("a", "half-few", calls=5, now=0.015, base=0.01), _row("a", "no-base", calls=99, now=0.5, base=None)], [])
    # when · then
    assert [(e.caller, e.endpoint) for e in g.edges] == [("a", "slow3"), ("a", "half-many"), ("a", "half-few"), ("a", "half-few2"), ("b", "half-few"),
                                                         ("a", "no-base")]


def test_service_graph_joins_callee_from_baseline_server_row_when_window_has_none() -> None:
    """조사 창에 서버 행이 없고 평소(base)에만 있으면 callee 는 그 서비스이고 server_p95_ms 는 None 이다."""
    # given
    g = _sg([_row("a", "X", calls=1, now=0.04, base=0.02)], [_eq("b", "X", base=0.03, now=None)])
    # when · then
    assert g.edges[0].callee == "b"
    assert g.edges[0].server_p95_ms is None


def test_service_graph_marks_callee_unknown_without_server_row() -> None:
    """같은 이름의 서버 행이 전혀 없으면 엣지는 남되 callee 가 None(미확인) 이다."""
    # given
    g = _sg([_row("a", "X", calls=1, now=0.04, base=0.02)], [])
    # when · then
    assert len(g.edges) == 1
    assert g.edges[0].callee is None
    assert g.edges[0].server_p95_ms is None


def test_service_graph_marks_callee_unknown_when_span_name_is_ambiguous() -> None:
    """같은 이름의 서버 행이 서비스 둘에 있으면 callee 는 None 이다 — 짐작하지 않는다."""
    # given
    g = _sg([_row("a", "X", calls=1, now=0.04, base=0.02)], [_eq("b", "X", 0.03, 0.03), _eq("c", "X", 0.03, 0.03)])
    # when · then
    assert g.edges[0].callee is None


def test_service_graph_rejects_duplicate_server_quantiles() -> None:
    """servers 에 같은 엔드포인트(서비스까지 같은) 의 EndpointQuantiles 가 둘이면 ObsError 이고 문구에 그 엔드포인트가 있다 — 첫 것을 조용히 쓰지 않는다."""
    # given
    servers = [_eq("b", "dup-X", 0.03, 0.03), _eq("b", "dup-X", 0.05, 0.05)]
    # when · then
    with pytest.raises(ObsError) as ei:
        _sg([_row("a", "dup-X", calls=1, now=0.04, base=0.02)], servers)
    assert "dup-X" in str(ei.value)


def test_service_graph_of_no_client_rows_is_no_data() -> None:
    """클라이언트 행이 없으면 edges [] · idle 0 — 데이터 없음이다."""
    # given · when
    g = _sg([], [_eq("b", "X", 0.03, 0.03)])
    # then
    assert g.edges == []
    assert g.idle == 0


def test_service_graph_counts_idle_rows_instead_of_dropping_them_silently() -> None:
    """클라이언트 행 셋의 calls 가 전부 0 이면 edges [] 이되 idle 3 — 0 건이지 데이터 없음이 아니다."""
    # given · when
    g = _sg([_row("a", "X", 0), _row("a", "Y", 0), _row("a", "Z", 0)], [])
    # then
    assert g.edges == []
    assert g.idle == 3


@pytest.mark.parametrize("row,field,expected", [(_row("a", "X", calls=10, errors=0, now=0.04, base=0.02), "error_rate", 0.0),
                                                 (_row("a", "X", calls=10, now=None, base=0.02), "client_p95_ms", None),
                                                 (_row("a", "X", calls=10, now=None, base=0.02), "ratio", None)],
                         ids=["no_errors", "no_now", "no_now_ratio"])
def test_service_graph_rates_without_division_by_missing_values(row: ClientRow, field: str, expected: float | None) -> None:
    """오류 0 이면 error_rate 0.0 이고, now 가 없으면 client_p95_ms · ratio 가 None 이다 — 없는 값으로 나누지 않는다."""
    # given · when
    g = _sg([row], [])
    # then
    assert getattr(g.edges[0], field) == expected


def test_service_graph_ratio_is_none_when_baseline_is_zero() -> None:
    """base 가 0.0 이면 ratio 는 None 이고 client_base_p95_ms 는 0.0 이다 — 0 으로 나누지 않고 평소 없음과 같이 다룬다."""
    # given · when
    g = _sg([_row("a", "X", calls=10, now=0.04, base=0.0)], [])
    # then
    assert g.edges[0].ratio is None
    assert g.edges[0].client_base_p95_ms == 0.0


# ── service_graph — metrics GET 2 회 ────────────────────────────────────────────────

def test_service_graph_sends_edge_then_server_queries_to_metrics_only(make_client: ClientFactory) -> None:
    """호출 2 회 전부 metrics 즉시 조회(time = 창 끝) — 첫 질의가 edge_query · 둘째가 threshold_query 순이다. traces 호출은 없다."""
    # given
    client, t = make_client({("metrics", INSTANT): (200, _vector(_stat_series("a", "X", "calls", "3")))})
    # when
    traces.service_graph(client, W)
    # then
    assert len(t.calls) == 2
    assert all(_path(c).endswith(INSTANT) for c in t.calls)
    assert {_params(c)["time"] for c in t.calls} == {str(W.end)}


def test_service_graph_sends_edge_then_server_queries(make_client: ClientFactory) -> None:
    """요청은 [edge, server] 순이고 value 의 quantile · baseline_range 는 인자다."""
    # given
    client, t = make_client({("metrics", INSTANT): (200, _vector())})
    # when
    ev = traces.service_graph(client, W, quantile=0.9, baseline_range="3d")
    # then
    assert [_params(c)["query"] for c in t.calls] == [traces.edge_query(0.9, W, "3d"), traces.threshold_query(0.9, W, "3d")]
    assert ev.value.quantile == 0.9
    assert ev.value.baseline_range == "3d"


@pytest.mark.parametrize("quantile,baseline_range", [(1.0, "7d"), (0.95, "week")], ids=["quantile", "baseline_range"])
def test_service_graph_rejects_bad_arguments_before_any_request(make_client: ClientFactory, quantile: float, baseline_range: str) -> None:
    """분위수 · baseline_range 가 틀리면 요청 없이 ObsError 다."""
    # given
    client, t = make_client({})
    # when · then
    with pytest.raises(ObsError):
        traces.service_graph(client, W, quantile=quantile, baseline_range=baseline_range)
    assert t.calls == []


# ── diff_of ────────────────────────────────────────────────────────────────────────

def _pair(slow: list[Span], normal: list[Span]) -> TraceDiff:
    return traces.diff_of(Trace("slow", slow), Trace("normal", normal))


def test_diff_pairs_spans_by_name_and_reports_time_difference() -> None:
    """같은 루트의 두 트레이스는 (서비스, 이름) 으로 짝지어 delta = slow − normal 만 내고, 행은 name(그다음 service) 오름차순이며 root_name 이 루트 이름이다."""
    # given — 느린 쪽이 각각 +90 · +0 · −5
    slow = [_span("root", "r", "", 0, 200), _span("b-op", "b", "r", 0, 100), _span("a-op", "a", "r", 0, 50), _span("c-op", "c", "r", 0, 15)]
    normal = [_span("root", "r", "", 0, 110), _span("b-op", "b", "r", 0, 10), _span("a-op", "a", "r", 0, 50), _span("c-op", "c", "r", 0, 20)]
    # when
    d = _pair(slow, normal)
    # then
    assert d == TraceDiff(slow_id="slow", normal_id="normal", root_name="root", deltas=[
        SpanDelta("svc", "a-op", SpanTime(50.0, 1), SpanTime(50.0, 1), 0.0),
        SpanDelta("svc", "b-op", SpanTime(100.0, 1), SpanTime(10.0, 1), 90.0),
        SpanDelta("svc", "c-op", SpanTime(15.0, 1), SpanTime(20.0, 1), -5.0),
        SpanDelta("svc", "root", SpanTime(200.0, 1), SpanTime(110.0, 1), 90.0),
    ])


@pytest.mark.parametrize("services", [["f", "c", "a", "e", "b", "d"], ["zeta", "eta", "theta", "iota", "kappa", "lambda"]], ids=["letters", "words"])
def test_diff_orders_rows_of_one_name_by_service(services: list[str]) -> None:
    """같은 이름의 span 이 서로 다른 서비스 여럿에 있으면 그 행들은 service 오름차순이다 — 동률의 순서가 set 반복 순서(해시 시드)를 따르지 않는다."""
    # given — 이름 하나 · 서비스 여섯: 이름만으로 정렬하는 구현이 우연히 통과할 확률은 벌마다 1/6! 이고, 두 벌이 함께 통과할 확률은 그 제곱이다
    spans = [_span("root", "r", "", 0, 100)] + [_span("q", "q-" + s, "r", 0, 10, service=s) for s in services]
    # when
    d = _pair(spans, spans)
    # then
    assert [x.service for x in d.deltas if x.name == "q"] == sorted(services)


def test_diff_marks_spans_present_on_one_side_only() -> None:
    """한쪽에만 있는 span 은 있는 쪽 SpanTime 만 있고 다른 쪽과 delta 가 None 이다."""
    # given
    slow = [_span("root", "r", "", 0, 100), _span("only-slow", "s", "r", 0, 30)]
    normal = [_span("root", "r", "", 0, 100), _span("only-normal", "n", "r", 0, 20)]
    # when
    by_name = {x.name: x for x in _pair(slow, normal).deltas}
    # then
    assert by_name["only-slow"].slow == SpanTime(total_ms=30.0, n=1)
    assert by_name["only-slow"].normal is None
    assert by_name["only-slow"].delta_ms is None
    assert by_name["only-normal"].slow is None
    assert by_name["only-normal"].normal == SpanTime(total_ms=20.0, n=1)
    assert by_name["only-normal"].delta_ms is None


def test_diff_sums_repeated_span_names_and_counts_them() -> None:
    """같은 이름의 span 이 느린 쪽에 셋 · 정상 쪽에 하나면 행 하나에 합과 건수(3 · 1)로 접힌다."""
    # given
    slow = [_span("root", "r", "", 0, 100)] + [_span("q", "q%d" % i, "r", 0, 10) for i in range(3)]
    normal = [_span("root", "r", "", 0, 100), _span("q", "q", "r", 0, 10)]
    # when
    by_name = {x.name: x for x in _pair(slow, normal).deltas}
    # then
    assert by_name["q"].slow == SpanTime(total_ms=30.0, n=3)
    assert by_name["q"].normal == SpanTime(total_ms=10.0, n=1)
    assert by_name["q"].delta_ms == pytest.approx(20.0)


def test_diff_rejects_traces_with_different_root_names() -> None:
    """루트 이름이 다르면 ObsError 이고 문구에 두 이름이 있다."""
    # given · when · then
    with pytest.raises(ObsError) as ei:
        _pair([_span("GET /a", "r", "", 0, 10)], [_span("GET /b", "r", "", 0, 10)])
    assert "GET /a" in str(ei.value)
    assert "GET /b" in str(ei.value)


def test_diff_rejects_empty_trace() -> None:
    """한쪽이 span 없음이면 ObsError 다."""
    # given · when · then
    with pytest.raises(ObsError):
        _pair([_span("root", "r", "", 0, 10)], [])


def test_diff_names_the_root_not_an_earlier_orphan() -> None:
    """느린 쪽에 루트(parent 빔)와 그보다 먼저 시작한 고아(parent 가 없는 id · 이름 다름)가 있으면 root_name 은 루트의 이름이고 ObsError 가 아니며 고아는 느린 쪽에만 있음 행으로 남는다."""
    # given
    slow = [_span("db.query", "o", "missing", 0, 50), _span("GET /jobs", "r", "", 100, 800)]
    normal = [_span("GET /jobs", "r", "", 0, 10)]
    # when
    d = _pair(slow, normal)
    orphan_rows = [x for x in d.deltas if x.name == "db.query"]
    # then
    assert d.root_name == "GET /jobs"
    assert len(orphan_rows) == 1
    assert orphan_rows[0].normal is None
    assert orphan_rows[0].delta_ms is None


@pytest.mark.parametrize("slow", [[_span("root", "x", "m1", 0, 10), _span("b", "y", "m2", 5, 10)], [_span("a", "x", "y", 0, 10), _span("b", "y", "x", 0, 10)]],
                         ids=["orphans", "cycle"])
def test_diff_rejects_trace_without_root(slow: list[Span]) -> None:
    """한쪽에 루트(parent 가 빈 span)가 없으면 — 고아뿐이든 순환이든 — 루트 없음을 말하는 ObsError 다. 비교할 루트 이름이 없고 ValueError 가 아니다."""
    # given — [orphans] 의 먼저 시작한 고아 이름은 정상 쪽 루트와 같다: 고아를 루트로 승격하는 구현이면 이름이 맞아 거부되지 않는다
    normal = [_span("root", "r", "", 0, 10)]
    # when · then
    with pytest.raises(ObsError) as ei:
        _pair(slow, normal)
    assert "루트" in str(ei.value)
    assert "이름이 다른" not in str(ei.value)


# ── diff — GET 2 회 ────────────────────────────────────────────────────────────────

def test_diff_fetches_slow_then_normal(make_client: ClientFactory) -> None:
    """api/traces/<slow> → api/traces/<normal> 순으로 두 번 부른다."""
    # given
    body = _otlp([_span("root", "r", "", 0, 10)])
    client, t = make_client({("traces", _trace_path("s1")): (200, body), ("traces", _trace_path("n1")): (200, body)})
    # when
    ev = traces.diff(client, "s1", "n1", W)
    # then
    assert [_path(c).rsplit("/", 1)[1] for c in t.calls] == ["s1", "n1"]
    assert (ev.value.slow_id, ev.value.normal_id) == ("s1", "n1")


@pytest.mark.parametrize("slow,normal", [("same", "same"), (" ", "n1")], ids=["same", "blank"])
def test_diff_rejects_identical_or_blank_ids(make_client: ClientFactory, slow: str, normal: str) -> None:
    """두 id 가 같거나 한쪽이 공백이면 요청 없이 ObsError 다."""
    # given
    client, t = make_client({})
    # when · then
    with pytest.raises(ObsError):
        traces.diff(client, slow, normal, W)
    assert t.calls == []


# ── 렌더러의 성질 — 모양은 format 이, 여기는 약속만 ───────────────────────────────────

def _exemplar(trace_id: str = "t1", source: str = THRESHOLD_BASELINE, threshold_ms: float = 10.0) -> Exemplar:
    return Exemplar(trace_id=trace_id, span_id="s", service="a", endpoint="x", pod="p", duration_ms=50.0, start_ts=AT, threshold_ms=threshold_ms,
                    threshold_source=source, ratio=50.0 / threshold_ms)


def _exemplars(sample: int = 1, slow: list[Exemplar] | None = None, normal: list[Exemplar] | None = None, truncated: bool = False,
               quantile: float = 0.95, slow_total: int | None = None) -> Exemplars:
    slow_items = slow or []
    return Exemplars(k=3, quantile=quantile, sample=sample, truncated=truncated,
                     slow=Bucket("slow", slow_total if slow_total is not None else len(slow_items), slow_items), error=Bucket("error", 0, []),
                     normal=Bucket("normal", len(normal or []), normal or []))


def _path_span(name: str = "x", clipped_ms: float = 0.0, depth: int = 0) -> PathSpan:
    return PathSpan(service="a", name=name, span_id=name, depth=depth, offset_ms=0.0, duration_ms=10.0, self_ms=5.0, clipped_ms=clipped_ms)


def _critical(path: list[PathSpan], roots: int = 1, orphans: int = 0, spans: int | None = None) -> CriticalPath:
    return CriticalPath(trace_id="t", spans=len(path) if spans is None else spans, total_ms=10.0, path=path, dropped=0, roots=roots, orphans=orphans)


def _edge(callee: str | None = "b", server: float | None = 30.0) -> Edge:
    return Edge(caller="a", callee=callee, endpoint="X", count=10, error_rate=0.0, client_p95_ms=40.0, client_base_p95_ms=20.0, ratio=2.0, server_p95_ms=server)


def _graph(edges: list[Edge], idle: int = 0) -> ServiceGraph:
    return ServiceGraph(quantile=0.95, baseline_range="7d", edges=edges, idle=idle)


def _tdiff(deltas: list[SpanDelta]) -> TraceDiff:
    return TraceDiff(slow_id="s", normal_id="n", root_name="root", deltas=deltas)


def test_exemplars_lines_mark_empty_bucket_as_none_not_no_data() -> None:
    """표본은 있는데 slow 묶음이 비면 그 묶음 줄에 '없음' 이 있고 어느 줄에도 '데이터 없음' 은 없다."""
    # given
    lines = cli.exemplars_lines(_exemplars(sample=1, normal=[_exemplar()]), W)
    # when
    slow_lines = [line for line in lines if line.startswith("slow")]
    # then
    assert len(slow_lines) == 1
    assert "없음" in slow_lines[0]
    assert not any("데이터 없음" in line for line in lines)


def test_exemplars_lines_no_data_when_sample_is_zero() -> None:
    """sample 이 0 이면 한 줄이고 '(데이터 없음' 으로 시작하며 창을 넓히라는 --since 안내가 있다."""
    # given · when
    lines = cli.exemplars_lines(_exemplars(sample=0), W)
    # then
    assert len(lines) == 1
    assert lines[0].startswith("(데이터 없음")
    assert "--since" in lines[0]


@pytest.mark.parametrize("source", [THRESHOLD_BASELINE, THRESHOLD_SAMPLE])
def test_exemplars_lines_show_threshold_and_source_per_item(source: str) -> None:
    """건 줄에 적용한 임계 값과 그 출처 문자열이 있다."""
    # given
    lines = cli.exemplars_lines(_exemplars(sample=1, slow=[_exemplar(source=source, threshold_ms=12.5)]), W)
    # when
    item_lines = [line for line in lines if "t1" in line]
    # then
    assert len(item_lines) == 1
    assert "12.5" in item_lines[0]
    assert source in item_lines[0]


def test_exemplars_lines_mark_truncated_sample() -> None:
    """truncated 면 헤더에 절단 표시가 있고 아니면 없다."""
    # given · when
    with_cut = cli.exemplars_lines(_exemplars(sample=5, truncated=True), W)
    without = cli.exemplars_lines(_exemplars(sample=5, truncated=False), W)
    # then
    assert any("절단" in line for line in with_cut)
    assert not any("절단" in line for line in without)


@pytest.mark.parametrize("quantile,expected", [(0.95, "5%"), (0.9, "10%")], ids=["p95", "p90"])
def test_exemplars_lines_summarize_sample_exceeded_and_expected_ratio(quantile: float, expected: str) -> None:
    """요약 줄에 표본 수 · 임계 초과 수 · 평소 기대 비율((1 − q) × 100%) 이 함께 있다."""
    # given
    value = _exemplars(sample=40, slow=[_exemplar("t%d" % i) for i in range(3)], slow_total=6, quantile=quantile)
    # when
    summary = [line for line in cli.exemplars_lines(value, W) if "40" in line and expected in line]
    # then
    assert len(summary) == 1
    assert " 6" in summary[0]


def test_critical_path_lines_mark_clipped_spans() -> None:
    """clipped_ms 가 0 보다 큰 경로 span 의 줄에만 잘림 표시가 있고 그 줄에 넘친 길이(+400.0ms)가 함께 있으며 문구는 "부모 경계" 를 말한다(시작 쪽 잘림에도 맞는 말)."""
    # given
    lines = cli.critical_path_lines(_critical([_path_span("root"), _path_span("async", clipped_ms=400.0, depth=1)]), W)
    # when
    marked = [line for line in lines if "잘림" in line]
    # then
    assert len(marked) == 1
    assert "async" in marked[0]
    assert "+400.0ms" in marked[0]
    assert "부모 경계" in marked[0]
    assert not any("잘림" in line and "root" in line for line in lines)


@pytest.mark.parametrize("roots,flagged", [(2, True), (1, False)], ids=["two", "one"])
def test_critical_path_lines_flag_incomplete_trace_with_several_roots(roots: int, flagged: bool) -> None:
    """roots 가 2 면 헤더에 불완전한 트레이스 표시(루트 2 개)가 있고 1 이면 없다."""
    # given · when
    lines = cli.critical_path_lines(_critical([_path_span("root")], roots=roots), W)
    # then
    assert any("불완전" in line and "루트 2" in line for line in lines) is flagged


@pytest.mark.parametrize("orphans,flagged", [(1, True), (0, False)], ids=["one", "none"])
def test_critical_path_lines_flag_orphans(orphans: int, flagged: bool) -> None:
    """orphans 가 1 이면 헤더에 고아 표시("고아 1 개" · 부모 span 유실)가 있고 0 이면 없다."""
    # given · when
    lines = cli.critical_path_lines(_critical([_path_span("root")], orphans=orphans), W)
    # then
    assert any("고아 1" in line for line in lines) is flagged


def test_critical_path_lines_say_no_path_without_root() -> None:
    """roots 가 0 이고 path 가 비면 헤더 한 줄뿐이고 거기에 "임계경로 없음" 과 "루트 없음" 이 있다 — 경로 줄도 병목 줄도 없다."""
    # given · when
    lines = cli.critical_path_lines(_critical([], roots=0, orphans=2, spans=2), W)
    # then
    assert len(lines) == 1
    assert "임계경로 없음" in lines[0]
    assert "루트 없음" in lines[0]


@pytest.mark.parametrize("idle", [3, 7])
def test_service_graph_lines_say_idle_not_no_data_when_rows_have_zero_calls(idle: int) -> None:
    """edges 가 비어도 idle 이 있으면 '데이터 없음' 이 아니라 알려진 호출 idle 개가 창 안에서 0 건이라고 말한다."""
    # given
    lines = cli.service_graph_lines(_graph([], idle=idle), W)
    # when
    zero_lines = [line for line in lines if "0건" in line]
    # then
    assert not any("데이터 없음" in line for line in lines)
    assert len(zero_lines) == 1
    assert "%d개" % idle in zero_lines[0]


def test_service_graph_lines_mark_unknown_callee_and_missing_server_p95() -> None:
    """callee 가 None 이면 그 자리에 '미확인' 이, server_p95_ms 가 None 이면 숫자 대신 없음 표시가 있다."""
    # given
    lines = cli.service_graph_lines(_graph([_edge(callee=None, server=None)]), W)
    # when
    head_lines = [line for line in lines if "→" in line]
    endpoint_lines = [line for line in lines[1:] if "→" not in line]
    # then
    assert len(head_lines) == 1
    assert "미확인" in head_lines[0]
    assert len(endpoint_lines) == 1
    assert "서버 —" in endpoint_lines[0]


def test_diff_lines_mark_one_sided_spans() -> None:
    """한쪽에만 있는 행에는 '한쪽에만 있음' 표시가 있고 Δ 숫자가 없다."""
    # given
    deltas = [SpanDelta("a", "only-slow", SpanTime(30.0, 1), None, None), SpanDelta("a", "both", SpanTime(30.0, 1), SpanTime(10.0, 1), 20.0)]
    # when
    lines = cli.diff_lines(_tdiff(deltas), W)
    one_sided = [line for line in lines if "only-slow" in line]
    # then
    assert len(one_sided) == 1
    assert "한쪽에만 있음" in one_sided[0]
    assert "Δ+" not in one_sided[0]
    assert "Δ-" not in one_sided[0]


@pytest.mark.parametrize("render", ["exemplars", "critical-path", "service-graph", "diff"])
def test_traces_lines_carry_no_physical(render: str) -> None:
    """네 렌더러의 출력 어디에도 물리 문자열이 없다."""
    # given · when
    if render == "exemplars":
        lines = cli.exemplars_lines(_exemplars(sample=1, slow=[_exemplar()]), W)
    elif render == "critical-path":
        lines = cli.critical_path_lines(_critical([_path_span("root")]), W)
    elif render == "service-graph":
        lines = cli.service_graph_lines(_graph([_edge()]), W)
    else:
        lines = cli.diff_lines(_tdiff([SpanDelta("a", "x", SpanTime(1.0, 1), SpanTime(1.0, 1), 0.0)]), W)
    # then
    _no_physical("\n".join(lines))


# ── traces_run — 디스패치 · kind · queries 유지 · 원본은 요약에 없음 · validate ────────────

RAW_MARKERS: tuple[str, ...] = ("startTimeUnixNano", "batches", "resourceSpans", "spanSets", "resultType")


def _routes_for(cmd: str) -> dict[tuple[str, str], tuple[int, object]]:
    trace_body = _otlp([_span("root", "r", "", 0, 10), _span("child", "c", "r", 1, 9)])
    if cmd == "exemplars":
        return {("metrics", INSTANT): (200, _vector(_stat_series("a", "x", "base", "0.01"))), ("traces", SEARCH): (200, _search(("t1", [_hit_span("x", 50, service="a")])))}
    if cmd == "critical-path":
        return {("traces", _trace_path("abc")): (200, trace_body)}
    if cmd == "service-graph":
        return {("metrics", INSTANT): (200, _vector(_stat_series("a", "X", "calls", "3")))}
    return {("traces", _trace_path("s1")): (200, trace_body), ("traces", _trace_path("n1")): (200, trace_body)}


def _args_for(cmd: str) -> cli.TracesArgs:
    if cmd == "critical-path":
        return cli.TracesArgs(cmd=cmd, trace_id="abc")
    if cmd == "diff":
        return cli.TracesArgs(cmd=cmd, slow_id="s1", normal_id="n1")
    return cli.TracesArgs(cmd=cmd)


def test_traces_run_exemplars_passes_baseline_range(make_client: ClientFactory) -> None:
    """exemplars 의 호출은 metrics · traces 순이고 metrics 질의에 --baseline-range 의 구간이 있다."""
    # given
    client, t = make_client(_routes_for("exemplars"))
    # when
    cli.traces_run(client, cli.TracesArgs(cmd="exemplars", baseline_range="3d"), at=AT)
    # then
    assert [_path(c).endswith(p) for c, p in zip(t.calls, (INSTANT, SEARCH))] == [True, True]
    assert "[3d] offset" in _params(t.calls[0])["query"]


@pytest.mark.parametrize("a", [cli.TracesArgs(cmd="graph"), cli.TracesArgs(cmd="critical-path"), cli.TracesArgs(cmd="diff", slow_id="s1")],
                         ids=["unknown", "critical-path", "diff"])
def test_traces_args_validate_rejects(make_client: ClientFactory, a: cli.TracesArgs) -> None:
    """모르는 cmd · critical-path 에 id 없음 · diff 에 id 하나는 요청 없이 ObsError 다."""
    # given
    client, t = make_client({})
    # when · then
    with pytest.raises(ObsError):
        cli.traces_run(client, a, at=AT)
    assert t.calls == []



# ── traces_run — 환원 값을 이름으로 ──────────────────────────────────────────────────

TRACES_TYPES: dict[str, type[object]] = {"exemplars": Exemplars, "critical-path": CriticalPath, "service-graph": ServiceGraph, "diff": TraceDiff}
"""서브커맨드 → traces_run 의 evidence.value 가 되는 환원 dataclass."""


@pytest.mark.parametrize("cmd", cli.TRACES_SUBCOMMANDS)
def test_traces_run_returns_reduction_value_by_subcommand(make_client: ClientFactory, cmd: str) -> None:
    """traces_run 의 결과는 kind 가 traceql-<cmd> 이고 evidence.value 가 그 연산의 환원 dataclass(Exemplars · CriticalPath · ServiceGraph · TraceDiff)다 — 줄이 아니다."""
    # given — 최소 라우트
    client, _ = make_client(_routes_for(cmd))
    # when
    r = cli.traces_run(client, _args_for(cmd), at=AT)
    # then
    assert r.kind == "traceql-" + cmd
    assert isinstance(r.evidence.value, TRACES_TYPES[cmd])


@pytest.mark.parametrize("cmd", cli.TRACES_SUBCOMMANDS)
def test_traces_run_value_carries_no_raw_trace_body(make_client: ClientFactory, cmd: str) -> None:
    """환원 값(evidence.value)의 직렬화 어디에도 원본 표식(startTimeUnixNano · batches · resourceSpans · spanSets · resultType)이 없고 client.log 에는 있다 — 값에 없으면 어느 렌더러도 실을 수 없다."""
    # given
    client, _ = make_client(_routes_for(cmd))
    # when
    r = cli.traces_run(client, _args_for(cmd), at=AT)
    text = to_json(r.evidence.value)
    # then
    for marker in RAW_MARKERS:
        assert marker not in text
    assert len(client.log) >= 1
    assert any(marker in json.dumps(to_dict(client.log)) for marker in RAW_MARKERS)


def test_traces_run_exemplars_passes_k_quantile_limit(make_client: ClientFactory) -> None:
    """exemplars 의 Args 의 k · quantile 은 환원 값(Exemplars.k · .quantile)에, limit 은 search 요청(limit = a.limit + 1 — 초과 감지용 하나 더)에 나타난다."""
    # given — exemplars 의 최소 라우트(metrics 즉시 조회 + traces search) · k=2 · quantile=0.9 · limit=5
    client, t = make_client(_routes_for("exemplars"))
    # when
    r = cli.traces_run(client, cli.TracesArgs(cmd="exemplars", k=2, quantile=0.9, limit=5), at=AT)
    # then
    assert isinstance(r.evidence.value, Exemplars)
    assert r.evidence.value.k == 2
    assert r.evidence.value.quantile == 0.9
    assert _path(t.calls[1]).endswith(SEARCH)
    assert _params(t.calls[1])["limit"] == "6"
