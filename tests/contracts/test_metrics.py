"""메트릭 환원 연산의 약속 — 질의 조립 · 응답 → 계약 · 없음/0 판정 · 거부. 손 입력(_vector)과 성질 단언만. 렌더링의 모양은 format/test_metrics_lines.py."""
from __future__ import annotations

import math

import pytest

from obs import metrics
from obs.adapters import cli
from obs.core import JSON, ObsError, Window, to_dict
from tests.conftest import ClientFactory


CPU_QUERY: str = 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="careerhub-prod"}[5m]))'
AT: float = 100000.0          # 질의 조립 · 손으로 만든 응답 테스트의 now — fixture replay 는 녹화 at(t.at)을 쓴다


SQ: str = "(%s)[3600s:60s]" % CPU_QUERY


def _vector(*items: tuple[dict[str, str], str]) -> dict[str, JSON]:
    """Prometheus 즉시 조회 응답 모양을 손으로 — (레이블, 값) 목록."""
    result: list[JSON] = [{"metric": dict(labels), "value": [AT, v]} for labels, v in items]
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _stat(pod: str, stat: str, v: str) -> tuple[dict[str, str], str]:
    return {"pod": pod, "stat": stat}, v


# ── 질의 조립 — 계산은 이 문자열로 서버에 넘어간다 ─────────────────────────────────

def test_summarize_query_is_seven_stats_in_one_expression() -> None:
    """일곱 통계가 서브쿼리 `(Q)[창:step]` 위의 *_over_time · deriv 로, `stat` 이름표(label_replace)를 달고 `or` 로 한 식이다."""
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
    """기준선은 같은 식의 `offset` — 지금 p95 와 `offset <기준선>` p95 가 한 식. 두 창은 정의상 같은 길이·해상도다."""
    q = metrics.compare_query(CPU_QUERY, Window.last("1h", at=AT), "60s", "86400s")
    assert q == ('label_replace(quantile_over_time(0.95, %s), "stat", "now", "", "") or '
                 'label_replace(quantile_over_time(0.95, %s offset 86400s), "stat", "base", "", "")' % (SQ, SQ))


def test_auto_step_is_window_over_sixty_with_floor() -> None:
    """step 자동 — 창 길이/60, 최소 15s. promql --range 와 같은 함수다."""
    assert metrics.auto_step(1800) == "30s"
    assert metrics.auto_step(60) == "15s"
    assert metrics.auto_step(86400) == "1440s"
    assert cli.auto_step is metrics.auto_step


def test_duration_literal_is_whole_seconds() -> None:
    """duration_literal 은 초를 PromQL 기간 표기로 — 1초 미만은 0s."""
    assert metrics.duration_literal(3600.0) == "3600s"
    assert metrics.duration_literal(0.4) == "0s"


def test_baseline_offset_rules() -> None:
    """offset 은 창 끝의 차이. 기준선이 없으면 DEFAULT_BASELINE(1d) · prev 는 창 길이 · 길이가 다르거나 뒤에 있는 기준선은 거부(질의 전에)."""
    w = Window.last("1h", at=AT)
    assert metrics.DEFAULT_BASELINE == "1d"
    assert metrics.baseline_offset(w) == (w.with_baseline("1d"), "86400s")
    assert metrics.baseline_offset(w.with_baseline("prev")) == (w.with_baseline("prev"), "3600s")
    assert metrics.baseline_offset(w.with_baseline("1w")) == (w.with_baseline("1w"), "604800s")
    for bad in (Window(0.0, 3600.0, baseline=Window(0.0, 1800.0)), Window(0.0, 3600.0, baseline=Window(100.0, 3700.0))):
        with pytest.raises(ObsError):
            metrics.baseline_offset(bad)


def test_window_baseline_prev_and_serialization() -> None:
    """Window.with_baseline 규약 — 'prev' 는 창 길이만큼 앞, 기간은 그만큼 앞의 같은 시각, 잘못된 spec 은 ObsError. to_dict 에 기준선이 중첩된다."""
    w = Window(1000.0, 1600.0)
    assert w.with_baseline("prev").baseline == Window(400.0, 1000.0)
    assert w.with_baseline("1w").baseline == Window(1000.0 - 604800, 1600.0 - 604800)
    with pytest.raises(ObsError) as ei:
        w.with_baseline("yesterday")
    assert "prev" in str(ei.value)
    assert "yesterday" in str(ei.value)   # 오류 문구가 유효한 후보 전부를 말한다
    d = to_dict(w.with_baseline("prev"))
    assert isinstance(d, dict)
    assert d["baseline"] == {"start": 400.0, "end": 1000.0, "baseline": None}


