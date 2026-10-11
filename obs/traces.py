"""obs.traces — 트레이스 환원 연산: exemplars · critical_path · service_graph · diff.

span 트리를 agent 가 읽지 않고 "어느 홉이 늘었나" 를 안다. 구조로 거는 것(obs.metrics · obs.logs 와 같다):
  - 순수 함수다 — HTTP 는 `Client` 주입(`Client.get` 만 부른다). 파서(`parse_*`)와 계산(`*_of`)은 HTTP 를 모른다.
  - 연산은 `Window` 를 받고 `(Evidence[V], list[Response])` 를 돌려준다. 원본은 어댑터가 spill 한다.
  - 호출이 둘 이상인 연산(exemplars · service_graph · diff)은 보낸 질의 전부를 `Evidence.queries` 에 담는다 — 재실행 가능.
  - "데이터 없음" 과 "0" 을 구분한다.

Tempo 응답 파서(`Span` · `parse_spans` · `TraceHit` · `parse_search` · `TagScope` · `parse_tags` · `parse_tag_values` · `check_json`)는
어댑터(cli)에서 이리로 옮겨 왔다 — cli 는 같은 이름을 import 한다. `parse_spans` 는 OTLP 두 형상(batches · resourceSpans)을 그대로 읽는다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from obs.core import (
    JSON, Client, Evidence, ObsError, Query, Response, Window, json_int, json_list, json_obj, json_str, json_strs, parse_duration,
)
from obs.metrics import STAT_LABEL, MetricResult, check_status, duration_literal, finite_value, parse_metric, tagged

DEFAULT_QUANTILE: float = 0.95
DEFAULT_EXEMPLARS_K: int = 3
DEFAULT_SEARCH_LIMIT: int = 100         # exemplars 후보 검색의 limit — k 보다 넉넉히
DEFAULT_BASELINE_RANGE: str = "7d"      # 평소 분포의 구간 — 조사 창 직전 7일(compare 의 이동 창 기준선과 다르다 — 의도)
DRIFT_NS: int = 1_000_000               # CRISP happensBefore 완화 — 1ms
SPAN_METRIC: str = "traces_span_metrics_duration_seconds"   # spanmetrics native histogram
CALLS_METRIC: str = "traces_span_metrics_calls_total"
SERVER_KIND: str = 'span_kind="SPAN_KIND_SERVER"'      # spanmetrics 의 서버 span 행 — 임계 · callee
CLIENT_KIND: str = 'span_kind="SPAN_KIND_CLIENT"'      # spanmetrics 의 클라이언트 span 행 — 엣지
THRESHOLD_BASELINE: str = "baseline"    # 평소 p95 — 조사 창 직전 baseline_range 분포
THRESHOLD_WINDOW: str = "window"        # 조사 창 p95
THRESHOLD_SAMPLE: str = "sample"        # 검색 표본 p95 — 같은 엔드포인트의 표본
BUCKETS: tuple[str, ...] = ("slow", "error", "normal")


# ── 응답 dataclass + 파서 — Tempo (이동: obs/adapters/cli.py) ─────────────────────

@dataclass(frozen=True)
class TraceHit:
    trace_id: str
    duration_ms: str
    start_ns: int
    root_service: str
    root_name: str


@dataclass
class Span:
    service: str
    name: str
    span_id: str
    parent_id: str
    start_ns: int
    end_ns: int
    status_code: str      # STATUS_CODE_ERROR / 2 / ""
    status_message: str = ""   # OTLP status.message — 오류 span 의 원인(계측 라이브러리가 스택을 붙이기도 한다)
    attrs: dict[str, str] = field(default_factory=dict[str, str])

    @property
    def duration_ms(self) -> float:
        return (self.end_ns - self.start_ns) / 1e6

    @property
    def is_error(self) -> bool:
        return self.status_code in ("STATUS_CODE_ERROR", "2")


@dataclass(frozen=True)
class TagScope:
    name: str
    tags: list[str]


def _clip(text: str, width: int) -> str:
    text = text.replace("\n", "⏎")
    return text if len(text) <= width else text[: width - 1] + "…"


def check_json(body: JSON, backend: str) -> dict[str, JSON]:
    if not isinstance(body, dict):
        raise ObsError("%s 백엔드가 JSON 이 아닌 응답을 냈다: %s" % (backend, _clip(json_str(body), 300)))
    return body


def parse_search(body: dict[str, JSON]) -> list[TraceHit]:
    hits: list[TraceHit] = []
    for t in json_list(body.get("traces")):
        o = json_obj(t)
        hits.append(TraceHit(
            trace_id=json_str(o.get("traceID"), "?"), duration_ms=json_str(o.get("durationMs"), "?"),
            start_ns=json_int(o.get("startTimeUnixNano")),
            root_service=json_str(o.get("rootServiceName"), "?"), root_name=json_str(o.get("rootTraceName"), "?")))
    return hits


def _attr_value(v: JSON) -> str:
    o = json_obj(v)
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in o:
            return json_str(o[k])
    return json_str(v)


def _attrs(items: JSON) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in json_list(items):
        o = json_obj(a)
        out[json_str(o.get("key"), "?")] = _attr_value(o.get("value"))
    return out


def parse_spans(body: dict[str, JSON]) -> list[Span]:
    """OTLP JSON — Tempo 는 `batches`(v1) 또는 `resourceSpans`(v2) 로 준다."""
    batches = body.get("batches") or body.get("resourceSpans") or json_obj(body.get("trace")).get("resourceSpans")
    spans: list[Span] = []
    for b in json_list(batches):
        bo = json_obj(b)
        service = _attrs(json_obj(bo.get("resource")).get("attributes")).get("service.name", "?")
        for ss in json_list(bo.get("scopeSpans") or bo.get("instrumentationLibrarySpans")):
            for s in json_list(json_obj(ss).get("spans")):
                so = json_obj(s)
                st = json_obj(so.get("status"))
                spans.append(Span(
                    service=service, name=json_str(so.get("name"), "?"), span_id=json_str(so.get("spanId")),
                    parent_id=json_str(so.get("parentSpanId")),
                    start_ns=json_int(so.get("startTimeUnixNano")), end_ns=json_int(so.get("endTimeUnixNano")),
                    status_code=json_str(st.get("code")), status_message=json_str(st.get("message")),
                    attrs=_attrs(so.get("attributes")),
                ))
    return spans


def parse_tags(body: dict[str, JSON]) -> list[TagScope]:
    return [TagScope(name=json_str(json_obj(sc).get("name")), tags=json_strs(json_obj(sc).get("tags"))) for sc in json_list(body.get("scopes"))]


def parse_tag_values(body: dict[str, JSON]) -> list[str]:
    return [json_str(json_obj(v).get("value")) for v in json_list(body.get("tagValues"))]


# ── 환원물 dataclass — 공통 ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Endpoint:
    """임계와 건의 열쇠 — (서비스, 서버 span 이름). spanmetrics 의 (service_name, span_name) 과 같은 쌍."""
    service: str
    name: str


@dataclass(frozen=True)
class Trace:
    """트레이스 하나 — api/traces/<id> 응답을 기존 parse_spans 로 옮긴 것. spans 가 비면 span 없음."""
    trace_id: str
    spans: list[Span]


# ── GET — 연산들이 함께 쓰는 호출 하나 ────────────────────────────────────────────

def _instant(client: Client, query: str, window: Window) -> tuple[MetricResult, Response]:
    """metrics 즉시 조회 한 번(time = 창 끝) — 검사 · 파싱한 결과와 그 Response."""
    resp = client.get("metrics", "api/v1/query", {"query": query, "time": window.end})
    return parse_metric(check_status(resp.body, "metrics")), resp


def _fetch_trace(client: Client, trace_id: str) -> tuple[Trace, Response]:
    """`api/traces/<id>` 한 번 — Trace 와 그 Response."""
    resp = client.get("traces", "api/traces/%s" % trace_id)
    return Trace(trace_id, parse_spans(check_json(resp.body, "traces"))), resp


# ── exemplars ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SpanMatch:
    """검색 응답 `traces[].spanSets[].spans[]` 의 매치 span 하나 — 트레이스를 열지 않고 분류한다."""
    trace_id: str
    span_id: str
    name: str
    start_ns: int
    duration_ns: int
    status: str                      # select 한 status — error · ok · unset · "" (없음)
    service: str                     # "" = 속성 없음
    pod: str                         # "" = 속성 없음

    @property
    def endpoint(self) -> Endpoint:
        """Endpoint(service, name) — thresholds_of · exemplars_of 가 이 열쇠로 임계를 찾는다."""
        return Endpoint(self.service, self.name)

    @property
    def seconds(self) -> float:
        """duration_ns / 1e9 — Threshold.seconds 와 같은 단위로 비교한다."""
        return self.duration_ns / 1e9


@dataclass(frozen=True)
class EndpointQuantiles:
    """threshold_query 응답을 엔드포인트 하나로 접은 것 — 평소(base) · 조사 창(now) 분위수(초). 없는 쪽은 None(호출이 없어 rate 가 NaN → 행 부재)."""
    endpoint: Endpoint
    base: float | None               # 조사 창 직전 baseline_range 분포의 분위수
    now: float | None                # 조사 창의 분위수


@dataclass(frozen=True)
class Threshold:
    """엔드포인트 하나의 느림 판정 기준 — thresholds_of 가 세 후보 중 하나를 골라 만든다. exemplars_of 가 건마다 꺼내 쓴다."""
    seconds: float                   # 서버 span 지속시간이 이것을 넘으면 slow (초 — spanmetrics 단위 그대로)
    source: str                      # THRESHOLD_BASELINE(평소 p95, 1순위) · THRESHOLD_WINDOW(조사 창 p95) · THRESHOLD_SAMPLE(표본 p95)


@dataclass(frozen=True)
class Exemplar:
    trace_id: str
    span_id: str
    service: str
    endpoint: str                    # 서버 span 이름
    pod: str
    duration_ms: float
    start_ts: float                  # unix 초
    threshold_ms: float              # 적용한 임계 — 늘 있다(thresholds_of 가 표본의 모든 엔드포인트에 임계를 만든다)
    threshold_source: str            # THRESHOLD_* — 어느 임계였나
    ratio: float                     # duration / threshold


@dataclass(frozen=True)
class Bucket:
    kind: str                        # slow · error · normal
    total: int                       # 분류된 전체 건수 — 0 = 없음(0 건, 데이터 없음과 다르다)
    items: list[Exemplar]            # k 개까지


@dataclass(frozen=True)
class Exemplars:
    k: int
    quantile: float
    sample: int                      # 검색 표본의 매치 span 수 — 0 = 데이터 없음
    truncated: bool                  # 매치가 limit 을 넘었다 — 표본은 앞 limit 개 트레이스의 건만
    slow: Bucket
    error: Bucket
    normal: Bucket


def parse_span_matches(body: dict[str, JSON]) -> list[SpanMatch]:
    """검색 응답의 매치 span 전부 — spanSets 없는 트레이스는 건너뛴다 · 없는 속성은 ""."""
    matches: list[SpanMatch] = []
    for t in json_list(body.get("traces")):
        o = json_obj(t)
        trace_id = json_str(o.get("traceID"))
        span_sets = json_list(o.get("spanSets")) or [o.get("spanSet")]
        for ss in span_sets:
            for sp in json_list(json_obj(ss).get("spans")):
                so = json_obj(sp)
                attrs = _attrs(so.get("attributes"))
                matches.append(SpanMatch(
                    trace_id=trace_id, span_id=json_str(so.get("spanID")), name=json_str(so.get("name")),
                    start_ns=json_int(so.get("startTimeUnixNano")), duration_ns=json_int(so.get("durationNanos")),
                    status=attrs.get("status", ""), service=attrs.get("service.name", ""), pod=attrs.get("k8s.pod.name", "")))
    return matches


def parse_quantiles(res: MetricResult) -> list[EndpointQuantiles]:
    """threshold_query 응답(시계열 = (service_name, span_name, stat))을 엔드포인트 하나당 base · now 로."""
    return [EndpointQuantiles(endpoint=ep, base=st.get("base"), now=st.get("now")) for ep, st in _stats_by_endpoint(res).items()]


def _stats_by_endpoint(res: MetricResult) -> dict[Endpoint, dict[str, float | None]]:
    """tagged 응답을 (service_name, span_name) 하나당 {stat: 값} 으로 — 유한하지 않은 값은 None(부재)이되 엔드포인트는 남는다."""
    stats: dict[Endpoint, dict[str, float | None]] = {}
    for s in res.series:
        ep = Endpoint(s.labels.get("service_name", ""), s.labels.get("span_name", ""))
        stats.setdefault(ep, {})[s.labels.get(STAT_LABEL, "")] = finite_value(s)
    return stats


def threshold_query(quantile: float, window: Window, baseline_range: str) -> str:
    """평소(`[<baseline_range>] offset <창 길이>`) · 조사 창(`[<창 길이>]`) 의 서버 span 분위수를 tagged 로 한 질의에."""
    length = _window_length(quantile, window, baseline_range)
    return " or ".join([
        tagged(_quantile_expr(quantile, SERVER_KIND, baseline_range, offset=length), "base"),
        tagged(_quantile_expr(quantile, SERVER_KIND, length), "now"),
    ])


def _window_length(quantile: float, window: Window, baseline_range: str) -> str:
    """분위수 · baseline_range 를 검사하고 창 길이의 PromQL 기간 표기를 돌려준다 — 질의 조립 전, 요청 전."""
    if not 0 < quantile < 1:
        raise ObsError("분위수는 0 과 1 사이여야 한다: %s" % quantile)
    parse_duration(baseline_range)
    return duration_literal(window.seconds)


def _quantile_expr(quantile: float, kind: str, range_: str, offset: str | None = None) -> str:
    """(service_name, span_name) 별 spanmetrics 분위수 — `rate(…[<range_>])`, offset 이 있으면 `[<range_>] offset <offset>`."""
    selector = "%s{%s}[%s]%s" % (SPAN_METRIC, kind, range_, " offset %s" % offset if offset is not None else "")
    return "histogram_quantile(%s, sum by (service_name, span_name) (rate(%s)))" % (quantile, selector)


def candidates_query(q: str) -> str:
    """`{ <q> && kind = server } | select(name, status, resource.service.name, resource.k8s.pod.name)` — 빈 q 는 서버 span 전부."""
    cond = q.strip()
    if cond.startswith("{") and cond.endswith("}"):
        cond = cond[1:-1].strip()
    where = "%s && kind = server" % cond if cond else "kind = server"
    return "{ %s } | select(name, status, resource.service.name, resource.k8s.pod.name)" % where


def thresholds_of(quantiles: Sequence[EndpointQuantiles], sample: Sequence[SpanMatch],
                  quantile: float = DEFAULT_QUANTILE) -> dict[Endpoint, Threshold]:
    """표본의 엔드포인트마다 임계 하나 — 평소 p95 → 조사 창 p95 → 표본 p95 순 폴백."""
    known = _by_endpoint(quantiles)
    durations: dict[Endpoint, list[float]] = {}
    for m in sample:
        durations.setdefault(m.endpoint, []).append(m.seconds)
    out: dict[Endpoint, Threshold] = {}
    for ep, secs in durations.items():
        q = known.get(ep)
        if q is not None and q.base is not None and q.base > 0:
            out[ep] = Threshold(q.base, THRESHOLD_BASELINE)
        elif q is not None and q.now is not None and q.now > 0:
            out[ep] = Threshold(q.now, THRESHOLD_WINDOW)
        else:
            out[ep] = Threshold(_nearest_rank(secs, quantile), THRESHOLD_SAMPLE)
    return out


def _by_endpoint(quantiles: Sequence[EndpointQuantiles]) -> dict[Endpoint, EndpointQuantiles]:
    """엔드포인트 하나당 EndpointQuantiles 하나 — 같은 엔드포인트가 둘이면 ObsError(뒤의 것이 조용히 이기지 않는다)."""
    known: dict[Endpoint, EndpointQuantiles] = {}
    for q in quantiles:
        if q.endpoint in known:
            raise ObsError("같은 엔드포인트의 분위수가 둘이다: %s / %s" % (q.endpoint.service, q.endpoint.name))
        known[q.endpoint] = q
    return known


def _nearest_rank(values: Sequence[float], quantile: float) -> float:
    """nearest-rank 분위수 — 정렬한 값의 ceil(q·n) 번째."""
    ordered = sorted(values)
    return ordered[math.ceil(quantile * len(ordered)) - 1]


def exemplars_of(sample: Sequence[SpanMatch], thresholds: Mapping[Endpoint, Threshold], k: int, quantile: float,
                 truncated: bool) -> Exemplars:
    """건마다 분류(오류 → 느림 → 정상) · 묶음별 정렬 · k 절단."""
    if k < 1:
        raise ObsError("k 는 1 이상이어야 한다: %d" % k)
    buckets: dict[str, list[Exemplar]] = {kind: [] for kind in BUCKETS}
    for m in sample:
        th = thresholds.get(m.endpoint)
        if th is None:
            raise ObsError("임계가 없는 엔드포인트 %s / %s — thresholds_of 의 결과를 넘긴다" % (m.service, m.name))
        if th.seconds <= 0:
            raise ObsError("임계가 0 이하인 엔드포인트 %s / %s: %s" % (m.service, m.name, th.seconds))
        duration_ms = m.duration_ns / 1e6
        threshold_ms = th.seconds * 1000
        kind = "error" if m.status == "error" else "slow" if m.seconds > th.seconds else "normal"
        buckets[kind].append(Exemplar(
            trace_id=m.trace_id, span_id=m.span_id, service=m.service, endpoint=m.name, pod=m.pod, duration_ms=duration_ms,
            start_ts=m.start_ns / 1e9, threshold_ms=threshold_ms, threshold_source=th.source, ratio=duration_ms / threshold_ms))
    buckets["slow"].sort(key=lambda e: (-e.ratio, e.trace_id))
    buckets["error"].sort(key=lambda e: (-e.duration_ms, e.trace_id))
    buckets["normal"].sort(key=lambda e: (-e.duration_ms, e.trace_id))
    slow, error, normal = (Bucket(kind, len(buckets[kind]), buckets[kind][:k]) for kind in BUCKETS)
    return Exemplars(k=k, quantile=quantile, sample=len(sample), truncated=truncated, slow=slow, error=error, normal=normal)


def exemplars(client: Client, q: str, window: Window, k: int = DEFAULT_EXEMPLARS_K, quantile: float = DEFAULT_QUANTILE,
              baseline_range: str = DEFAULT_BASELINE_RANGE, limit: int = DEFAULT_SEARCH_LIMIT) -> tuple[Evidence[Exemplars], list[Response]]:
    """metrics 임계 → traces 후보 GET 2 회 · 두 질의를 Evidence.queries 에."""
    if k < 1:
        raise ObsError("k 는 1 이상이어야 한다: %d" % k)
    if limit < 1:
        raise ObsError("limit 은 1 이상이어야 한다: %d" % limit)
    mq = threshold_query(quantile, window, baseline_range)
    tq = candidates_query(q)
    m_res, m_resp = _instant(client, mq, window)
    quantiles = parse_quantiles(m_res)
    t_resp = client.get("traces", "api/search", {"start": int(window.start), "end": int(window.end), "limit": limit + 1, "q": tq})
    hits = json_list(check_json(t_resp.body, "traces").get("traces"))
    sample = parse_span_matches({"traces": hits[:limit]})
    value = exemplars_of(sample, thresholds_of(quantiles, sample, quantile), k, quantile, truncated=len(hits) > limit)
    queries = [Query("metrics", mq, window), Query("traces", tq, window)]
    return Evidence(backend="traces", query=tq, window=window, value=value, queries=queries), [m_resp, t_resp]


# ── critical_path ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PathSpan:
    service: str
    name: str
    span_id: str
    depth: int                       # 루트 0
    offset_ms: float                 # 루트 시작 기준 누적 오프셋
    duration_ms: float               # 부모 경계로 자른 뒤
    self_ms: float                   # exclusive time — 임계경로 위 자식에 귀속되지 않은 시간
    clipped_ms: float                # 부모 경계 밖으로 넘친 길이(0.0 = 잘리지 않음 · 원래 길이 = duration_ms + clipped_ms)


@dataclass(frozen=True)
class CriticalPath:
    trace_id: str
    spans: int                       # 트레이스의 span 수 — 0 = span 없음
    total_ms: float                  # 루트 길이
    path: list[PathSpan]             # 임계경로 위 span 만, 루트부터 시간 순 — 겹쳐 무시된 형제 · 그 서브트리 · dropped 는 없다. [] = span 없음
    dropped: int                     # 부모 밖에서 통째로 시작 · 끝나 버린 서브트리 수
    roots: int                       # parent 가 빈 span 수 — 1 이 정상 · 2 이상 = 불완전(가장 먼저 시작한 것을 따랐다) · 0 = 루트 유실(path 는 비고 고아를 승격하지 않는다)
    orphans: int                     # parent id 가 트레이스 안에 없는 span 수 — 부모 유실. 그 서브트리는 path 에 없다

    @property
    def bottlenecks(self) -> list[PathSpan]:
        """path 를 self_ms 내림차순(동률은 offset_ms 오름차순)으로 — [0] 이 병목 · path 가 비면 []."""
        return sorted(self.path, key=lambda p: (-p.self_ms, p.offset_ms))


def critical_path_of(trace: Trace) -> CriticalPath:
    """CRISP — LFC 를 따라 임계경로 · exclusive time · 경계 규칙(드리프트 1ms · 부모 경계 절단 · 밖 서브트리 폐기)."""
    if not trace.spans:
        return CriticalPath(trace_id=trace.trace_id, spans=0, total_ms=0.0, path=[], dropped=0, roots=0, orphans=0)
    roots, orphans = _roots(trace.spans), len(_orphans(trace.spans))
    if not roots and not orphans:
        raise ObsError("트레이스 %s 에 루트가 없다 — parent 가 순환한다" % trace.trace_id)
    if not roots:
        return CriticalPath(trace_id=trace.trace_id, spans=len(trace.spans), total_ms=0.0, path=[], dropped=0, roots=0, orphans=orphans)
    children: dict[str, list[Span]] = {}
    for s in trace.spans:
        children.setdefault(s.parent_id, []).append(s)
    root = min(roots, key=lambda s: s.start_ns)
    walk = _CrispWalk(children=children, origin=root.start_ns)
    walk.visit(root, root.start_ns, root.end_ns, 0)
    return CriticalPath(trace_id=trace.trace_id, spans=len(trace.spans), total_ms=root.duration_ms, path=walk.path,
                        dropped=walk.dropped, roots=len(roots), orphans=orphans)


def _roots(spans: Sequence[Span]) -> list[Span]:
    """parent 가 빈 span — 정상이면 하나."""
    return [s for s in spans if not s.parent_id]


def _orphans(spans: Sequence[Span]) -> list[Span]:
    """parent id 가 있으나 트레이스 안에 없는 span — 부모 유실."""
    ids = {s.span_id for s in spans}
    return [s for s in spans if s.parent_id and s.parent_id not in ids]


@dataclass
class _CrispWalk:
    """임계경로를 루트부터 내려가며 모은다 — path 는 시간 순, dropped 는 부모 밖 서브트리 수."""
    children: dict[str, list[Span]]
    origin: int                      # 루트 시작(ns) — offset 의 기준
    path: list[PathSpan] = field(default_factory=list[PathSpan])
    dropped: int = 0

    def visit(self, span: Span, lo: int, hi: int, depth: int) -> None:
        """span 을 [lo, hi] 로 자른 구간에서 — LFC 를 끝에서부터 고르고, 고른 자식마다 내려간다."""
        inside: list[tuple[int, int, Span]] = []
        for c in self.children.get(span.span_id, []):
            if c.start_ns >= hi or c.end_ns <= lo:
                self.dropped += 1
                continue
            inside.append((max(c.start_ns, lo), min(c.end_ns, hi), c))
        picked: list[tuple[int, int, Span]] = []
        cursor, self_ns = hi, 0
        candidates: list[tuple[int, int, Span]] = inside
        while candidates:
            lfc = max(candidates, key=lambda x: (x[1], x[0]))
            picked.append(lfc)
            self_ns += cursor - min(lfc[1], cursor)
            cursor = lfc[0]
            candidates = [x for x in candidates if x is not lfc and x[1] <= cursor + DRIFT_NS]
        self_ns += cursor - lo
        self.path.append(PathSpan(service=span.service, name=span.name, span_id=span.span_id, depth=depth,
                                  offset_ms=(lo - self.origin) / 1e6, duration_ms=(hi - lo) / 1e6, self_ms=self_ns / 1e6,
                                  clipped_ms=((span.end_ns - span.start_ns) - (hi - lo)) / 1e6))
        for c_lo, c_hi, c in reversed(picked):
            self.visit(c, c_lo, c_hi, depth + 1)


def critical_path(client: Client, trace_id: str, window: Window) -> tuple[Evidence[CriticalPath], list[Response]]:
    """`api/traces/<id>` GET 1 회 → parse_spans → Trace → critical_path_of."""
    if not trace_id.strip():
        raise ObsError("traceID 가 필요하다")
    trace, resp = _fetch_trace(client, trace_id)
    value = critical_path_of(trace)
    return Evidence(backend="traces", query="critical_path(%s)" % trace_id, window=window, value=value), [resp]


# ── service_graph ───────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClientRow:
    """spanmetrics 의 클라이언트 span 행 하나 — 엣지의 원료. edge_query 응답을 (caller, endpoint) 로 접은 것."""
    caller: str                      # service_name — 호출한 쪽
    endpoint: str                    # span_name — callee 의 엔드포인트(gRPC 전체 메서드 이름)
    calls: int                       # 조사 창 건수(increase 를 반올림) — 0 = 창 안 호출 없음(idle)
    errors: int                      # 그중 status_code=ERROR
    now: float | None                # 조사 창 p95(초) — None = 분포 없음
    base: float | None               # 평소(baseline_range) p95(초) — None = 평소에 호출 없음


@dataclass(frozen=True)
class Edge:
    """호출 그래프의 엣지 하나 — (caller 서비스 → callee 서비스의 엔드포인트). 같은 서비스 쌍이라도 엔드포인트마다 갈린다."""
    caller: str
    callee: str | None               # 같은 span_name 의 서버 행의 service_name — None = 미확인(서버 행 없음 · 서비스 둘 이상에 있어 모호)
    endpoint: str
    count: int                       # 조사 창 호출 수(> 0 인 것만 엣지)
    error_rate: float                # errors / count · 0.0 = 오류 없음
    client_p95_ms: float | None      # caller 가 기다린 시간의 조사 창 p95 — None = 분포 없음
    client_base_p95_ms: float | None # 같은 것의 평소 p95 — None = 평소에 호출 없음
    ratio: float | None              # client_p95 / client_base — 둘 다 있을 때. 정렬 열쇠
    server_p95_ms: float | None      # callee 쪽 서버 span 의 조사 창 p95 — None = 조사 창에 서버 행 없음


@dataclass(frozen=True)
class ServiceGraph:
    quantile: float
    baseline_range: str
    edges: list[Edge]                # ratio 내림차순(None 뒤) · 동률 count 내림차순 · (caller, callee, endpoint). [] 이고 idle 0 = 데이터 없음
    idle: int                        # 창 안 호출 0 인 클라이언트 행 수 — edges 가 비어도 idle > 0 이면 "호출 0" 이지 데이터 없음이 아니다


def edge_query(quantile: float, window: Window, baseline_range: str) -> str:
    """클라이언트 span 행의 calls · errors · now · base 를 tagged 로 한 질의에."""
    length = _window_length(quantile, window, baseline_range)
    calls = "sum by (service_name, span_name) (increase(%s{%s}[%s]))" % (CALLS_METRIC, CLIENT_KIND, length)
    errors = 'sum by (service_name, span_name) (increase(%s{%s, status_code="STATUS_CODE_ERROR"}[%s]))' % (CALLS_METRIC, CLIENT_KIND, length)
    return " or ".join([
        tagged(calls, "calls"), tagged(errors, "errors"),
        tagged(_quantile_expr(quantile, CLIENT_KIND, length), "now"),
        tagged(_quantile_expr(quantile, CLIENT_KIND, baseline_range, offset=length), "base"),
    ])


def parse_edges(res: MetricResult) -> list[ClientRow]:
    """edge_query 응답을 (caller, endpoint) 하나당 ClientRow 로 — increase 의 소수는 정수로 반올림."""
    rows: list[ClientRow] = []
    for ep, st in _stats_by_endpoint(res).items():
        calls = st.get("calls")
        if calls is None:
            continue
        errors = st.get("errors")
        rows.append(ClientRow(caller=ep.service, endpoint=ep.name, calls=round(calls), errors=round(errors) if errors is not None else 0,
                              now=st.get("now"), base=st.get("base")))
    return rows


def service_graph_of(clients: Sequence[ClientRow], servers: Sequence[EndpointQuantiles], quantile: float,
                     baseline_range: str) -> ServiceGraph:
    """클라이언트 행 → 엣지 · span_name 으로 서버 행과 조인 · 배율 · 정렬 · 데이터 없음과 호출 0 의 구분."""
    by_name: dict[str, list[EndpointQuantiles]] = {}
    for sv in _by_endpoint(servers).values():
        by_name.setdefault(sv.endpoint.name, []).append(sv)
    edges: list[Edge] = []
    for row in clients:
        if row.calls == 0:
            continue
        matched = by_name.get(row.endpoint, [])
        server = matched[0] if len({m.endpoint.service for m in matched}) == 1 else None
        edges.append(Edge(
            caller=row.caller, callee=server.endpoint.service if server is not None else None, endpoint=row.endpoint, count=row.calls,
            error_rate=row.errors / row.calls, client_p95_ms=_ms(row.now), client_base_p95_ms=_ms(row.base),
            ratio=row.now / row.base if row.now is not None and row.base is not None and row.base > 0 else None,
            server_p95_ms=_ms(server.now) if server is not None else None))
    edges.sort(key=lambda e: (e.ratio is None, -(e.ratio or 0.0), -e.count, e.caller, e.callee or "", e.endpoint))
    return ServiceGraph(quantile=quantile, baseline_range=baseline_range, edges=edges, idle=sum(1 for r in clients if r.calls == 0))


def _ms(seconds: float | None) -> float | None:
    return seconds * 1000 if seconds is not None else None


def service_graph(client: Client, window: Window, quantile: float = DEFAULT_QUANTILE,
                  baseline_range: str = DEFAULT_BASELINE_RANGE) -> tuple[Evidence[ServiceGraph], list[Response]]:
    """metrics GET 2 회 — edge_query(CLIENT) → threshold_query(SERVER). traces 백엔드를 부르지 않는다."""
    eq = edge_query(quantile, window, baseline_range)
    sq = threshold_query(quantile, window, baseline_range)
    e_res, e_resp = _instant(client, eq, window)
    s_res, s_resp = _instant(client, sq, window)
    value = service_graph_of(parse_edges(e_res), parse_quantiles(s_res), quantile, baseline_range)
    queries = [Query("metrics", eq, window), Query("metrics", sq, window)]
    return Evidence(backend="metrics", query=eq, window=window, value=value, queries=queries), [e_resp, s_resp]


# ── diff ────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SpanTime:
    """한쪽 트레이스에서 같은 (서비스, 이름) span 들의 합과 건수."""
    total_ms: float
    n: int


@dataclass(frozen=True)
class SpanDelta:
    """(서비스, 이름) 하나의 양쪽 값과 차이 — 비교의 행."""
    service: str
    name: str
    slow: SpanTime | None            # None = 느린 쪽에 없음
    normal: SpanTime | None          # None = 정상 쪽에 없음
    delta_ms: float | None           # 둘 다 있을 때 slow.total_ms − normal.total_ms


@dataclass(frozen=True)
class TraceDiff:
    slow_id: str
    normal_id: str
    root_name: str
    deltas: list[SpanDelta]          # (name, service) 오름차순


def diff_of(slow: Trace, normal: Trace) -> TraceDiff:
    """(서비스, 이름) 으로 짝지어 시간 차이만 · 한쪽에만 있음의 표시. 루트 이름이 다르면 ObsError."""
    slow_root, normal_root = _root(slow), _root(normal)
    if slow_root.name != normal_root.name:
        raise ObsError("루트 이름이 다른 두 트레이스는 비교하지 않는다: %s · %s" % (slow_root.name, normal_root.name))
    slow_times, normal_times = _times(slow), _times(normal)
    deltas: list[SpanDelta] = []
    for service, name in sorted(slow_times.keys() | normal_times.keys(), key=lambda key: (key[1], key[0])):
        st, nt = slow_times.get((service, name)), normal_times.get((service, name))
        delta = st.total_ms - nt.total_ms if st is not None and nt is not None else None
        deltas.append(SpanDelta(service=service, name=name, slow=st, normal=nt, delta_ms=delta))
    return TraceDiff(slow_id=slow.trace_id, normal_id=normal.trace_id, root_name=slow_root.name, deltas=deltas)


def _root(trace: Trace) -> Span:
    """부모 없는 span 중 가장 먼저 시작한 것 — span 이 없으면 ObsError."""
    if not trace.spans:
        raise ObsError("트레이스 %s 에 span 이 없다" % trace.trace_id)
    roots = _roots(trace.spans)
    if not roots:
        raise ObsError("트레이스 %s 에 루트(parent 가 빈 span)가 없다" % trace.trace_id)
    return min(roots, key=lambda s: s.start_ns)


def _times(trace: Trace) -> dict[tuple[str, str], SpanTime]:
    """(서비스, 이름) 하나당 지속시간 합과 건수."""
    totals: dict[tuple[str, str], tuple[float, int]] = {}
    for s in trace.spans:
        total, n = totals.get((s.service, s.name), (0.0, 0))
        totals[(s.service, s.name)] = (total + s.duration_ms, n + 1)
    return {key: SpanTime(total_ms=total, n=n) for key, (total, n) in totals.items()}


def diff(client: Client, slow_id: str, normal_id: str, window: Window) -> tuple[Evidence[TraceDiff], list[Response]]:
    """`api/traces/<slow>` → `api/traces/<normal>` GET 2 회 → diff_of."""
    if not slow_id.strip() or not normal_id.strip():
        raise ObsError("traceID 두 개가 필요하다")
    if slow_id == normal_id:
        raise ObsError("같은 트레이스끼리는 비교하지 않는다: %s" % slow_id)
    slow, s_resp = _fetch_trace(client, slow_id)
    normal, n_resp = _fetch_trace(client, normal_id)
    value = diff_of(slow, normal)
    main = "diff(%s, %s)" % (slow_id, normal_id)
    queries = [Query("traces", main, window), Query("traces", "trace(%s)" % slow_id, window), Query("traces", "trace(%s)" % normal_id, window)]
    return Evidence(backend="traces", query=main, window=window, value=value, queries=queries), [s_resp, n_resp]
