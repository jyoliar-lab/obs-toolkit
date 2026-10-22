"""메트릭 환원 연산의 약속 — 질의 조립 · 응답 → 계약 · 없음/0 판정 · 거부. 손 입력(_vector)과 성질 단언만. 렌더링의 모양은 format/test_metrics_lines.py."""
from __future__ import annotations

import json
import math
import re
import urllib.parse

import pytest

from obs import metrics
from obs.adapters import cli
from obs.core import JSON, Client, Evidence, ObsError, Window, to_json
from tests.conftest import AT, DAY, HOUR, MINUTE, RED_BASE_ONLY, RED_FULL, RED_NOW_ONLY, WEEK, ClientFactory, _no_physical, _red_body, _stat, _vector
from tests.record import CPU_QUERY
from tests.replay import Call


def _params(call: Call) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call.url).query, keep_blank_values=True))


SQ: str = "(%s)[%ds:%ds]" % (CPU_QUERY, HOUR, MINUTE)   # 1h 창 · 60s step 의 서브쿼리
RED_W: Window = Window.last("1h", at=AT).with_baseline("1d")   # red 의 창 — 1h · 기준선 1d
RED_OFFSET: str = "%ds" % DAY
INSTANT: tuple[str, str] = ("metrics", "api/v1/query")


def _red_value(**stats: str) -> metrics.Red:
    """손 입력 응답에서 만든 Red — 렌더러의 성질 검사용."""
    return metrics.Red(service="svc", offset=RED_OFFSET, signals=metrics.signals_of(metrics.parse_metric(_red_body(**stats))))



# ── 질의 조립 — 계산은 이 문자열로 서버에 넘어간다 ─────────────────────────────────

def test_summarize_query_is_seven_stats_in_one_expression() -> None:
    """summarize_query 는 일곱 통계를 서브쿼리 위의 *_over_time · deriv 로 만들어 stat 이름표를 달고 or 로 한 식이다."""
    q = metrics.summarize_query(CPU_QUERY, Window.last("1h", at=AT), "60s")
    parts = q.split(" or ")
    assert len(parts) == 7
    assert all(p.startswith('label_replace(') and p.endswith('"stat", "%s", "", "")' % s)
                                   for p, s in zip(parts, metrics.SUMMARY_STATS))
    assert "count_over_time(%s)" % SQ in q
    assert "quantile_over_time(0.95, %s)" % SQ in q
    assert "deriv(%s)" % SQ in q
    assert "last_over_time(%s)" % SQ in q
    assert "min_over_time(%s)" % SQ in q
    assert "max_over_time(%s)" % SQ in q


def test_compare_query_uses_offset_of_same_expression() -> None:
    """compare_query 는 같은 식의 지금 p95 와 offset p95 를 한 식으로 만든다."""
    q = metrics.compare_query(CPU_QUERY, Window.last("1h", at=AT), "60s", "%ds" % DAY)
    assert q == ('label_replace(quantile_over_time(0.95, %s), "stat", "now", "", "") or '
                 'label_replace(quantile_over_time(0.95, %s offset %ds), "stat", "base", "", "")' % (SQ, SQ, DAY))


def test_red_query_is_six_stats_in_one_expression() -> None:
    """red_query 는 여섯 통계를 stat 이름표로 달아 or 로 한 식이다 — 기준선 셋에만 offset 이 붙고 구간은 창 길이다."""
    # given — 1h 창 · 1d offset
    w = Window.last("1h", at=AT)
    # when
    q = metrics.red_query("svc", w, RED_OFFSET)
    # then
    parts = q.split(" or ")
    assert len(parts) == 6
    for part, stat in zip(parts, metrics.RED_STATS):
        assert part.startswith("label_replace(")
        assert part.endswith('"stat", "%s", "", "")' % stat)
        assert "[%ds]" % HOUR in part
        assert ("offset %s" % RED_OFFSET in part) == stat.endswith("_base")


def test_red_query_selects_server_rows_of_one_service_on_native_histogram() -> None:
    """red_query 의 여섯 부분은 전부 서버 span 행 · 한 서비스 · service_name 합산이고, errors 둘만 오류 status, p95 둘은 native histogram 분위수다."""
    # given
    w = Window.last("1h", at=AT)
    # when
    q = metrics.red_query("svc", w, RED_OFFSET)
    parts = q.split(" or ")
    # then — 공통 선택자
    for part in parts:
        assert metrics.SERVER_KIND in part
        assert 'service_name="svc"' in part
        assert "sum by (service_name)" in part
        assert "SPAN_KIND_CLIENT" not in part
    # then — calls · errors 는 호출 카운터의 increase, errors 만 오류 status
    calls = [p for p in parts if '"stat", "calls_' in p]
    errors = [p for p in parts if '"stat", "errors_' in p]
    assert len(calls) == 2
    assert len(errors) == 2
    for part in calls + errors:
        assert "increase(%s{" % metrics.CALLS_METRIC in part
    for part in errors:
        assert 'status_code="STATUS_CODE_ERROR"' in part
    for part in calls:
        assert "status_code" not in part
    # then — p95 는 native histogram 위의 histogram_quantile(rate) — _bucket · le 없음
    p95 = [p for p in parts if '"stat", "p95_' in p]
    assert len(p95) == 2
    for part in p95:
        assert part.startswith("label_replace(histogram_quantile(%s, sum by (service_name) (rate(%s{" % (metrics.RED_QUANTILE, metrics.SPAN_METRIC))
        assert "status_code" not in part
    assert "_bucket" not in q
    assert re.search(r"\ble\b", q) is None


