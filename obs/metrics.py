"""obs.metrics — 메트릭 환원 연산: summarize · compare · top · red (metrics 백엔드, GET 만).

계산은 백엔드(PromQL)가 한다 — 코어는 질의를 조립하고 응답을 계약(dataclass)에 담으며 "데이터 없음" · "기준선 없음" · 0 을
가른다. 원본(시계열)은 서버를 떠나지 않고, 부른 쪽은 시계열당 값 몇 개만 받는다: "now p95=0.42, base p95=0.05, ×8.4".

구조로 거는 것:
  - 순수 함수다 — HTTP 는 `Client` 주입(`Client.get` 만 부른다), fixture(서버 환원 응답)만으로 테스트된다.
  - 모든 연산은 `Window` 를 받고 `Evidence[V]` 를 돌려준다. 보낸 호출의 기록은 `Client.log` 에 있고 어댑터가 spill 한다(CLI → raw.json).
  - 통계는 **한 질의**로 묶는다 — `label_replace(<stat>, "stat", "<이름>", "", "") or …` 를 `/api/v1/query` 한 번에.
    통계마다 질의를 따로 던지면 예산(`core.MAX_QUERIES`)이 연산 한 번에 바닥난다. 연산당 GET 은 1 회다.
  - `summarize` 의 일곱 값은 서브쿼리 `(Q)[<창 길이>:<step>]` 위의 `count_over_time` · `min_over_time` ·
    `quantile_over_time(0.5 / 0.95)` · `max_over_time` · `last_over_time` · `deriv`(단순 선형 회귀 = 최소제곱 기울기, 값/초).
    step 은 창 길이에서 자동(`auto_step`) — 서브쿼리 해상도다.
  - `compare` 의 기준선은 **같은 식의 `offset`** 이다 — 두 창은 정의상 같은 길이 · 같은 해상도라 "같은 step 정렬" 이
    따로 필요 없다. `offset` 이 보존 기간 밖이면 기준선 시계열이 부재로 온다 — "기준선 없음" 이 서버 응답만으로 선다.
  - "데이터 없음" 과 "0" 을 구분한다 — 시계열이 없으면 목록이 비고(`series == []` · `entries == []`), 값이 0 이면 0 이
    실린다. 기준선 시계열이 없으면 `Delta.base is None`("기준선 없음"), 0 이면 `ratio is None` 이되 `base` 는 있다 —
    어느 쪽도 0 으로 나누지 않는다. NaN 은 값이 아니라 부재다.
  - 기준선 창의 규약(#102 의 `red` 가 그대로 쓴다): 기준선은 `Window.baseline` 필드이고 `Window.with_baseline(spec)`
    으로 단다. spec 은 `1d`(1일 전 같은 시각) · `1w`(1주 전) 같은 기간 또는 `prev`(직전 창). 기본값 `DEFAULT_BASELINE`
    = `1d` — 주기성이 있는 서비스 지표에 맞다. 직전 창은 급변 감지용. 기준선 창은 `offset` 으로 옮겨진다(창 끝의 차이).
  - `top` 은 카디널리티 폭발을 서버에서 막는다 — `topk(k, <agg> by (<by>) (<query>))` 로 감싸 k 개만 받는다.

Prometheus 형 응답(`{status, data: {resultType, result}}`)의 파서(`parse_metric` · `check_status`)도 여기 산다 —
Loki 의 메트릭 질의 응답이 같은 형상이라 로그 어댑터도 이것을 쓴다.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from typing import Sequence

from obs.core import (
    JSON, Client, Evidence, ObsError, Window, json_labels, json_list, json_obj, json_pair, json_str, json_strs,
)

DEFAULT_BASELINE: str = "1d"   # 기준선 창 기본값 — 1일 전 같은 시각. `prev` 는 직전 창, `1w` 는 1주 전
STAT_LABEL: str = "stat"       # 한 질의로 묶은 통계를 가르는 레이블 — label_replace 가 붙이고 파서가 뗀다
SUMMARY_STATS: tuple[str, ...] = ("n", "min", "p50", "p95", "max", "last", "slope")
DEFAULT_TOP_K: int = 10
DEFAULT_TOP_AGG: str = "sum"
TOP_AGGS: tuple[str, ...] = ("sum", "max", "min", "avg", "count")
MIN_STEP_SEC: int = 15

# spanmetrics(Alloy spanmetrics connector → Prometheus) 의 메트릭 · 라벨 이름 — obs.traces(exemplars · service_graph)와 red 가 같이 쓴다.
# 이름은 한 벌이다 — traces 가 metrics 를 import 하므로 자리는 여기(아래층).
SPAN_METRIC: str = "traces_span_metrics_duration_seconds"   # spanmetrics native histogram
CALLS_METRIC: str = "traces_span_metrics_calls_total"
SERVER_KIND: str = 'span_kind="SPAN_KIND_SERVER"'      # spanmetrics 의 서버 span 행 — 임계 · callee · red
CLIENT_KIND: str = 'span_kind="SPAN_KIND_CLIENT"'      # spanmetrics 의 클라이언트 span 행 — 엣지

RED_QUANTILE: float = 0.95                          # D 의 분위수 — 고정
RED_STATS: tuple[str, ...] = ("calls_now", "calls_base", "errors_now", "errors_base", "p95_now", "p95_base")
RED_SIGNALS: tuple[str, ...] = ("R", "E", "D")      # 행 순서 — 항상 셋
RED_MISSING: str = "missing"                        # 그 창에 호출 카운터 시계열이 없다
RED_IDLE: str = "idle"                              # 시계열은 있고 창 안 호출 0 — R·E 는 값 0, D 는 산출 불가(None)
RED_VALUE: str = "value"


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
    """시계열 하나의 요약 — 서버가 계산한 일곱 값. 값 목록 대신 이것이 컨텍스트에 실린다. 서버가 못 낸 통계는 NaN(부재)."""
    labels: dict[str, str]
    n: int          # count_over_time — 서브쿼리 점 수
    min: float
    p50: float      # quantile_over_time(0.5)
    p95: float      # quantile_over_time(0.95)
    max: float
    last: float     # last_over_time
    slope: float    # deriv — 단순 선형 회귀 기울기(값/초). 창 안의 추세


@dataclass(frozen=True)
class Summary:
    """`summarize` 의 값 — 시계열별 요약(레이블 순). `series` 가 비면 데이터 없음(0 이 아니다)."""
    step: str
    series: list[SeriesSummary]


@dataclass(frozen=True)
class Delta:
    """시계열 하나의 지금 창 vs 기준선 창 — 서버가 낸 두 p95 의 차이만.

    - `base is None` → 기준선 창(offset)에 이 시계열이 없다("기준선 없음"). `now is None` → 지금 창에 없다("지금 없음").
    - `ratio` = now / base — 둘 다 있고 base 가 0 이 아닐 때만. 0 으로 나누지 않는다.
    - `delta` = now - base — 둘 다 있을 때."""
    labels: dict[str, str]
    now: float | None
    base: float | None
    ratio: float | None
    delta: float | None

    @property
    def base_is_zero(self) -> bool:
        return self.now is not None and self.base is not None and self.base == 0


@dataclass(frozen=True)
class Comparison:
    """`compare` 의 값. `deltas` 는 비율 내림차순, 그 뒤에 기준선 0 · 기준선 없음 · 지금 없음 순.
    `now_empty` / `baseline_empty` 는 창 전체에 시계열이 하나도 없었다는 뜻(0 이 아니라 부재)."""
    step: str
    offset: str        # 기준선 offset(PromQL 기간 표기, 예: 86400s)
    deltas: list[Delta]
    now_empty: bool
    baseline_empty: bool


@dataclass(frozen=True)
class Signal:
    """R · E · D 한 행. 값이 None 이면 산출이 없고 이유는 *_state 다."""
    name: str                 # R · E · D
    now: float | None         # None = missing, 또는 D 의 idle
    base: float | None        # None = missing, 또는 D 의 idle
    ratio: float | None       # now / base — 둘 다 value 이고 base ≠ 0 일 때만. 0 으로 나누지 않는다
    now_state: str            # RED_MISSING · RED_IDLE · RED_VALUE
    base_state: str           # 같은 셋 — "기준선 없음" = missing · "기준선 0" = idle, 또는 value 이되 0


@dataclass(frozen=True)
class Red:
    """`red` 의 값 — 서비스 하나의 R · E · D. `signals` 는 항상 세 행, 이 순서. 시계열이 없어도 비지 않는다(각 행이 missing)."""
    service: str
    offset: str               # 기준선 offset(PromQL 기간) — Comparison.offset 과 같다
    signals: list[Signal]


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


# ── 질의 조립 — 계산은 여기서 문자열로 서버에 넘어간다 ────────────────────────────

def auto_step(range_sec: float) -> str:
    """창 길이에서 step 을 정한다 — range/60 에 가까운 정수 초, 최소 15s. 서브쿼리 해상도(창당 점 ~60)로 쓰인다."""
    return "%ds" % max(MIN_STEP_SEC, int(range_sec / 60))


def duration_literal(seconds: float) -> str:
    """PromQL 기간 표기 — 정수 초로. (Window 의 길이 · offset 을 질의에 넣을 때)"""
    return "%ds" % int(round(seconds))


def subquery(query: str, window: Window, step: str) -> str:
    """`(Q)[<창 길이>:<step>]` — 창 끝(time=)에서 평가하면 창 전체의 range vector 가 된다."""
    return "(%s)[%s:%s]" % (query, duration_literal(window.seconds), step)


def tagged(expr: str, stat: str) -> str:
    """`label_replace(expr, "stat", "<이름>", "", "")` — 한 질의로 묶은 통계를 이름표로 가른다."""
    return 'label_replace(%s, "%s", "%s", "", "")' % (expr, STAT_LABEL, stat)


def summarize_query(query: str, window: Window, step: str) -> str:
    """일곱 통계를 `or` 로 이어 한 질의로. deriv 는 Prometheus 문서상 단순 선형 회귀 — 최소제곱 기울기다."""
    sq = subquery(query, window, step)
    parts = [
        tagged("count_over_time(%s)" % sq, "n"),
        tagged("min_over_time(%s)" % sq, "min"),
        tagged("quantile_over_time(0.5, %s)" % sq, "p50"),
        tagged("quantile_over_time(0.95, %s)" % sq, "p95"),
        tagged("max_over_time(%s)" % sq, "max"),
        tagged("last_over_time(%s)" % sq, "last"),
        tagged("deriv(%s)" % sq, "slope"),
    ]
    return " or ".join(parts)


def compare_query(query: str, window: Window, step: str, offset: str) -> str:
    """지금 창의 p95 와 같은 식 `offset <기준선>` 의 p95 를 한 질의로."""
    sq = subquery(query, window, step)
    return " or ".join([
        tagged("quantile_over_time(0.95, %s)" % sq, "now"),
        tagged("quantile_over_time(0.95, %s offset %s)" % (sq, offset), "base"),
    ])


def red_query(service: str, window: Window, offset: str) -> str:
    """서비스 하나의 여섯 통계(calls · errors · p95 × 지금 · 기준선)를 서버 span 행 위에서 한 질의로."""
    if not service.strip() or '"' in service or "\n" in service:
        raise ObsError("서비스 이름이 필요하다 — 공백 · 따옴표 · 개행은 받지 않는다: %r" % service)
    length = duration_literal(window.seconds)
    selector = '%s, service_name="%s"' % (SERVER_KIND, service)
    parts: list[str] = []
    for stat in RED_STATS:
        rng = "[%s]%s" % (length, " offset %s" % offset if stat.endswith("_base") else "")
        if stat.startswith("p95_"):
            expr = "histogram_quantile(%s, sum by (service_name) (rate(%s{%s}%s)))" % (RED_QUANTILE, SPAN_METRIC, selector, rng)
        else:
            errors_only = ', status_code="STATUS_CODE_ERROR"' if stat.startswith("errors_") else ""
            expr = "sum by (service_name) (increase(%s{%s%s}%s))" % (CALLS_METRIC, selector, errors_only, rng)
        parts.append(tagged(expr, stat))
    return " or ".join(parts)


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


# ── 응답 → 계약 — 계산 없이 판정만 ────────────────────────────────────────────────

SeriesKey = tuple[tuple[str, str], ...]


def series_key(labels: dict[str, str]) -> SeriesKey:
    return tuple(sorted(labels.items()))


def finite_value(s: Series) -> float | None:
    """즉시 조회 시계열의 값 — 유한하면 float, NaN · ±Inf · 숫자 아님 · 없음은 None(부재)."""
    if not s.values:
        return None
    try:
        v = float(s.values[-1][1])
    except ValueError:
        return None
    return v if math.isfinite(v) else None


def split_stats(res: MetricResult) -> dict[SeriesKey, dict[str, float]]:
    """`stat` 이름표로 묶인 vector 응답을 시계열(그 밖의 레이블)별 {통계: 값} 으로. 부재(NaN 등)는 빠진다."""
    out: dict[SeriesKey, dict[str, float]] = {}
    for s in res.series:
        stat = s.labels.get(STAT_LABEL)
        v = finite_value(s)
        if stat is None or v is None:
            continue
        key = series_key({k: x for k, x in s.labels.items() if k != STAT_LABEL})
        out.setdefault(key, {})[stat] = v
    return out


def summaries_of(res: MetricResult) -> list[SeriesSummary]:
    """응답 → 시계열별 요약(레이블 순). `n` 이 없는 시계열은 부재로 보고 뺀다 — 데이터 없음은 목록에 실리지 않는다."""
    out: list[SeriesSummary] = []
    for key, stats in sorted(split_stats(res).items()):
        if "n" not in stats:
            continue

        def g(name: str) -> float:
            return stats.get(name, math.nan)   # 서버가 못 낸 통계는 NaN(부재)

        out.append(SeriesSummary(labels=dict(key), n=int(stats["n"]), min=g("min"), p50=g("p50"), p95=g("p95"),
                                 max=g("max"), last=g("last"), slope=g("slope")))
    return out


def deltas_of(res: MetricResult) -> list[Delta]:
    """`now` · `base` 이름표의 p95 를 시계열별로 짝지어 차이만. 순서: 비율 내림차순 → 기준선 0 → 기준선 없음 → 지금 없음."""
    deltas: list[Delta] = []
    for key, stats in sorted(split_stats(res).items()):
        n, b = stats.get("now"), stats.get("base")
        ratio: float | None = None
        delta: float | None = None
        if n is not None and b is not None:
            delta = n - b
            if b != 0:
                ratio = n / b
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


def signals_of(res: MetricResult) -> list[Signal]:
    """여섯 통계 → R · E · D 세 행의 상태(missing · idle · value)와 비율. 0 으로 나누지 않는다."""
    stats: dict[str, float] = {}
    for per_series in split_stats(res).values():
        stats.update(per_series)
    now = _side_rows(stats, "now")
    base = _side_rows(stats, "base")
    signals: list[Signal] = []
    for name in RED_SIGNALS:
        (n, n_state), (b, b_state) = now[name], base[name]
        ratio: float | None = None
        if n is not None and b is not None and n_state == RED_VALUE and b_state == RED_VALUE and b != 0:
            ratio = n / b
        signals.append(Signal(name=name, now=n, base=b, ratio=ratio, now_state=n_state, base_state=b_state))
    return signals


def _side_rows(stats: dict[str, float], side: str) -> dict[str, tuple[float | None, str]]:
    """한 창(now · base)의 R · E · D 값과 상태. 호출 카운터가 없으면 셋 다 missing, 0 이면 셋 다 idle(R·E 는 0)."""
    calls = stats.get("calls_%s" % side)
    if calls is None:
        return {name: (None, RED_MISSING) for name in RED_SIGNALS}
    if calls == 0:
        return {"R": (0.0, RED_IDLE), "E": (0.0, RED_IDLE), "D": (None, RED_IDLE)}
    p95 = stats.get("p95_%s" % side)
    return {
        "R": (calls, RED_VALUE),
        "E": (stats.get("errors_%s" % side, 0.0) / calls, RED_VALUE),
        "D": (p95, RED_VALUE if p95 is not None else RED_MISSING),
    }


# ── 연산 — Client 주입, Window 를 받고 Evidence 를 돌려준다. 연산당 GET 1 회 ─────────

def _instant(client: Client, query: str, at: float) -> dict[str, JSON]:
    """즉시 조회 한 번 — 검사한 body."""
    return check_status(client.get("metrics", "api/v1/query", {"query": query, "time": at}).body, "metrics")


def summarize(client: Client, query: str, window: Window, step: str | None = None) -> Evidence[Summary]:
    """창 안의 시계열별 n · min · p50 · p95 · max · last · 기울기 — 전부 서버가 서브쿼리 위에서 계산한다. GET 1 회.
    보낸 조립 질의는 Client.log 의 params 에 있다."""
    if not query.strip():
        raise ObsError("PromQL 이 필요하다")
    step = step or auto_step(window.seconds)
    q = summarize_query(query, window, step)
    body = _instant(client, q, window.end)
    value = Summary(step=step, series=summaries_of(parse_metric(body)))
    return Evidence(value=value)


def baseline_offset(window: Window) -> tuple[Window, str]:
    """기준선이 달린 창과 그 offset(PromQL 기간). 기준선이 없으면 DEFAULT_BASELINE 으로 단다.
    offset 은 창 끝의 차이다 — 길이가 다르거나 뒤에 있는 기준선은 offset 으로 표현할 수 없어 거부한다."""
    w = window if window.baseline is not None else window.with_baseline(DEFAULT_BASELINE)
    base = w.baseline
    assert base is not None
    if abs(base.seconds - w.seconds) > 1e-6 or base.end > w.end:
        raise ObsError("기준선 창은 같은 길이로 앞에 있어야 한다(offset) — Window.with_baseline 으로 단다. "
                       "받은 것: 지금 %.0fs · 기준선 %.0fs" % (w.seconds, base.seconds))
    return w, duration_literal(w.end - base.end)


def compare(client: Client, query: str, window: Window, step: str | None = None) -> Evidence[Comparison]:
    """같은 질의의 p95 를 지금 창과 기준선 창(같은 식의 `offset`)에서 한 질의로 받아 시계열별 차이만. GET 1 회.
    기준선 시계열이 부재면 "기준선 없음" — 서버 응답만으로 선다."""
    if not query.strip():
        raise ObsError("PromQL 이 필요하다")
    w, offset = baseline_offset(window)
    step = step or auto_step(w.seconds)
    q = compare_query(query, w, step, offset)
    body = _instant(client, q, w.end)
    stats = split_stats(parse_metric(body))
    value = Comparison(
        step=step, offset=offset, deltas=deltas_of(parse_metric(body)),
        now_empty=not any("now" in st for st in stats.values()),
        baseline_empty=not any("base" in st for st in stats.values()),
    )
    return Evidence(value=value)


def top(client: Client, query: str, window: Window, k: int = DEFAULT_TOP_K, by: Sequence[str] = ("pod",),
        agg: str = DEFAULT_TOP_AGG) -> Evidence[Top]:
    """레이블 축 상위 k — `window.end` 시각의 즉시 조회. 질의를 `topk(k, agg by (by) (query))` 로 감싸 서버가 k 개만
    돌려주게 한다. 감싼 질의는 Client.log 의 params 에 있다."""
    if not query.strip():
        raise ObsError("PromQL 이 필요하다")
    wrapped = top_query(query, k, by, agg)
    body = _instant(client, wrapped, window.end)
    entries: list[TopEntry] = []
    for s in parse_metric(body).series:
        v = finite_value(s)
        if v is not None:
            entries.append(TopEntry(labels=dict(s.labels), value=v))
    entries.sort(key=lambda e: -e.value)
    value = Top(k=k, by=[a.strip() for a in by if a.strip()], agg=agg, entries=entries[:k])
    return Evidence(value=value)


def red(client: Client, service: str, window: Window) -> Evidence[Red]:
    """서비스 하나의 R · E · D 를 지금 창과 기준선 창(같은 식의 offset)에서 한 질의로. GET 1 회."""
    w, offset = baseline_offset(window)
    q = red_query(service, w, offset)
    body = _instant(client, q, w.end)
    return Evidence(value=Red(service=service, offset=offset, signals=signals_of(parse_metric(body))))