# ── 응답 → 계약 — 계산 없이 판정만 ───────────────────────────────────────────────

def test_split_stats_drops_non_finite_and_strips_stat_label() -> None:
    """`stat` 이름표로 시계열별 {통계: 값}. NaN · +Inf · 숫자 아님 · 이름표 없음은 부재로 빠진다. 0 은 값이다."""
    res = metrics.parse_metric(metrics.check_status(_vector(
        _stat("a", "n", "60"), _stat("a", "p95", "NaN"), _stat("a", "min", "0"), _stat("a", "max", "+Inf"),
        _stat("b", "p95", "x"), ({"pod": "c"}, "1")), "metrics"))
    assert metrics.split_stats(res) == {(("pod", "a"),): {"n": 60.0, "min": 0.0}}


def test_summaries_of_requires_n_and_marks_missing_as_nan() -> None:
    """n 이 없는 시계열은 부재(목록에서 빠진다). 있는데 다른 통계가 부재면 그 값은 NaN — 0 이 아니다."""
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
    client, _ = make_client()
    with pytest.raises(ObsError):
        metrics.summarize(client, "  ", Window.last("1h", at=AT))


def test_compare_rejects_baseline_not_expressible_as_offset(make_client: ClientFactory) -> None:
    """길이가 다른 기준선은 offset 으로 표현할 수 없다 — 질의 전에 거부."""
    client, t = make_client()
    with pytest.raises(ObsError):
        metrics.compare(client, CPU_QUERY, Window(0.0, 3600.0, baseline=Window(0.0, 1800.0)))
    assert t.calls == []


def test_compare_missing_series_and_zero_do_not_divide(make_client: ClientFactory) -> None:
    """AC 4 — 시계열 단위: 기준선에 없는 시계열은 "기준선 없음"(base None), 기준선이 0 인 시계열은 비율 없음(base 는 있다),
    지금 창에만 없는 시계열은 "지금 없음". 어느 경우도 ZeroDivisionError 없이 절대 차이는 남는다. 순서: 비율 → 기준선 0 → 기준선 없음 → 지금 없음."""
    body = _vector(_stat("a", "now", "0.4"), _stat("a", "base", "0"), _stat("b", "now", "1"), _stat("c", "base", "3"),
                   _stat("d", "now", "3.9"), _stat("d", "base", "1"))
    client, _ = make_client({("metrics", "api/v1/query"): (200, body)})
    ev, _ = metrics.compare(client, "q", Window.last("1h", at=AT).with_baseline("1d"))
    by = {d.labels["pod"]: d for d in ev.value.deltas}
    assert [d.labels["pod"] for d in ev.value.deltas] == ["d", "a", "b", "c"]

    def fields(d: metrics.Delta) -> tuple[float | None, float | None, float | None, float | None, bool]:
        return d.now, d.base, d.ratio, d.delta, d.base_is_zero

    assert fields(by["a"]) == (0.4, 0.0, None, pytest.approx(0.4), True)     # 기준선 0 — 비율 없음, Δ 는 남는다
    assert fields(by["b"]) == (1.0, None, None, None, False)                 # 기준선 없음
    assert fields(by["c"]) == (None, 3.0, None, None, False)                 # 지금 없음
    assert fields(by["d"]) == (3.9, 1.0, pytest.approx(3.9), pytest.approx(2.9), False)   # 보통 — 비율·Δ
    assert not ev.value.now_empty
    assert not ev.value.baseline_empty
    lines = cli.compare_lines(ev.value, ev.window or Window(0, 0))
    assert lines[1].startswith("×3.9  Δ+2.9  now p95=3.9  base p95=1  {pod=d}")
    assert lines[2].startswith("×—  Δ+0.4  now p95=0.4  base p95=0  (기준선 0)  {pod=a}")
    assert lines[3] == "기준선 없음  now p95=1  {pod=b}"
    assert lines[4] == "지금 없음  base p95=3  {pod=c}"


def test_top_query_wraps_topk_over_aggregation_by_axes() -> None:
    """top_query 는 topk(k, <agg> by (<축들>) (q)) — 축은 여럿이고 공백은 다듬는다."""
    assert metrics.top_query("up", 5, ["namespace", " pod "], "max") == "topk(5, max by (namespace, pod) (up))"


@pytest.mark.parametrize("k,by,agg", [(0, ["pod"], "sum"), (5, ["pod"], "p95"), (5, [" "], "sum")])
def test_top_query_rejects_bad_k_agg_or_empty_axis(k: int, by: list[str], agg: str) -> None:
    """k < 1 · 정해지지 않은 집계 · 빈 축은 질의 전에 거부한다."""
    with pytest.raises(ObsError):
        metrics.top_query("up", k, by, agg)