@pytest.mark.parametrize("service", ["  ", 'a"b', "a\nb"], ids=["blank", "quote", "newline"])
def test_red_query_rejects_blank_or_unsafe_service(service: str) -> None:
    """공백 · 따옴표 · 개행이 든 서비스 이름은 질의 조립 전에 거부한다."""
    # given — 1h 창
    w = Window.last("1h", at=AT)
    # when · then
    with pytest.raises(ObsError):
        metrics.red_query(service, w, RED_OFFSET)


def test_auto_step_is_window_over_sixty_with_floor() -> None:
    """step 자동 — 창 길이/60, 최소 15s. promql --range 와 같은 함수다."""
    assert metrics.auto_step(30 * MINUTE) == "30s"
    assert metrics.auto_step(MINUTE) == "15s"
    assert metrics.auto_step(DAY) == "1440s"


def test_duration_literal_is_whole_seconds() -> None:
    """duration_literal 은 초를 PromQL 기간 표기로 — 1초 미만은 0s."""
    assert metrics.duration_literal(HOUR) == "3600s"
    assert metrics.duration_literal(0.4) == "0s"


def test_baseline_offset_rules() -> None:
    """offset 은 창 끝의 차이다 — 기준선 없음은 1d, prev 는 창 길이, 길이가 다르거나 뒤에 있는 기준선은 거부한다."""
    w = Window.last("1h", at=AT)
    assert metrics.DEFAULT_BASELINE == "1d"
    assert metrics.baseline_offset(w) == (w.with_baseline("1d"), "%ds" % DAY)
    assert metrics.baseline_offset(w.with_baseline("prev")) == (w.with_baseline("prev"), "%ds" % HOUR)
    assert metrics.baseline_offset(w.with_baseline("1w")) == (w.with_baseline("1w"), "%ds" % WEEK)
    shorter = Window(AT - HOUR, AT, baseline=Window(AT - HOUR, AT - 30 * MINUTE))
    later = Window(AT - HOUR, AT, baseline=Window(AT - HOUR + MINUTE, AT + MINUTE))
    for bad in (shorter, later):
        with pytest.raises(ObsError):
            metrics.baseline_offset(bad)


# ── 응답 → 계약 — 계산 없이 판정만 ───────────────────────────────────────────────

def test_split_stats_drops_non_finite_and_strips_stat_label() -> None:
    """split_stats 는 stat 이름표로 시계열별 {통계: 값} 을 만들고 NaN · Inf · 숫자 아님 · 이름표 없음은 부재로 뺀다."""
    res = metrics.parse_metric(metrics.check_status(_vector(
        _stat("a", "n", "60"), _stat("a", "p95", "NaN"), _stat("a", "min", "0"), _stat("a", "max", "+Inf"),
        _stat("b", "p95", "x"), ({"pod": "c"}, "1")), "metrics"))
    assert metrics.split_stats(res) == {(("pod", "a"),): {"n": 60.0, "min": 0.0}}


def test_summaries_of_requires_n_and_marks_missing_as_nan() -> None:
    """n 이 없는 시계열은 목록에서 빠지고, 있는데 다른 통계가 부재면 그 값은 NaN 이다."""
    res = metrics.parse_metric(_vector(_stat("a", "n", "60"), _stat("a", "p95", "0.4"), _stat("b", "p95", "1")))
    out = metrics.summaries_of(res)
    assert len(out) == 1
    assert out[0].labels == {"pod": "a"}
    assert out[0].n == 60
    assert out[0].p95 == 0.4
    assert math.isnan(out[0].min)
    assert math.isnan(out[0].slope)


# ── signals_of — 여섯 통계 → 세 행의 상태 · 비율. 0 으로 나누지 않는다 ───────────────────

def _signals(**stats: str) -> list[metrics.Signal]:
    return metrics.signals_of(metrics.parse_metric(_red_body(**stats)))


