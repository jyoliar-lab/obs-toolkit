"""메트릭 환원 연산의 약속 — 질의 조립 · 응답 → 계약 · 없음/0 판정 · 거부. 손 입력(_vector)과 성질 단언만. 렌더링의 모양은 format/test_metrics_lines.py."""
from __future__ import annotations

import json
import math

import pytest

from obs import metrics
from obs.adapters import cli
from obs.core import JSON, Client, Evidence, Lines, ObsError, Window, to_json
from tests.conftest import AT, DAY, HOUR, MINUTE, WEEK, ClientFactory, _stat, _vector
from tests.record import CPU_QUERY


SQ: str = "(%s)[%ds:%ds]" % (CPU_QUERY, HOUR, MINUTE)   # 1h 창 · 60s step 의 서브쿼리



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
    ev, _ = metrics.compare(client, "q", Window.last("1h", at=AT).with_baseline("1d"))
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


def _run(client: Client, op: str) -> Evidence[metrics.Summary] | Evidence[metrics.Comparison] | Evidence[metrics.Top]:
    """세 연산을 같은 창으로 부른다 — 약속 검사의 공통 입구."""
    if op == "summarize":
        return metrics.summarize(client, CPU_QUERY, W)[0]
    if op == "compare":
        return metrics.compare(client, CPU_QUERY, W.with_baseline("1d"))[0]
    return metrics.top(client, "up", W, k=3, by=["pod"])[0]


@pytest.mark.parametrize("op", ["summarize", "compare", "top"])
def test_each_operation_makes_one_get(make_client: ClientFactory, op: str) -> None:
    """연산당 GET 은 1회다 — 계산은 서버가 한다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    _run(client, op)
    assert len(t.calls) == 1


@pytest.mark.parametrize("op", ["summarize", "compare", "top"])
def test_each_operation_serializes_to_json(make_client: ClientFactory, op: str) -> None:
    """환원물이 든 Evidence 는 to_json 으로 직렬화된다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    json.loads(to_json(_run(client, op)))


def test_summarize_evidence_carries_assembled_query_window_and_step(make_client: ClientFactory) -> None:
    """summarize 의 Evidence.query 는 조립 질의(재실행 가능)이고 window 는 받은 창, step 은 자동값이다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev, _ = metrics.summarize(client, CPU_QUERY, W)
    assert ev.backend == "metrics"
    assert ev.query == metrics.summarize_query(CPU_QUERY, W, "60s")
    assert ev.window == W
    assert ev.value.step == "60s"


def test_summarize_sorts_series_by_labels(make_client: ClientFactory) -> None:
    """summarize 의 시계열은 레이블 순이다 — 서버가 준 순서가 아니다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector(_stat("b", "n", "1"), _stat("a", "n", "1")))})
    ev, _ = metrics.summarize(client, CPU_QUERY, W)
    assert [s.labels["pod"] for s in ev.value.series] == ["a", "b"]


def test_summarize_empty_response_has_no_series(make_client: ClientFactory) -> None:
    """빈 응답의 summarize 는 series 가 빈 목록이다 — 0 이 아니다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev, _ = metrics.summarize(client, CPU_QUERY, W)
    assert ev.value.series == []


def test_compare_default_baseline_is_one_day(make_client: ClientFactory) -> None:
    """기준선 없는 Window 를 주면 DEFAULT_BASELINE(1d) — Evidence.window 에 하루 앞 창이 달리고 offset 86400s 가 나간다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev, _ = metrics.compare(client, CPU_QUERY, W)
    assert ev.window is not None
    assert ev.window.baseline == Window(ev.window.start - DAY, ev.window.end - DAY)
    assert ev.value.offset == "%ds" % DAY
    assert "offset+%ds" % DAY in t.calls[0].url


def test_compare_prev_baseline(make_client: ClientFactory) -> None:
    """--baseline prev 는 직전 창 — offset 이 창 길이(3600s)다."""
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev, _ = metrics.compare(client, CPU_QUERY, W.with_baseline("prev"))
    assert ev.window is not None
    assert ev.window.baseline == Window(ev.window.start - ev.window.seconds, ev.window.end - ev.window.seconds)
    assert ev.value.offset == "%ds" % ev.window.seconds
    assert "offset+%ds" % ev.window.seconds in t.calls[0].url


