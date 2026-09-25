"""응답 형상의 변형 — 파서 · 렌더러가 손 입력의 변형에서 지키는 성질: OTLP v1/v2 봉투 · 속성 값의 형 · 스트림 둘 이상의 공통 레이블 ·
루트 없음/여럿 · 오류 span 절단 · 즉시 조회의 range vector · 숫자 아닌 값만 있는 시계열 · string resultType · 파싱 불가 ts."""
from __future__ import annotations

import pytest

from obs.adapters import cli
from obs.core import JSON
from obs.metrics import Series, parse_metric, series_items


# ── OTLP — parse_spans 의 봉투 · 속성 값 ─────────────────────────────────────────

def _attr(key: str, value: dict[str, JSON]) -> JSON:
    return {"key": key, "value": value}


def _otlp_span(name: str, span_id: str, parent: str = "", attrs: list[JSON] | None = None) -> JSON:
    return {"name": name, "spanId": span_id, "parentSpanId": parent, "startTimeUnixNano": "1000", "endTimeUnixNano": "3000",
            "status": {"code": "STATUS_CODE_UNSET"}, "attributes": attrs or []}


def _batch(spans: list[JSON], scope_key: str = "scopeSpans") -> JSON:
    return {"resource": {"attributes": [_attr("service.name", {"stringValue": "svc"})]}, scope_key: [{"spans": spans}]}


def _envelope(kind: str, batches: list[JSON]) -> dict[str, JSON]:
    if kind == "batches":
        return {"batches": batches}
    if kind == "resourceSpans":
        return {"resourceSpans": batches}
    return {"trace": {"resourceSpans": batches}}


@pytest.mark.parametrize("kind", ["batches", "resourceSpans", "trace.resourceSpans"])
def test_parse_spans_reads_the_same_spans_from_every_envelope(kind: str) -> None:
    """parse_spans 는 batches(v1) · resourceSpans(v2) · trace.resourceSpans 어느 봉투에서든 같은 span 을 읽는다."""
    body = _envelope(kind, [_batch([_otlp_span("root", "1"), _otlp_span("child", "2", parent="1")])])
    spans = cli.parse_spans(body)
    assert [(s.service, s.name, s.parent_id) for s in spans] == [("svc", "root", ""), ("svc", "child", "1")]


def test_parse_spans_accepts_instrumentation_library_spans() -> None:
    """scopeSpans 대신 옛 이름 instrumentationLibrarySpans 로 와도 span 을 읽는다."""
    spans = cli.parse_spans({"batches": [_batch([_otlp_span("op", "1")], scope_key="instrumentationLibrarySpans")]})
    assert [s.name for s in spans] == ["op"]


def test_attribute_values_of_every_otlp_type_become_strings() -> None:
    """속성 값은 stringValue · intValue · doubleValue · boolValue 어느 형이든 문자열로 실린다."""
    attrs = [_attr("s", {"stringValue": "x"}), _attr("i", {"intValue": "42"}), _attr("d", {"doubleValue": 1.5}), _attr("b", {"boolValue": True})]
    spans = cli.parse_spans({"batches": [_batch([_otlp_span("op", "1", attrs=attrs)])]})
    assert spans[0].attrs == {"s": "x", "i": "42", "d": "1.5", "b": "True"}


def test_attribute_value_of_unknown_shape_is_still_a_string() -> None:
    """알 수 없는 형(arrayValue 등)의 속성 값도 예외 없이 문자열로 실린다."""
    spans = cli.parse_spans({"batches": [_batch([_otlp_span("op", "1", attrs=[_attr("a", {"arrayValue": {"values": []}})])])]})
    assert isinstance(spans[0].attrs["a"], str)
    assert spans[0].attrs["a"] != ""


# ── Loki — 스트림 둘 이상의 공통 레이블 ───────────────────────────────────────────

def _stream(labels: dict[str, str], *lines: tuple[int, str]) -> cli.Stream:
    return cli.Stream(labels=labels, values=list(lines))


def test_common_labels_keeps_only_keys_equal_across_all_streams() -> None:
    """common_labels 는 모든 스트림에 같은 값으로 있는 키만 남긴다 — 값이 다르거나 한 스트림에 없는 키는 빠진다."""
    streams = [_stream({"ns": "n", "app": "x", "pod": "a"}), _stream({"ns": "n", "app": "x", "pod": "b"}), _stream({"ns": "n", "pod": "c"})]
    assert cli.common_labels(streams) == {"ns": "n"}


def test_common_labels_of_single_stream_is_all_its_labels() -> None:
    """스트림이 하나면 그 레이블 전부가 공통이고, 없으면 빈 dict 다."""
    assert cli.common_labels([_stream({"ns": "n", "pod": "a"})]) == {"ns": "n", "pod": "a"}
    assert cli.common_labels([]) == {}


def test_stream_lines_prints_common_header_once_and_only_differences_per_line() -> None:
    """스트림 둘 이상이면 공통 레이블은 머리 한 줄에만 있고 각 로그 줄에는 차이 나는 레이블만 붙는다."""
    res = cli.LogResult("streams", [_stream({"ns": "n", "pod": "a"}, (1_000_000_000, "l1")), _stream({"ns": "n", "pod": "b"}, (2_000_000_000, "l2"))], [])
    lines = cli.stream_lines(res, "backward", 200)
    assert len(lines) == 1 + 2
    assert lines[0].startswith("공통 레이블 ")
    assert "ns=n" in lines[0]
    assert all("ns=" not in line for line in lines[1:])
    assert all("pod=" in line for line in lines[1:])