def test_signals_of_three_rows_with_now_base_ratio() -> None:
    """양쪽 창에 호출 · 오류 · 분위수가 있으면 R · E · D 세 행 순으로 지금 값 · 기준선 값 · 비율이 있고 두 상태 모두 value 다."""
    # given — 지금 200/10 건 · 0.4s, 기준선 100/5 건 · 0.2s
    res = metrics.parse_metric(_red_body(**RED_FULL))
    # when
    out = metrics.signals_of(res)
    # then
    assert [s.name for s in out] == ["R", "E", "D"]
    r, e, d = out
    assert r.now == 200
    assert r.base == 100
    assert r.ratio == pytest.approx(2)
    assert e.now == pytest.approx(0.05)
    assert e.base == pytest.approx(0.05)
    assert e.ratio == pytest.approx(1)
    assert d.now == pytest.approx(0.4)
    assert d.base == pytest.approx(0.2)
    assert d.ratio == pytest.approx(2)
    for s in out:
        assert s.now_state == metrics.RED_VALUE
        assert s.base_state == metrics.RED_VALUE


def test_signals_of_error_rise_moves_only_e_ratio() -> None:
    """호출 수 · 분위수가 두 창에 같고 오류 행만 늘면 E 의 비율만 1 을 넘고 R · D 의 비율은 1 이다."""
    # given — 호출 100 · p95 0.3 양쪽 같음, 오류 20 vs 5
    out = _signals(calls_now="100", calls_base="100", errors_now="20", errors_base="5", p95_now="0.3", p95_base="0.3")
    # when
    r, e, d = out
    # then
    assert e.ratio is not None
    assert e.ratio > 1
    assert r.ratio == pytest.approx(1)
    assert d.ratio == pytest.approx(1)


def test_signals_of_missing_calls_series_marks_all_three_missing() -> None:
    """지금 창에 호출 카운터 시계열이 없으면(오류 행만 있어도) 세 행의 지금 값이 없고 상태가 missing 이며 비율이 없다 — 기준선 값은 있다."""
    # given — calls_now 없음 · errors_now · p95_now 는 있음(제 시계열이 있어도 호출 카운터가 없으면 missing) · 기준선 셋은 있음
    out = _signals(errors_now="3", p95_now="0.3", **RED_BASE_ONLY)
    # when
    r, e, d = out
    # then
    for s in (r, e, d):
        assert s.now is None
        assert s.now_state == metrics.RED_MISSING
        assert s.ratio is None
        assert s.base is not None


def test_signals_of_zero_calls_is_idle_not_missing() -> None:
    """시계열은 있되 창 안 증가분이 0 이면 R 은 0 · E 는 0 · D 는 값 없음이고 상태는 idle 이다 — missing 과 다르다."""
    # given — calls_now 0 · errors_now 없음 · p95_now NaN · 기준선 셋은 있음
    out = _signals(calls_now="0", p95_now="NaN", **RED_BASE_ONLY)
    # when
    r, e, d = out
    # then
    assert r.now == 0
    assert e.now == 0
    assert d.now is None
    for s in (r, e, d):
        assert s.now_state == metrics.RED_IDLE
        assert s.now_state != metrics.RED_MISSING
        assert s.ratio is None


def test_signals_of_no_error_rows_means_zero_errors() -> None:
    """전체 호출 행은 있고 오류 status 행이 없으면 E 의 지금 값은 0 이고 상태는 value 다."""
    # given — calls_now 100 · errors_now 없음 · p95_now 있음 · 기준선 셋은 있음
    out = _signals(calls_now="100", p95_now="0.3", **RED_BASE_ONLY)
    # when
    _, e, _ = out
    # then
    assert e.now == 0
    assert e.now_state == metrics.RED_VALUE


def test_signals_of_missing_baseline_marks_base_missing_without_ratio() -> None:
    """기준선 창에 시계열이 없으면 세 행의 기준선 값이 없고 상태가 missing 이며 비율이 없다 — 지금 값은 있다."""
    # given — 지금 셋은 있음 · 기준선 셋 없음
    out = _signals(**RED_NOW_ONLY)
    # when
    r, e, d = out
    # then
    for s in (r, e, d):
        assert s.base is None
        assert s.base_state == metrics.RED_MISSING
        assert s.ratio is None
        assert s.now is not None


def test_signals_of_zero_baseline_has_no_ratio() -> None:
    """기준선 창의 호출이 0 이면 R · E 의 기준선 값은 0, D 는 값 없음이고 상태는 idle 이며 비율은 없다 — 0 으로 나누지 않는다."""
    # given — 지금 셋은 있음 · calls_base 0 · errors_base 없음 · p95_base NaN
    out = _signals(calls_base="0", p95_base="NaN", **RED_NOW_ONLY)
    # when
    r, e, d = out
    # then
    assert r.base == 0
    assert e.base == 0
    assert d.base is None
    for s in (r, e, d):
        assert s.base_state == metrics.RED_IDLE
        assert s.ratio is None


def test_signals_of_zero_error_baseline_has_no_e_ratio() -> None:
    """기준선에 호출은 있고 오류 행이 없으면 E 의 기준선은 0(value)이고 E 의 비율만 없다 — R 의 비율은 있다."""
    # given — 기준선 calls 100 · errors_base 없음 · 지금 errors 10
    out = _signals(calls_now="200", errors_now="10", p95_now="0.4", calls_base="100", p95_base="0.2")
    # when
    r, e, _ = out
    # then
    assert e.base == 0
    assert e.base_state == metrics.RED_VALUE
    assert e.ratio is None
    assert r.ratio is not None