def test_compare_both_empty(make_client: ClientFactory) -> None:
    """양쪽 다 비면 now_empty · baseline_empty 이고 deltas 는 빈 목록이다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev, _ = metrics.compare(client, CPU_QUERY, W)
    assert ev.value.now_empty
    assert ev.value.baseline_empty
    assert ev.value.deltas == []


def test_top_wraps_query_and_uses_window_end(make_client: ClientFactory) -> None:
    """top 의 Evidence.query 는 topk 로 감싼 질의이고 창은 끝 시각 한 점이다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev, _ = metrics.top(client, "up", W, k=10, by=["pod"])
    assert ev.query == "topk(10, sum by (pod) (up))"
    assert ev.window == Window(AT, AT)
    assert (ev.value.k, ev.value.by, ev.value.agg) == (10, ["pod"], "sum")


def test_top_clips_to_k_even_if_server_returns_more(make_client: ClientFactory) -> None:
    """서버가 k 보다 많이 돌려줘도 top 이 k 로 자른다 — 상한은 함수의 것."""
    body = _vector(({"pod": "a"}, "4"), ({"pod": "b"}, "3"), ({"pod": "c"}, "2"), ({"pod": "d"}, "1"))
    client, _ = make_client({("metrics", "api/v1/query"): (200, body)})
    ev, _ = metrics.top(client, "up", W, k=3, by=["pod"])
    assert len(ev.value.entries) == 3


def test_top_sorts_entries_by_value_descending(make_client: ClientFactory) -> None:
    """top 의 항목은 값 내림차순이다 — 서버가 준 순서가 아니다."""
    body = _vector(({"pod": "a"}, "1"), ({"pod": "b"}, "3"), ({"pod": "c"}, "2"))
    client, _ = make_client({("metrics", "api/v1/query"): (200, body)})
    ev, _ = metrics.top(client, "up", W, k=3, by=["pod"])
    assert [e.value for e in ev.value.entries] == [3.0, 2.0, 1.0]


def test_top_empty_response_has_no_entries(make_client: ClientFactory) -> None:
    """빈 응답의 top 은 entries 가 빈 목록이다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev, _ = metrics.top(client, "nothing", W)
    assert ev.value.entries == []


def test_metrics_run_dispatches_by_subcommand(make_client: ClientFactory) -> None:
    """metrics_run 은 세 서브커맨드를 Evidence[Lines] 와 kind 로 내고 모르는 서브커맨드는 ObsError 다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    for cmd, kind in (("summarize", "promql-summarize"), ("compare", "promql-compare"), ("top", "promql-top")):
        ev, _, got = cli.metrics_run(client, cli.MetricsArgs(cmd=cmd, query="up"), at=AT)
        assert got == kind
        assert isinstance(ev.value, Lines)
    with pytest.raises(ObsError):
        cli.metrics_run(client, cli.MetricsArgs(cmd="red", query="x"), at=AT)


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
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    ev, _ = metrics.summarize(client, CPU_QUERY, W, step="5m")
    assert ev.value.step == "5m"
    assert ev.query == metrics.summarize_query(CPU_QUERY, W, "5m")


def test_top_drops_non_finite_values(make_client: ClientFactory) -> None:
    """top 은 NaN · ±Inf · 숫자 아닌 값의 시계열을 항목에서 뺀다 — 부재는 순위에 들지 않는다."""
    body = _vector(({"pod": "a"}, "NaN"), ({"pod": "b"}, "+Inf"), ({"pod": "c"}, "x"), ({"pod": "d"}, "1"))
    client, _ = make_client({("metrics", "api/v1/query"): (200, body)})
    ev, _ = metrics.top(client, "up", W, k=10, by=["pod"])
    assert [e.labels["pod"] for e in ev.value.entries] == ["d"]


def test_compare_baseline_only_series_marks_now_empty(make_client: ClientFactory) -> None:
    """지금 창에는 시계열이 없고 기준선에만 있으면 now_empty 이고 baseline_empty 는 아니며 그 시계열은 now None 으로 실린다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector(_stat("a", "base", "1")))})
    ev, _ = metrics.compare(client, CPU_QUERY, W.with_baseline("1d"))
    assert ev.value.now_empty
    assert not ev.value.baseline_empty
    assert [(d.now, d.base) for d in ev.value.deltas] == [(None, 1.0)]
