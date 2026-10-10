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

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from obs.core import JSON, Client, Evidence, ObsError, Response, Window, json_int, json_list, json_obj, json_str, json_strs
from obs.metrics import MetricResult

DEFAULT_QUANTILE: float = 0.95
DEFAULT_EXEMPLARS_K: int = 3
DEFAULT_SEARCH_LIMIT: int = 100         # exemplars 후보 검색의 limit — k 보다 넉넉히
DEFAULT_BASELINE_RANGE: str = "7d"      # 평소 분포의 구간 — 조사 창 직전 7일(compare 의 이동 창 기준선과 다르다 — 의도)
DRIFT_NS: int = 1_000_000               # CRISP happensBefore 완화 — 1ms
SPAN_METRIC: str = "traces_span_metrics_duration_seconds"   # spanmetrics native histogram
CALLS_METRIC: str = "traces_span_metrics_calls_total"
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
        raise NotImplementedError("SpanMatch.endpoint")

    @property
    def seconds(self) -> float:
        """duration_ns / 1e9 — Threshold.seconds 와 같은 단위로 비교한다."""
        raise NotImplementedError("SpanMatch.seconds")


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
    raise NotImplementedError("parse_span_matches")


def parse_quantiles(res: MetricResult) -> list[EndpointQuantiles]:
    """threshold_query 응답(시계열 = (service_name, span_name, stat))을 엔드포인트 하나당 base · now 로."""
    raise NotImplementedError("parse_quantiles")


def threshold_query(quantile: float, window: Window, baseline_range: str) -> str:
    """평소(`[<baseline_range>] offset <창 길이>`) · 조사 창(`[<창 길이>]`) 의 서버 span 분위수를 tagged 로 한 질의에."""
    raise NotImplementedError("threshold_query")


def candidates_query(q: str) -> str:
    """`{ <q> && kind = server } | select(name, status, resource.service.name, resource.k8s.pod.name)` — 빈 q 는 서버 span 전부."""
    raise NotImplementedError("candidates_query")


def thresholds_of(quantiles: Sequence[EndpointQuantiles], sample: Sequence[SpanMatch],
                  quantile: float = DEFAULT_QUANTILE) -> dict[Endpoint, Threshold]:
    """표본의 엔드포인트마다 임계 하나 — 평소 p95 → 조사 창 p95 → 표본 p95 순 폴백."""
    raise NotImplementedError("thresholds_of")


def exemplars_of(sample: Sequence[SpanMatch], thresholds: Mapping[Endpoint, Threshold], k: int, quantile: float,
                 truncated: bool) -> Exemplars:
    """건마다 분류(오류 → 느림 → 정상) · 묶음별 정렬 · k 절단."""
    raise NotImplementedError("exemplars_of")


def exemplars(client: Client, q: str, window: Window, k: int = DEFAULT_EXEMPLARS_K, quantile: float = DEFAULT_QUANTILE,
              baseline_range: str = DEFAULT_BASELINE_RANGE, limit: int = DEFAULT_SEARCH_LIMIT) -> tuple[Evidence[Exemplars], list[Response]]:
    """metrics 임계 → traces 후보 GET 2 회 · 두 질의를 Evidence.queries 에."""
    raise NotImplementedError("exemplars")


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
    clipped: bool                    # 부모 경계에서 잘렸다(fire-and-forget)


@dataclass(frozen=True)
class CriticalPath:
    trace_id: str
    spans: int                       # 트레이스의 span 수 — 0 = span 없음
    total_ms: float                  # 루트 길이
    path: list[PathSpan]             # 임계경로 위 span 만, 루트부터 시간 순 — 겹쳐 무시된 형제 · 그 서브트리 · dropped 는 없다. [] = span 없음
    dropped: int                     # 부모 밖에서 통째로 시작 · 끝나 버린 서브트리 수
    roots: int                       # 부모 없는 span 수 — 1 이 정상 · 2 이상 = 불완전한 트레이스, 가장 먼저 시작한 것을 루트로 따랐다

    @property
    def bottlenecks(self) -> list[PathSpan]:
        """path 를 self_ms 내림차순(동률은 offset_ms 오름차순)으로 — [0] 이 병목 · path 가 비면 []."""
        raise NotImplementedError("CriticalPath.bottlenecks")


def critical_path_of(trace: Trace) -> CriticalPath:
    """CRISP — LFC 를 따라 임계경로 · exclusive time · 경계 규칙(드리프트 1ms · 부모 경계 절단 · 밖 서브트리 폐기)."""
    raise NotImplementedError("critical_path_of")


def critical_path(client: Client, trace_id: str, window: Window) -> tuple[Evidence[CriticalPath], list[Response]]:
    """`api/traces/<id>` GET 1 회 → parse_spans → Trace → critical_path_of."""
    raise NotImplementedError("critical_path")


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
    raise NotImplementedError("edge_query")


def parse_edges(res: MetricResult) -> list[ClientRow]:
    """edge_query 응답을 (caller, endpoint) 하나당 ClientRow 로 — increase 의 소수는 정수로 반올림."""
    raise NotImplementedError("parse_edges")


def service_graph_of(clients: Sequence[ClientRow], servers: Sequence[EndpointQuantiles], quantile: float,
                     baseline_range: str) -> ServiceGraph:
    """클라이언트 행 → 엣지 · span_name 으로 서버 행과 조인 · 배율 · 정렬 · 데이터 없음과 호출 0 의 구분."""
    raise NotImplementedError("service_graph_of")


def service_graph(client: Client, window: Window, quantile: float = DEFAULT_QUANTILE,
                  baseline_range: str = DEFAULT_BASELINE_RANGE) -> tuple[Evidence[ServiceGraph], list[Response]]:
    """metrics GET 2 회 — edge_query(CLIENT) → threshold_query(SERVER). traces 백엔드를 부르지 않는다."""
    raise NotImplementedError("service_graph")


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
    raise NotImplementedError("diff_of")


def diff(client: Client, slow_id: str, normal_id: str, window: Window) -> tuple[Evidence[TraceDiff], list[Response]]:
    """`api/traces/<slow>` → `api/traces/<normal>` GET 2 회 → diff_of."""
    raise NotImplementedError("diff")