@pytest.mark.parametrize("p95", [None, "NaN"], ids=["absent", "nan"])
def test_signals_of_missing_histogram_marks_only_d_missing(p95: str | None) -> None:
    """호출은 있는데 분위수 시계열이 없거나 NaN 이면 D 만 값 없음 · missing 이고 R · E 는 value 다."""
    # given — calls_now 100 · errors_now 1 · p95_now 부재/NaN · 기준선 셋은 있음
    stats = dict(calls_now="100", errors_now="1", **RED_BASE_ONLY)
    if p95 is not None:
        stats["p95_now"] = p95
    out = _signals(**stats)
    # when
    r, e, d = out
    # then
    assert d.now is None
    assert d.now_state == metrics.RED_MISSING
    assert r.now_state == metrics.RED_VALUE
    assert e.now_state == metrics.RED_VALUE


def test_signals_of_empty_response_is_three_missing_rows() -> None:
    """빈 응답이면 세 행이 모두 양쪽 missing 이다 — 목록은 비지 않는다."""
    # given — 빈 vector
    res = metrics.parse_metric(_vector())
    # when
    out = metrics.signals_of(res)
    # then
    assert [s.name for s in out] == ["R", "E", "D"]
    for s in out:
        assert s.now is None
        assert s.base is None
        assert s.now_state == metrics.RED_MISSING
        assert s.base_state == metrics.RED_MISSING


@pytest.mark.parametrize("p95", [None, "NaN"], ids=["absent", "nan"])
def test_signals_of_missing_baseline_histogram_marks_only_d_base_missing(p95: str | None) -> None:
    """기준선 창에 호출 · 오류는 있는데 분위수 시계열이 없거나 NaN 이면 D 만 기준선 없음(missing) 이고 R · E 의 기준선은 value 이며 비율이 있다."""
    # given — 지금 셋 · calls_base 100 · errors_base 5 · p95_base 부재/NaN
    stats = dict(calls_base="100", errors_base="5", **RED_NOW_ONLY)
    if p95 is not None:
        stats["p95_base"] = p95
    out = _signals(**stats)
    # when
    r, e, d = out
    # then
    assert d.base is None
    assert d.base_state == metrics.RED_MISSING
    assert d.ratio is None
    for s in (r, e):
        assert s.base is not None
        assert s.base_state == metrics.RED_VALUE
        assert s.ratio is not None


def test_signals_of_zero_baseline_quantile_has_no_d_ratio() -> None:
    """기준선의 분위수가 정확히 0 이면 D 의 기준선은 0(value)이고 D 의 비율만 없다 — R 의 비율은 있다."""
    # given — 지금 셋 · calls_base 100 · errors_base 5 · p95_base 0
    out = _signals(calls_base="100", errors_base="5", p95_base="0", **RED_NOW_ONLY)
    # when
    r, _, d = out
    # then
    assert d.base == 0
    assert d.base_state == metrics.RED_VALUE
    assert d.ratio is None
    assert r.ratio is not None


# ── 거부 · 판정 ──────────────────────────────────────────────────────────────────

def test_summarize_requires_query(make_client: ClientFactory) -> None:
    """빈 질의는 summarize 가 질의 전에 거부한다 — 문구가 PromQL 을 말하고 transport 는 불리지 않는다."""
    client, t = make_client()
    with pytest.raises(ObsError, match="PromQL 이 필요하다"):
        metrics.summarize(client, "  ", Window.last("1h", at=AT))
    assert t.calls == []


def test_compare_rejects_baseline_not_expressible_as_offset(make_client: ClientFactory) -> None:
    """길이가 다른 기준선은 offset 으로 표현할 수 없어 질의 전에 거부한다."""
    client, t = make_client()
    with pytest.raises(ObsError):
        metrics.compare(client, CPU_QUERY, Window(AT - HOUR, AT, baseline=Window(AT - HOUR, AT - 30 * MINUTE)))
    assert t.calls == []


def test_compare_missing_series_and_zero_do_not_divide(make_client: ClientFactory) -> None:
    """기준선에 없는 시계열은 base None, 기준선 0 은 비율 없음(base 는 있다), 지금에 없는 시계열은 now None 이며 어느 경우도 나누지 않는다 — 순서는 비율 → 기준선 0 → 기준선 없음 → 지금 없음."""
    body = _vector(
        _stat("a", "now", "0.4"), _stat("a", "base", "0"),
        _stat("b", "now", "1"),
                                  _stat("c", "base", "3"),
        _stat("d", "now", "3.9"), _stat("d", "base", "1")
    )
    client, _ = make_client({("metrics", "api/v1/query"): (200, body)})
    ev = metrics.compare(client, "q", Window.last("1h", at=AT).with_baseline("1d"))
    by = {d.labels["pod"]: d for d in ev.value.deltas}
    assert [d.labels["pod"] for d in ev.value.deltas] == ["d", "a", "b", "c"]

    def fields(d: metrics.Delta) -> tuple[float | None, float | None, float | None, float | None, bool]:
        return d.now, d.base, d.ratio, d.delta, d.base_is_zero

    assert fields(by["a"]) == (0.4, 0.0, None, pytest.approx(0.4), True)
    assert fields(by["b"]) == (1.0, None, None, None, False)
    assert fields(by["c"]) == (None, 3.0, None, None, False)
    assert fields(by["d"]) == (3.9, 1.0, pytest.approx(3.9), pytest.approx(2.9), False)
    assert not ev.value.now_empty
    assert not ev.value.baseline_empty