def test_stream_lines_without_common_labels_has_no_header() -> None:
    """공통 레이블이 하나도 없으면 머리 줄이 없다 — 첫 줄부터 로그 줄이고 각 줄에 자기 레이블이 전부 붙는다."""
    res = cli.LogResult("streams", [_stream({"pod": "a"}, (1_000_000_000, "l1")), _stream({"pod": "b"}, (2_000_000_000, "l2"))], [])
    lines = cli.stream_lines(res, "backward", 200)
    assert len(lines) == 2
    assert all("pod=" in line for line in lines)


# ── Tempo — trace_lines 의 가장자리 ──────────────────────────────────────────────

def _span(name: str, span_id: str, parent: str = "", error: bool = False, service: str = "svc") -> cli.Span:
    return cli.Span(service=service, name=name, span_id=span_id, parent_id=parent, start_ns=1_000_000_000, end_ns=1_002_000_000,
                    status_code="STATUS_CODE_ERROR" if error else "")


def test_trace_lines_without_spans_says_so() -> None:
    """span 이 하나도 없으면 한 줄로 "span 없음" 을 말한다."""
    lines = cli.trace_lines("abc", [], top=5)
    assert len(lines) == 1
    assert "span 없음" in lines[0]
    assert "abc" in lines[0]


def test_trace_lines_without_root_marks_none() -> None:
    """모든 span 에 부모가 있으면(루트 없음) 루트 줄이 "(없음)" 이다."""
    lines = cli.trace_lines("abc", [_span("a", "1", parent="9"), _span("b", "2", parent="1")], top=5)
    assert lines[1].startswith("루트: ")
    assert lines[1].endswith("(없음)")


def test_trace_lines_lists_every_root() -> None:
    """루트가 여럿이면 루트 줄에 전부 실린다."""
    lines = cli.trace_lines("abc", [_span("first", "1"), _span("second", "2"), _span("child", "3", parent="1")], top=5)
    assert "first" in lines[1]
    assert "second" in lines[1]
    assert "child" not in lines[1]


def test_trace_lines_shows_at_most_ten_error_spans() -> None:
    """오류 span 이 10개를 넘어도 ✗ 줄은 10개까지만이고 오류 수는 전부를 센다."""
    spans = [_span("root", "0")] + [_span("e%d" % i, "%d" % (i + 1), parent="0", error=True) for i in range(12)]
    lines = cli.trace_lines("abc", spans, top=3)
    assert lines[3] == "오류 span: 12"
    assert sum(1 for line in lines if line.startswith("  ✗ ")) == 10


# ── Prometheus 형 — 즉시 조회의 range vector · 숫자 아닌 값 · string · 파싱 불가 ts ────

def _series(values: list[tuple[float, str]], labels: dict[str, str] | None = None) -> Series:
    return Series(labels=labels or {"pod": "a"}, values=values)


def _result(*series: Series) -> cli.MetricResult:
    return cli.MetricResult(result_type="vector", scalar=None, series=list(series))


def test_instant_lines_range_vector_lists_values_in_brackets() -> None:
    """즉시 조회에 range vector(값 여럿)가 오면 값 목록을 [ ] 안에 한 줄로 낸다."""
    lines = cli.instant_lines(_result(_series([(1.0, "1"), (2.0, "2"), (3.0, "3")])))
    assert len(lines) == 1
    assert lines[0].startswith("[1, 2, 3]")
    assert "pod=a" in lines[0]


def test_instant_lines_range_vector_clips_after_ten_values() -> None:
    """range vector 의 값이 10개를 넘으면 10개까지만 보이고 … 로 나머지를 표시한다."""
    lines = cli.instant_lines(_result(_series([(float(i), "%d" % i) for i in range(12)])))
    inside = lines[0][lines[0].index("[") + 1: lines[0].index("]")]
    assert inside.endswith(" …")
    assert len(inside.rstrip(" …").split(", ")) == 10


def test_range_lines_non_numeric_only_is_n_zero() -> None:
    """값이 전부 숫자가 아닌 시계열은 요약 대신 n=0 줄이다."""
    lines = cli.range_lines(_result(_series([(1.0, "x"), (2.0, "y")])), points=False)
    assert lines == ["n=0  " + cli.labels_str({"pod": "a"})]


def test_matrix_lines_non_numeric_only_is_n_zero() -> None:
    """메트릭형 LogQL 의 시계열도 값이 전부 숫자가 아니면 n=0 줄이다."""
    res = cli.LogResult("matrix", [], [_series([(1.0, "x")])])
    assert cli.matrix_lines(res) == ["n=0  " + cli.labels_str({"pod": "a"})]


def test_parse_metric_string_result_goes_to_scalar() -> None:
    """resultType string 은 scalar 와 같이 scalar 필드에 값이 실리고 시계열은 없다."""
    res = parse_metric({"data": {"resultType": "string", "result": [1.0, "hello"]}})
    assert res.result_type == "string"
    assert res.scalar == "hello"
    assert res.series == []


def test_series_items_skips_unparsable_timestamps_but_keeps_the_series() -> None:
    """ts 가 숫자가 아닌 값 쌍은 건너뛰고 나머지 값은 남는다 — 시계열 자체는 유지된다."""
    out = series_items([{"metric": {"pod": "a"}, "values": [["abc", "1"], ["2", "3"]]}])
    assert len(out) == 1
    assert out[0].values == [(2.0, "3")]
