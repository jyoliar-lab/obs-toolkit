"""obs.metrics — 메트릭 환원 연산: summarize · compare · top (metrics 백엔드, GET 만).

원본(시계열)은 여기서 환원되고, 부른 쪽은 요약 dataclass 만 받는다 — "이 값이 높은가" 를 판단하는 데
시계열 두 벌이 컨텍스트에 실리지 않는다: "now p95=0.42, base p95=0.05, ×8.4".

구조로 거는 것:
  - 순수 함수다 — HTTP 는 `Client` 주입(`Client.get` 만 부른다), fixture 만으로 테스트된다.
  - 모든 연산은 `Window` 를 받고 `(Evidence[V], 원본)` 을 돌려준다. 원본은 어댑터가 spill 한다(CLI → raw.json).
  - "데이터 없음" 과 "0" 을 구분한다 — 시계열이 없으면 목록이 비고(`series == []` · `entries == []`),
    값이 0 이면 0 이 실린다. 기준선 시계열이 없으면 `Delta.base is None`("기준선 없음"), 0 이면 `ratio is None`
    이되 `base` 는 있다 — 어느 쪽도 0 으로 나누지 않는다.
  - 기준선 창의 규약(#102 의 `red` 가 그대로 쓴다): 기준선은 `Window.baseline` 필드이고 `Window.with_baseline(spec)`
    으로 단다. spec 은 `1d`(1일 전 같은 시각) · `1w`(1주 전) 같은 기간 또는 `prev`(직전 창). 기본값 `DEFAULT_BASELINE`
    = `1d` — 주기성이 있는 서비스 지표에 맞다. 직전 창은 급변 감지용.
  - `compare` 의 두 질의는 같은 step 이다 — 창 길이가 같고 step 이 같아야 점 수가 같다. 길이가 다른 기준선은 거부한다.
  - `top` 은 카디널리티 폭발을 서버에서 막는다 — `topk(k, <agg> by (<by>) (<query>))` 로 감싸 k 개만 받는다.
    원본이 이미 k 줄이라 stdout 도 파일도 작다.

Prometheus 형 응답(`{status, data: {resultType, result}}`)의 파서(`parse_metric` · `check_status`)도 여기 산다 —
Loki 의 메트릭 질의 응답이 같은 형상이라 로그 어댑터도 이것을 쓴다.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Sequence

from obs.core import (
    JSON, Client, Evidence, ObsError, Params, Window, json_labels, json_list, json_obj, json_pair, json_str, json_strs,
)

DEFAULT_BASELINE: str = "1d"   # 기준선 창 기본값 — 1일 전 같은 시각. `prev` 는 직전 창, `1w` 는 1주 전
DEFAULT_TOP_K: int = 10
DEFAULT_TOP_AGG: str = "sum"
TOP_AGGS: tuple[str, ...] = ("sum", "max", "min", "avg", "count")
MIN_STEP_SEC: int = 15


# ── 응답 dataclass + 파서 — Prometheus 형 ────────────────────────────────────────

@dataclass(frozen=True)
class Series:
    """시계열 하나 — Prometheus·Loki 메트릭 응답의 vector/matrix 항목."""
    labels: dict[str, str]
    values: list[tuple[float, str]]   # (unix 초, 값 문자열)


@dataclass(frozen=True)
class MetricResult:
    """`{status, data: {resultType, result}}` 형 응답."""
    result_type: str            # vector · matrix · scalar · string
    scalar: str | None          # scalar/string 일 때 값
    series: list[Series]


def check_status(body: JSON, backend: str) -> dict[str, JSON]:
    """Prometheus·Loki 형 응답의 `status: error` 를 논리 이름의 오류로. 통과하면 객체를 돌려준다."""
    if not isinstance(body, dict):
        text = json_str(body).replace("\n", "⏎")
        raise ObsError("%s 백엔드가 JSON 이 아닌 응답을 냈다: %s" % (backend, text if len(text) <= 300 else text[:299] + "…"))
    if body.get("status") == "error":
        raise ObsError("%s 백엔드 질의 오류(%s): %s" % (backend, json_str(body.get("errorType")), json_str(body.get("error"))))
    for w in json_strs(body.get("warnings")):
        print("warning: %s" % w, file=sys.stderr)
    return body


def series_items(result: JSON) -> list[Series]:
    out: list[Series] = []
    for item in json_list(result):
        o = json_obj(item)
        pairs: list[tuple[str, str]] = []
        single = json_pair(o.get("value"))
        if single is not None:
            pairs.append(single)
        for v in json_list(o.get("values")):
            pr = json_pair(v)
            if pr is not None:
                pairs.append(pr)
        values: list[tuple[float, str]] = []
        for ts, val in pairs:
            try:
                values.append((float(ts), val))
            except ValueError:
                continue
        out.append(Series(labels=json_labels(o.get("metric")), values=values))
    return out


def parse_metric(body: dict[str, JSON]) -> MetricResult:
    data = json_obj(body.get("data"))
    rtype = json_str(data.get("resultType"))
    result = data.get("result")
    if rtype in ("scalar", "string"):
        pr = json_pair(result)
        return MetricResult(result_type=rtype, scalar=pr[1] if pr else None, series=[])
    return MetricResult(result_type=rtype, scalar=None, series=series_items(result))


# ── 환원물 dataclass ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SeriesSummary:
    """시계열 하나의 요약 — 값 목록 대신 이것이 컨텍스트에 실린다."""
    labels: dict[str, str]
    n: int          # 유한한 값의 수 — NaN · ±Inf 는 세지 않는다(값이 아니라 부재)
    min: float
    p50: float
    p95: float
    max: float
    last: float     # 창 안 마지막 유한값
    slope: float    # 최소제곱 기울기(값/초) — 창 안의 추세. 점이 하나면 0


@dataclass(frozen=True)
class Summary:
    """`summarize` 의 값 — 시계열별 요약. `series` 가 비면 데이터 없음(0 이 아니다)."""
    step: str
    series: list[SeriesSummary]


@dataclass(frozen=True)
class Delta:
    """시계열 하나의 지금 창 vs 기준선 창. 비교 통계는 p95 다.

    - `base is None` → 기준선 창에 이 시계열이 없다("기준선 없음"). `now is None` → 지금 창에 없다("지금 없음").
    - `ratio` = now.p95 / base.p95 — 둘 다 있고 base.p95 가 0 이 아닐 때만. 0 으로 나누지 않는다.
    - `delta` = now.p95 - base.p95 — 둘 다 있을 때."""
    labels: dict[str, str]
    now: SeriesSummary | None
    base: SeriesSummary | None
    ratio: float | None
    delta: float | None

    @property
    def base_is_zero(self) -> bool:
        return self.now is not None and self.base is not None and self.base.p95 == 0


@dataclass(frozen=True)
class Comparison:
    """`compare` 의 값. `deltas` 는 비율 내림차순, 그 뒤에 기준선 0 · 기준선 없음 · 지금 없음 순.
    `now_empty` / `baseline_empty` 는 창 전체에 시계열이 하나도 없었다는 뜻(0 이 아니라 부재)."""
    step: str
    deltas: list[Delta]
    now_empty: bool
    baseline_empty: bool


@dataclass(frozen=True)
class TopEntry:
    labels: dict[str, str]
    value: float


@dataclass(frozen=True)
class Top:
    """`top` 의 값 — 레이블 축 상위 k. `entries` 는 값 내림차순이고 k 개를 넘지 않는다. 비면 데이터 없음."""
    k: int
    by: list[str]
    agg: str
    entries: list[TopEntry]


# ── 계산 — 순수 ──────────────────────────────────────────────────────────────────

def auto_step(range_sec: float) -> str:
    """창 길이에서 step 을 정한다 — range/60 에 가까운 정수 초, 최소 15s. 창당 점 수가 ~60 으로 일정하다."""
    return "%ds" % max(MIN_STEP_SEC, int(range_sec / 60))


def percentile(sorted_vals: Sequence[float], p: float) -> float:
    """정렬된 값의 p(0~1) 분위수 — 이웃 두 값을 선형 보간한다(numpy 기본과 같다). 빈 입력은 호출자가 막는다."""
    if not sorted_vals:
        raise ValueError("빈 목록의 분위수")
    pos = p * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def slope(points: Sequence[tuple[float, float]]) -> float:
    """(t, v) 점들의 최소제곱 기울기(값/초). 점이 둘 미만이거나 t 가 전부 같으면 0."""
    n = len(points)
    if n < 2:
        return 0.0
    t0 = points[0][0]
    ts = [t - t0 for t, _ in points]
    vs = [v for _, v in points]
    mt = sum(ts) / n
    mv = sum(vs) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    if sxx == 0:
        return 0.0
    sxy = sum((t - mt) * (v - mv) for t, v in zip(ts, vs))
    return sxy / sxx


def finite_points(s: Series) -> list[tuple[float, float]]:
    """(unix 초, 값) 중 유한한 값만 — NaN · ±Inf · 숫자 아님은 부재로 본다."""
    out: list[tuple[float, float]] = []
    for ts, raw in s.values:
        try:
            v = float(raw)
        except ValueError:
            continue
        if math.isfinite(v):
            out.append((ts, v))
    return out


def summarize_series(s: Series) -> SeriesSummary | None:
    """시계열 하나 → 요약. 유한한 값이 하나도 없으면 None(데이터 없음)."""
    pts = finite_points(s)
    if not pts:
        return None
    vals = sorted(v for _, v in pts)
    return SeriesSummary(
        labels=dict(s.labels), n=len(vals), min=vals[0], p50=percentile(vals, 0.5), p95=percentile(vals, 0.95),
        max=vals[-1], last=pts[-1][1], slope=slope(pts),
    )


def summarize_result(res: MetricResult) -> list[SeriesSummary]:
    """응답의 시계열 전부를 요약한다 — 유한값이 없는 시계열은 빠진다(데이터 없음은 목록에 실리지 않는다)."""
    out: list[SeriesSummary] = []
    for s in res.series:
        summary = summarize_series(s)
        if summary is not None:
            out.append(summary)
    return out


def series_key(labels: dict[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted(labels.items()))


def diff_summaries(now: Sequence[SeriesSummary], base: Sequence[SeriesSummary]) -> list[Delta]:
    """레이블 집합이 같은 시계열끼리 짝지어 차이만 낸다. 순서: 비율 내림차순 → 기준선 0 → 기준선 없음 → 지금 없음."""
    now_by = {series_key(s.labels): s for s in now}
    base_by = {series_key(s.labels): s for s in base}
    deltas: list[Delta] = []
    for key in list(now_by) + [k for k in base_by if k not in now_by]:
        n, b = now_by.get(key), base_by.get(key)
        ratio: float | None = None
        delta: float | None = None
        if n is not None and b is not None:
            delta = n.p95 - b.p95
            if b.p95 != 0:
                ratio = n.p95 / b.p95
        deltas.append(Delta(labels=dict(key), now=n, base=b, ratio=ratio, delta=delta))

    def order(d: Delta) -> tuple[int, float]:
        if d.ratio is not None:
            return (0, -d.ratio)
        if d.base_is_zero:
            return (1, 0.0)
        if d.base is None and d.now is not None:
            return (2, 0.0)
        return (3, 0.0)

    deltas.sort(key=order)
    return deltas


def top_query(query: str, k: int, by: Sequence[str], agg: str) -> str:
    """`topk(k, <agg> by (<by>) (<query>))` — 서버가 k 개만 돌려준다. 카디널리티는 여기서 막힌다."""
    if k < 1:
        raise ObsError("k 는 1 이상이어야 한다: %d" % k)
    if agg not in TOP_AGGS:
        raise ObsError("알 수 없는 집계 %r — %s 중 하나" % (agg, " · ".join(TOP_AGGS)))
    axes = [a.strip() for a in by if a.strip()]
    if not axes:
        raise ObsError("레이블 축(--by)이 필요하다 — 예: pod · namespace,pod")
    return "topk(%d, %s by (%s) (%s))" % (k, agg, ", ".join(axes), query)


# ── 연산 — Client 주입, Window 를 받고 Evidence 를 돌려준다 ─────────────────────────

def _range(client: Client, query: str, window: Window, step: str) -> dict[str, JSON]:
    params: Params = {"query": query, "start": window.start, "end": window.end, "step": step}
    return check_status(client.get("metrics", "api/v1/query_range", params).body, "metrics")


def summarize(client: Client, query: str, window: Window, step: str | None = None) -> tuple[Evidence[Summary], JSON]:
    """창 안의 구간 조회를 시계열별 요약(n · min · p50 · p95 · max · last · 기울기)으로."""
    if not query.strip():
        raise ObsError("PromQL 이 필요하다")
    step = step or auto_step(window.seconds)
    body = _range(client, query, window, step)
    value = Summary(step=step, series=summarize_result(parse_metric(body)))
    return Evidence(backend="metrics", query=query, window=window, value=value), body


def compare(client: Client, query: str, window: Window, step: str | None = None) -> tuple[Evidence[Comparison], JSON]:
    """같은 질의를 지금 창과 기준선 창(`window.baseline` — 없으면 DEFAULT_BASELINE 으로 단다)에 **같은 step** 으로
    던져 시계열별 차이만 낸다. 원본은 {"now": …, "baseline": …}."""
    if not query.strip():
        raise ObsError("PromQL 이 필요하다")
    w = window if window.baseline is not None else window.with_baseline(DEFAULT_BASELINE)
    base = w.baseline
    assert base is not None
    if abs(base.seconds - w.seconds) > 1e-6:
        raise ObsError("기준선 창 길이(%.0fs)가 지금 창(%.0fs)과 다르다 — 같은 step 으로 정렬할 수 없어 비교가 틀린다. "
                       "Window.with_baseline 으로 단다." % (base.seconds, w.seconds))
    step = step or auto_step(w.seconds)
    now_body = _range(client, query, w, step)
    base_body = _range(client, query, base, step)
    now_res, base_res = parse_metric(now_body), parse_metric(base_body)
    value = Comparison(
        step=step, deltas=diff_summaries(summarize_result(now_res), summarize_result(base_res)),
        now_empty=not now_res.series, baseline_empty=not base_res.series,
    )
    raw: JSON = {"now": now_body, "baseline": base_body}
    return Evidence(backend="metrics", query=query, window=w, value=value), raw


def top(client: Client, query: str, window: Window, k: int = DEFAULT_TOP_K, by: Sequence[str] = ("pod",),
        agg: str = DEFAULT_TOP_AGG) -> tuple[Evidence[Top], JSON]:
    """레이블 축 상위 k — `window.end` 시각의 즉시 조회. 질의를 `topk(k, agg by (by) (query))` 로 감싸 서버가 k 개만
    돌려주게 한다. Evidence.query 는 감싼 질의(그대로 재실행 가능), window 는 평가 시각(start = end)."""
    if not query.strip():
        raise ObsError("PromQL 이 필요하다")
    wrapped = top_query(query, k, by, agg)
    body = check_status(client.get("metrics", "api/v1/query", {"query": wrapped, "time": window.end}).body, "metrics")
    entries: list[TopEntry] = []
    for s in parse_metric(body).series:
        pts = finite_points(s)
        if pts:
            entries.append(TopEntry(labels=dict(s.labels), value=pts[-1][1]))
    entries.sort(key=lambda e: -e.value)
    value = Top(k=k, by=[a.strip() for a in by if a.strip()], agg=agg, entries=entries[:k])
    return Evidence(backend="metrics", query=wrapped, window=Window(start=window.end, end=window.end), value=value), body