def test_top_query_wraps_topk_over_aggregation_by_axes() -> None:
    """top_query 는 topk(k, <agg> by (<축들>) (q)) — 축은 여럿이고 공백은 다듬는다."""
    assert metrics.top_query("up", 5, ["namespace", " pod "], "max") == "topk(5, max by (namespace, pod) (up))"


@pytest.mark.parametrize("k,by,agg", [(0, ["pod"], "sum"), (5, ["pod"], "p95"), (5, [" "], "sum")])
def test_top_query_rejects_bad_k_agg_or_empty_axis(k: int, by: list[str], agg: str) -> None:
    """k < 1 · 정해지지 않은 집계 · 빈 축은 질의 전에 거부한다."""
    with pytest.raises(ObsError):
        metrics.top_query("up", k, by, agg)


# ── 연산의 약속 — 손 입력으로 ─────────────────────────────────────────────────────

W: Window = Window.last("1h", at=AT)


def _run(client: Client, op: str) -> Evidence[metrics.Summary] | Evidence[metrics.Comparison] | Evidence[metrics.Top] | Evidence[metrics.Red]:
    """네 연산을 같은 창으로 부른다 — 약속 검사의 공통 입구."""
    if op == "summarize":
        return metrics.summarize(client, CPU_QUERY, W)
    if op == "compare":
        return metrics.compare(client, CPU_QUERY, W.with_baseline("1d"))
    if op == "red":
        return metrics.red(client, "svc", W.with_baseline("1d"))
    return metrics.top(client, "up", W, k=3, by=["pod"])


@pytest.mark.parametrize("op", ["summarize", "compare", "top", "red"])
def test_each_operation_makes_one_get(make_client: ClientFactory, op: str) -> None:
    """연산당 GET 은 1회다 — 계산은 서버가 한다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    _run(client, op)
    assert len(t.calls) == 1


@pytest.mark.parametrize("op", ["summarize", "compare", "top", "red"])
def test_each_operation_serializes_to_json(make_client: ClientFactory, op: str) -> None:
    """환원물이 든 Evidence 는 to_json 으로 직렬화된다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    json.loads(to_json(_run(client, op)))


def test_summarize_sends_assembled_query_with_auto_step(make_client: ClientFactory) -> None:
    """summarize 는 자동 step 으로 조립한 질의를 보내고 Summary 의 step 은 그 자동값이다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev = metrics.summarize(client, CPU_QUERY, W)
    assert _params(t.calls[0])["query"] == metrics.summarize_query(CPU_QUERY, W, "60s")
    assert ev.value.step == "60s"


def test_summarize_sorts_series_by_labels(make_client: ClientFactory) -> None:
    """summarize 의 시계열은 레이블 순이다 — 서버가 준 순서가 아니다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector(_stat("b", "n", "1"), _stat("a", "n", "1")))})
    ev = metrics.summarize(client, CPU_QUERY, W)
    assert [s.labels["pod"] for s in ev.value.series] == ["a", "b"]


def test_summarize_empty_response_has_no_series(make_client: ClientFactory) -> None:
    """빈 응답의 summarize 는 series 가 빈 목록이다 — 0 이 아니다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev = metrics.summarize(client, CPU_QUERY, W)
    assert ev.value.series == []


def test_compare_default_baseline_is_one_day(make_client: ClientFactory) -> None:
    """기준선 없는 Window 를 주면 DEFAULT_BASELINE(1d) — offset 86400s 가 나간다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev = metrics.compare(client, CPU_QUERY, W)
    assert ev.value.offset == "%ds" % DAY
    assert "offset+%ds" % DAY in t.calls[0].url


def test_compare_prev_baseline(make_client: ClientFactory) -> None:
    """--baseline prev 는 직전 창 — offset 이 창 길이(3600s)다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev = metrics.compare(client, CPU_QUERY, W.with_baseline("prev"))
    assert ev.value.offset == "%ds" % W.seconds
    assert "offset+%ds" % W.seconds in t.calls[0].url


def test_compare_both_empty(make_client: ClientFactory) -> None:
    """양쪽 다 비면 now_empty · baseline_empty 이고 deltas 는 빈 목록이다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev = metrics.compare(client, CPU_QUERY, W)
    assert ev.value.now_empty
    assert ev.value.baseline_empty
    assert ev.value.deltas == []


def test_red_sends_assembled_query_once_at_window_end(make_client: ClientFactory) -> None:
    """red 는 metrics 즉시 조회를 한 번 보내고, 그 query 는 red_query 가 조립한 것이며 time 은 창 끝이다."""
    # given — 빈 응답
    client, t = make_client({INSTANT: (200, _vector())})
    # when
    metrics.red(client, "svc", RED_W)
    # then
    assert len(t.calls) == 1
    assert "/api/v1/query?" in t.calls[0].url
    params = _params(t.calls[0])
    assert params["query"] == metrics.red_query("svc", RED_W, RED_OFFSET)
    assert float(params["time"]) == RED_W.end


def test_red_returns_red_with_service_offset_and_signals(make_client: ClientFactory) -> None:
    """red 의 Evidence 값은 서비스 · 기준선 offset · R · E · D 세 행을 담은 Red 다."""
    # given — 양쪽 창에 값이 있는 응답
    client, _ = make_client({INSTANT: (200, _red_body(**RED_FULL))})
    # when
    ev = metrics.red(client, "svc", RED_W)
    # then
    assert isinstance(ev.value, metrics.Red)
    assert ev.value.service == "svc"
    assert ev.value.offset == RED_OFFSET
    assert [s.name for s in ev.value.signals] == ["R", "E", "D"]
    assert [s.now for s in ev.value.signals] == [200, pytest.approx(0.05), pytest.approx(0.4)]
    assert ev.value.signals == metrics.signals_of(metrics.parse_metric(_red_body(**RED_FULL)))


def test_red_default_baseline_is_one_day(make_client: ClientFactory) -> None:
    """기준선 없는 Window 를 주면 DEFAULT_BASELINE(1d) — offset 86400s 가 나간다."""
    # given — 기준선 없는 창 · 빈 응답
    client, t = make_client({INSTANT: (200, _vector())})
    # when
    ev = metrics.red(client, "svc", W)
    # then
    assert ev.value.offset == "%ds" % DAY
    assert "offset+%ds" % DAY in t.calls[0].url


def test_red_prev_baseline_offset_is_window_length(make_client: ClientFactory) -> None:
    """--baseline prev 는 직전 창 — offset 이 창 길이(3600s)다."""
    # given — prev 기준선 · 빈 응답
    client, t = make_client({INSTANT: (200, _vector())})
    # when
    ev = metrics.red(client, "svc", W.with_baseline("prev"))
    # then
    assert ev.value.offset == "%ds" % W.seconds
    assert "offset+%ds" % W.seconds in t.calls[0].url


def test_red_rejects_baseline_not_expressible_as_offset(make_client: ClientFactory) -> None:
    """길이가 다른 기준선은 offset 으로 표현할 수 없어 질의 전에 거부한다."""
    # given — 30분짜리 기준선
    client, t = make_client()
    # when · then
    with pytest.raises(ObsError):
        metrics.red(client, "svc", Window(AT - HOUR, AT, baseline=Window(AT - HOUR, AT - 30 * MINUTE)))
    assert t.calls == []


def test_red_requires_service(make_client: ClientFactory) -> None:
    """공백 서비스 이름은 red 가 질의 전에 거부한다 — transport 는 불리지 않는다."""
    # given
    client, t = make_client()
    # when · then
    with pytest.raises(ObsError):
        metrics.red(client, "  ", RED_W)
    assert t.calls == []


def test_top_wraps_query_and_uses_window_end(make_client: ClientFactory) -> None:
    """top 은 topk 로 감싼 질의를 창의 끝 시각에 보낸다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev = metrics.top(client, "up", W, k=10, by=["pod"])
    assert _params(t.calls[0])["query"] == "topk(10, sum by (pod) (up))"
    assert _params(t.calls[0])["time"] == str(W.end)
    assert (ev.value.k, ev.value.by, ev.value.agg) == (10, ["pod"], "sum")


def test_top_clips_to_k_even_if_server_returns_more(make_client: ClientFactory) -> None:
    """서버가 k 보다 많이 돌려줘도 top 이 k 로 자른다 — 상한은 함수의 것."""
    body = _vector(({"pod": "a"}, "4"), ({"pod": "b"}, "3"), ({"pod": "c"}, "2"), ({"pod": "d"}, "1"))
    client, _ = make_client({("metrics", "api/v1/query"): (200, body)})
    ev = metrics.top(client, "up", W, k=3, by=["pod"])
    assert len(ev.value.entries) == 3


def test_top_sorts_entries_by_value_descending(make_client: ClientFactory) -> None:
    """top 의 항목은 값 내림차순이다 — 서버가 준 순서가 아니다."""
    body = _vector(({"pod": "a"}, "1"), ({"pod": "b"}, "3"), ({"pod": "c"}, "2"))
    client, _ = make_client({("metrics", "api/v1/query"): (200, body)})
    ev = metrics.top(client, "up", W, k=3, by=["pod"])
    assert [e.value for e in ev.value.entries] == [3.0, 2.0, 1.0]


def test_top_empty_response_has_no_entries(make_client: ClientFactory) -> None:
    """빈 응답의 top 은 entries 가 빈 목록이다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev = metrics.top(client, "nothing", W)
    assert ev.value.entries == []


def test_metrics_run_compare_window_has_baseline(make_client: ClientFactory) -> None:
    """compare 의 RunResult 의 window 는 --since · --end 의 창에 --baseline 의 기준선이 달린 창이다."""
    # given — 빈 응답
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    # when
    window = cli.metrics_run(client, cli.MetricsArgs(cmd="compare", query="up", since="1h", baseline="prev"), at=AT).window
    # then
    assert window == Window.last("1h", None, at=AT).with_baseline("prev")
    assert window.baseline is not None


def test_metrics_run_top_window_is_end_instant(make_client: ClientFactory) -> None:
    """top 의 RunResult 의 window 는 --end 시각 한 점(start = end)이다 — --since 창이 아니다."""
    # given — 빈 응답 · --end 는 10분 전
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    # when
    window = cli.metrics_run(client, cli.MetricsArgs(cmd="top", query="up", end="-10m"), at=AT).window
    # then
    assert window == Window(AT - 10 * MINUTE, AT - 10 * MINUTE)


# ── 경계값 — check_status · 명시 step · 비유한 값 · 기준선만 있는 시계열 ───────────────

def test_check_status_prints_warnings_to_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """응답의 warnings 는 stderr 에 warning: 으로 한 줄씩 나가고 본문은 그대로 통과한다 — stdout 에는 아무것도 없다."""
    body: dict[str, JSON] = {**_vector(), "warnings": ["w1", "w2"]}
    assert metrics.check_status(body, "metrics") is body
    captured = capsys.readouterr()
    assert captured.err.splitlines() == ["warning: w1", "warning: w2"]
    assert captured.out == ""


def test_check_status_rejects_non_object_body() -> None:
    """본문이 객체가 아니면(문자열) 논리 이름으로 "JSON 이 아닌 응답" ObsError 다."""
    with pytest.raises(ObsError) as ei:
        metrics.check_status("<html>oops</html>", "logs")
    assert str(ei.value).startswith("logs 백엔드가 JSON 이 아닌 응답을 냈다")
    assert "oops" in str(ei.value)


def test_summarize_explicit_step_overrides_auto(make_client: ClientFactory) -> None:
    """summarize 에 step 을 주면 자동값 대신 그 step 이 Summary 와 서브쿼리에 쓰인다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev = metrics.summarize(client, CPU_QUERY, W, step="5m")
    assert ev.value.step == "5m"
    assert _params(t.calls[0])["query"] == metrics.summarize_query(CPU_QUERY, W, "5m")


def test_top_drops_non_finite_values(make_client: ClientFactory) -> None:
    """top 은 NaN · ±Inf · 숫자 아닌 값의 시계열을 항목에서 뺀다 — 부재는 순위에 들지 않는다."""
    body = _vector(({"pod": "a"}, "NaN"), ({"pod": "b"}, "+Inf"), ({"pod": "c"}, "x"), ({"pod": "d"}, "1"))
    client, _ = make_client({("metrics", "api/v1/query"): (200, body)})
    ev = metrics.top(client, "up", W, k=10, by=["pod"])
    assert [e.labels["pod"] for e in ev.value.entries] == ["d"]


def test_compare_baseline_only_series_marks_now_empty(make_client: ClientFactory) -> None:
    """지금 창에는 시계열이 없고 기준선에만 있으면 now_empty 이고 baseline_empty 는 아니며 그 시계열은 now None 으로 실린다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector(_stat("a", "base", "1")))})
    ev = metrics.compare(client, CPU_QUERY, W.with_baseline("1d"))
    assert ev.value.now_empty
    assert not ev.value.baseline_empty
    assert [(d.now, d.base) for d in ev.value.deltas] == [(None, 1.0)]


# ── red_lines — 성질: 시계열 없음 vs 호출 0 · 기준선 없음 · 기준선 0 · 물리 비노출 (모양은 format/test_metrics_lines.py) ────

def test_red_lines_distinguish_missing_from_idle() -> None:
    """지금 창의 시계열 없음과 호출 0 은 다른 문구다 — missing 은 세 행에 "시계열 없음", idle 은 어느 행에도 없고 D 행에 "호출 0" 이다."""
    # given — 6 의 Red(지금 missing) · 7 의 Red(지금 idle)
    missing = cli.red_lines(_red_value(**RED_BASE_ONLY), RED_W)
    idle = cli.red_lines(_red_value(calls_now="0", p95_now="NaN", **RED_BASE_ONLY), RED_W)
    # when
    rows_missing, rows_idle = missing[1:], idle[1:]
    # then
    assert len(rows_missing) == 3
    assert len(rows_idle) == 3
    for line in rows_missing:
        assert "시계열 없음" in line
    for line in rows_idle:
        assert "시계열 없음" not in line
    d_rows = [line for line in rows_idle if line.startswith("D")]
    assert len(d_rows) == 1
    assert "호출 0" in d_rows[0]
    for m, i in zip(rows_missing, rows_idle):
        assert m != i


def test_red_lines_say_baseline_missing_without_ratio() -> None:
    """기준선 창에 시계열이 없으면 세 행에 "기준선 없음" 이 있고 어느 행에도 × 비율이 없다."""
    # given — 9 의 Red
    lines = cli.red_lines(_red_value(**RED_NOW_ONLY), RED_W)
    # when
    rows = lines[1:]
    # then
    assert len(rows) == 3
    for line in rows:
        assert "기준선 없음" in line
        assert "×" not in line


@pytest.mark.parametrize("stats,expected_rows", [
    (dict(calls_base="0", p95_base="NaN", **RED_NOW_ONLY), ["R", "E", "D"]),
    (dict(calls_now="200", errors_now="10", p95_now="0.4", calls_base="100", p95_base="0.2"), ["E"]),
], ids=["idle", "e-zero"])
def test_red_lines_say_baseline_zero_without_ratio(stats: dict[str, str], expected_rows: list[str]) -> None:
    """기준선 값이 0 인 행(기준선 호출 0 이면 셋 · 오류 행만 없으면 E)에 "기준선 0" 이 있고 그 행에 × 비율이 없다."""
    # given — 10 · 11 의 Red
    lines = cli.red_lines(_red_value(**stats), RED_W)
    # when
    zero_rows = [line for line in lines[1:] if "기준선 0" in line]
    # then
    assert [line[0] for line in zero_rows] == expected_rows
    for line in zero_rows:
        assert "×" not in line


def test_red_lines_carry_no_physical() -> None:
    """red 의 렌더링 어디에도 물리 문자열이 없다."""
    # given — 4 의 Red
    lines = cli.red_lines(_red_value(**RED_FULL), RED_W)
    # when · then
    assert len(lines) == 4
    _no_physical("\n".join(lines))


# ── metrics_run — 환원 값을 이름으로 ─────────────────────────────────────────────────

REDUCTION_TYPES: dict[str, type[object]] = {"summarize": metrics.Summary, "compare": metrics.Comparison, "top": metrics.Top, "red": metrics.Red}
"""서브커맨드 → metrics_run 의 evidence.value 가 되는 환원 dataclass."""


@pytest.mark.parametrize("cmd", sorted(REDUCTION_TYPES))
def test_metrics_run_returns_reduction_value_by_subcommand(make_client: ClientFactory, cmd: str) -> None:
    """metrics_run 의 결과는 kind 가 promql-<cmd> 이고 evidence.value 가 그 연산의 환원 dataclass(Summary · Comparison · Top · Red)다 — 줄이 아니다."""
    # given — 빈 vector 라우트
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    # when
    r = cli.metrics_run(client, cli.MetricsArgs(cmd=cmd, query="up", service="svc"), at=AT)
    # then
    assert r.kind == "promql-%s" % cmd
    assert isinstance(r.evidence.value, REDUCTION_TYPES[cmd])


def test_metrics_run_rejects_unknown_subcommand(make_client: ClientFactory) -> None:
    """모르는 서브커맨드의 MetricsArgs 는 metrics_run 이 요청 없이 ObsError 다."""
    # given — 라우트 없는 fake transport
    client, t = make_client()
    # when · then
    with pytest.raises(ObsError):
        cli.metrics_run(client, cli.MetricsArgs(cmd="nope", query="x"), at=AT)
    assert t.calls == []


def _sent_query(call: Call) -> str:
    """나간 호출의 query 파라미터."""
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call.url).query, keep_blank_values=True))["query"]


def test_metrics_run_top_passes_k_and_by(make_client: ClientFactory) -> None:
    """top 의 Args 의 k · by 는 환원 값(Top.k · Top.by)과 나간 질의(topk(k, … by (by) …)) 둘 다에 나타난다."""
    # given — 빈 vector 라우트 · k=2 · by=ns
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    # when
    r = cli.metrics_run(client, cli.MetricsArgs(cmd="top", query="up", k=2, by="ns"), at=AT)
    # then
    assert isinstance(r.evidence.value, metrics.Top)
    assert r.evidence.value.k == 2
    assert r.evidence.value.by == ["ns"]
    sent = _sent_query(t.calls[0])
    assert "topk(2," in sent
    assert "by (ns)" in sent


def test_metrics_run_summarize_passes_step(make_client: ClientFactory) -> None:
    """summarize 의 Args 의 step 은 환원 값(Summary.step)과 나간 서브쿼리의 해상도(…:30s]) 둘 다에 나타난다."""
    # given — 빈 vector 라우트 · step=30s
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    # when
    r = cli.metrics_run(client, cli.MetricsArgs(cmd="summarize", query="up", step="30s"), at=AT)
    # then
    assert isinstance(r.evidence.value, metrics.Summary)
    assert r.evidence.value.step == "30s"
    assert ":30s]" in _sent_query(t.calls[0])
