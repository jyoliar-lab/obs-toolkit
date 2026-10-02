"""obs.logs — 로그 환원 연산: histogram · templates · around · first_seen (logs 백엔드, GET 만).

원본 로그 수천 줄을 agent 가 읽지 않고 "무엇이 얼마나 언제부터" 를 숫자로 안다. 계산은 서버(Loki)가 하거나 — `count_over_time`
matrix · Patterns API — 서버가 못 내는 것(예시 한 줄 · 첫 발생)만 `query_range` 한 줄 질의로 보탠다.

구조로 거는 것(obs.metrics 와 같다):
  - 순수 함수다 — HTTP 는 `Client` 주입(`Client.get` 만), fixture(서버 환원 응답)만으로 테스트된다.
  - 연산은 `(Evidence[V], 원본)` 을 돌려주고 거부는 `ObsError`(요청 전). 값 dataclass 는 frozen.
  - "데이터 없음" 과 "0" 을 구분한다 — 빈 목록 / None 이 없음이고, 0 은 0 으로 실린다.
  - 상한은 함수 안에서 — histogram 의 행 수(max_groups) · templates 의 군집 수(k) · around 의 쪽별 줄 수(limit).

Loki 응답 파서(`Stream` · `LogResult` · `parse_logs` · `LogEntry`)는 어댑터(cli)에서 이리로 옮겨 왔다 — cli 는 같은 이름을 import 한다.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from obs.core import JSON, Client, Evidence, ObsError, Params, Window, json_int, json_labels, json_list, json_obj, json_pair, json_str
from obs.metrics import Series, auto_step, check_status, parse_metric, series_items

DEFAULT_MAX_GROUPS: int = 20
DEFAULT_TEMPLATES_K: int = 10
DEFAULT_AROUND_LIMIT: int = 50
DEFAULT_AROUND_SPAN: str = "5m"


# ── 응답 dataclass + 파서 — Loki 로그 (이동: obs/adapters/cli.py) ───────────────────

@dataclass(frozen=True)
class Stream:
    """Loki 로그 스트림 — 레이블 집합과 (나노초, 줄) 목록."""
    labels: dict[str, str]
    values: list[tuple[int, str]]


@dataclass(frozen=True)
class LogResult:
    result_type: str            # streams · matrix
    streams: list[Stream]
    series: list[Series]


@dataclass(frozen=True)
class LogEntry:
    """로그 줄 하나 — 스트림을 펼친 것. (이동 · 승격: cli._Entry)"""
    ts_ns: int
    labels: dict[str, str]
    line: str


def parse_logs(body: dict[str, JSON]) -> LogResult:
    data = json_obj(body.get("data"))
    rtype = json_str(data.get("resultType"))
    if rtype == "matrix":
        return LogResult(result_type=rtype, streams=[], series=series_items(data.get("result")))
    streams: list[Stream] = []
    for item in json_list(data.get("result")):
        o = json_obj(item)
        values: list[tuple[int, str]] = []
        for v in json_list(o.get("values")):
            pr = json_pair(v)
            if pr is not None:
                values.append((json_int(pr[0]), pr[1]))
        streams.append(Stream(labels=json_labels(o.get("stream")), values=values))
    return LogResult(result_type=rtype, streams=streams, series=[])


# ── histogram ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GroupCount:
    """표의 행 하나 — `by` 값 하나(예: detected_level=error)."""
    labels: dict[str, str]
    total: int                          # 창 전체 건수 = counts 의 합
    counts: list[int]                   # 열 — Histogram.times 와 같은 길이 · 점이 없던 구간은 0


@dataclass(frozen=True)
class Histogram:
    """matrix 를 편 표 — 행 = by 값(groups) · 열 = 시간 구간(times)."""
    by: list[str]
    step: str                           # 구간 폭
    times: list[float]                  # 시간축 — 구간 시작 시각(unix 초), 모든 시계열의 시각 합집합 정렬
    groups: list[GroupCount]            # [] = 데이터 없음 (값 0 인 시계열은 total 0 인 행으로 실린다)
    folded: int                         # max_groups 에 밀려 표에서 뺀 행 수 · 0 = 없음


def histogram_query(selector: str, by: Sequence[str], step: str) -> str:
    """`sum by (<by>) (count_over_time(<selector>[<step>]))` — 축은 다듬어 쉼표로 잇는다. 축이 없으면 ObsError."""
    axes = [a.strip() for a in by if a.strip()]
    if not axes:
        raise ObsError("레이블 축(--by)이 필요하다 — 예: detected_level · app,pod")
    return "sum by (%s) (count_over_time(%s[%s]))" % (", ".join(axes), selector, step)


def histogram(client: Client, selector: str, window: Window, by: Sequence[str] = ("detected_level",),
              step: str | None = None, max_groups: int = DEFAULT_MAX_GROUPS) -> tuple[Evidence[Histogram], JSON]:
    """by 값별 · 구간별 건수 표 — `count_over_time` matrix 를 query_range 한 번으로 받아 편다. 행은 total 내림차순, max_groups 까지."""
    if not selector.strip():
        raise ObsError("LogQL selector 가 필요하다")
    if max_groups < 1:
        raise ObsError("max_groups 는 1 이상이어야 한다: %d" % max_groups)
    step = step or auto_step(window.seconds)
    q = histogram_query(selector, by, step)
    params: Params = {"start": int(window.start * 1e9), "end": int(window.end * 1e9), "query": q, "step": step}
    body = check_status(client.get("logs", "loki/api/v1/query_range", params).body, "logs")
    series = parse_logs(body).series
    times = sorted({ts for s in series for ts, _ in s.values})
    groups: list[GroupCount] = []
    for s in series:
        at = {ts: json_int(v) for ts, v in s.values}
        counts = [at.get(t, 0) for t in times]
        groups.append(GroupCount(labels=dict(s.labels), total=sum(counts), counts=counts))
    groups.sort(key=lambda g: -g.total)
    value = Histogram(by=[a.strip() for a in by if a.strip()], step=step, times=times, groups=groups[:max_groups],
                      folded=max(0, len(groups) - max_groups))
    return Evidence(backend="logs", query=q, window=window, value=value), body


# ── templates ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Sample:
    """Patterns API 의 sample 하나 — 한 구간(step)의 건수."""
    ts: float                           # unix 초 — 구간 시작
    count: int


@dataclass(frozen=True)
class Pattern:
    """Patterns API 응답 항목."""
    pattern: str
    samples: list[Sample]


@dataclass(frozen=True)
class Template:
    template: str
    count: int
    ratio: float
    first_ts: float                     # 건수 > 0 인 첫 sample 의 시각 — 정밀도는 step
    last_ts: float
    example: str | None                 # None = 예시 질의가 빈 스트림


@dataclass(frozen=True)
class Templates:
    k: int
    step: str                           # sample 구간 폭
    total: int
    clusters: list[Template]            # [] = 빈 군집
    other: int


def pattern_filter(selector: str, pattern: str) -> str:
    """`<selector> |> <패턴 리터럴>` — 백틱이 없으면 백틱 리터럴(escape 없음), 있으면 따옴표 리터럴(역슬래시 · 따옴표 escape)."""
    if not pattern.strip():
        raise ObsError("패턴(템플릿)이 필요하다")
    if "`" not in pattern:
        return "%s |> `%s`" % (selector, pattern)
    return '%s |> "%s"' % (selector, pattern.replace("\\", "\\\\").replace('"', '\\"'))


def parse_patterns(body: dict[str, JSON]) -> list[Pattern]:
    """Patterns API 응답(`data: [{pattern, samples: [[ts, count]…]}]`) → Pattern 목록, 응답 순서 그대로.
    [ts, count] 꼴이 아닌 sample 은 조용히 빼지 않고 ObsError 다 — 건수가 틀린 표보다 멈추는 편이 낫다."""
    out: list[Pattern] = []
    for item in json_list(body.get("data")):
        o = json_obj(item)
        samples: list[Sample] = []
        for v in json_list(o.get("samples")):
            pair = json_list(v)
            ts, n = (pair[0], pair[1]) if len(pair) == 2 else (None, None)
            if isinstance(ts, bool) or isinstance(n, bool) or not isinstance(ts, (int, float)) or not isinstance(n, (int, float)):
                raise ObsError("logs 백엔드의 Patterns API 응답이 형식([ts, count])을 벗어났다: sample %r — 사람에게" % (v,))
            samples.append(Sample(ts=float(ts), count=int(n)))
        out.append(Pattern(pattern=json_str(o.get("pattern")), samples=samples))
    return out


def clusters_of(patterns: Sequence[Pattern], k: int, step: str) -> Templates:
    """Pattern → 군집 — 건수가 있는 패턴만, count 내림차순 · template 사전순으로 k 개. 넘친 건수는 other. example 은 비워 둔다."""
    counted: list[Template] = []
    for p in patterns:
        hits = [x for x in p.samples if x.count > 0]
        if hits:
            counted.append(Template(template=p.pattern, count=sum(x.count for x in hits), ratio=0.0,
                                    first_ts=min(x.ts for x in hits), last_ts=max(x.ts for x in hits), example=None))
    total = sum(c.count for c in counted)
    counted.sort(key=lambda c: (-c.count, c.template))
    kept = [replace(c, ratio=c.count / total) for c in counted[:k]]
    return Templates(k=k, step=step, total=total, clusters=kept, other=sum(c.count for c in counted[k:]))


def _first_line(client: Client, query: str, window: Window) -> tuple[LogEntry | None, JSON]:
    """창 안에서 질의에 걸리는 가장 이른 줄 — query_range forward · limit 1. 없으면 None."""
    params: Params = {"start": int(window.start * 1e9), "end": int(window.end * 1e9), "query": query, "limit": 1, "direction": "forward"}
    body = check_status(client.get("logs", "loki/api/v1/query_range", params).body, "logs")
    entries = [LogEntry(ts, s.labels, line) for s in parse_logs(body).streams for ts, line in s.values]
    return (min(entries, key=lambda e: e.ts_ns) if entries else None), body


def templates(client: Client, selector: str, window: Window, k: int = DEFAULT_TEMPLATES_K,
              step: str | None = None) -> tuple[Evidence[Templates], JSON]:
    """창 안의 로그 군집 상위 k — Patterns API 한 번 + 남긴 군집마다 예시 한 줄(pattern_filter · forward · limit 1)."""
    if not selector.strip():
        raise ObsError("LogQL selector 가 필요하다")
    if k < 1:
        raise ObsError("k 는 1 이상이어야 한다: %d" % k)
    step = step or auto_step(window.seconds)
    params: Params = {"query": selector, "start": int(window.start * 1e9), "end": int(window.end * 1e9), "step": step}
    try:
        resp = client.get("logs", "loki/api/v1/patterns", params)
    except ObsError as e:
        if "(404)" in str(e):
            raise ObsError("logs 백엔드에 Patterns API 가 없다(404) — Loki 의 pattern_ingester 가 꺼져 있을 수 있다. 사람에게")
        raise
    body = check_status(resp.body, "logs")
    t = clusters_of(parse_patterns(body), k, step)
    clusters: list[Template] = []
    for c in t.clusters:
        first, _ = _first_line(client, pattern_filter(selector, c.template), window)
        clusters.append(replace(c, example=first.line if first else None))
    return Evidence(backend="logs", query=selector, window=window, value=replace(t, clusters=clusters)), body


# ── around ───────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Side:
    """ts 의 한쪽 — 앞(t < ts) · 뒤(t ≥ ts)."""
    shown: int                          # 받아 온 줄 수 (≤ limit)
    truncated: bool                     # 그 쪽 창 안에 보인 것보다 더 있다


@dataclass(frozen=True)
class Context:
    center: float
    span: float
    limit: int                          # 한 쪽 상한 — 앞 ≤ limit · 뒤 ≤ limit
    total: int                          # 창(ts±span) 전체 건수 — count 질의
    before: Side
    after: Side
    entries: list[LogEntry]             # 앞 + 뒤 · 시각 오름차순 · [] = 빈 문맥 · 길이 ≤ 2·limit

    @property
    def shown(self) -> int:
        return self.before.shown + self.after.shown

    @property
    def truncated(self) -> bool:
        return self.before.truncated or self.after.truncated


def count_query(selector: str, seconds: float) -> str:
    """`sum(count_over_time(<selector>[<n>s]))` — 창 끝에서 평가하면 창 전체 건수."""
    return "sum(count_over_time(%s[%ds]))" % (selector, int(round(seconds)))


def _side(client: Client, selector: str, start: float, end: float, direction: str, limit: int) -> tuple[list[LogEntry], Side, JSON]:
    """[start, end) 의 ts 쪽 줄 limit 개 — limit+1 개를 물어 넘치면 잘림이다. direction 이 ts 에 가까운 것부터 받게 한다."""
    params: Params = {"start": int(start * 1e9), "end": int(end * 1e9), "query": selector, "limit": limit + 1, "direction": direction}
    body = check_status(client.get("logs", "loki/api/v1/query_range", params).body, "logs")
    entries = [LogEntry(ts, s.labels, line) for s in parse_logs(body).streams for ts, line in s.values]
    entries.sort(key=lambda e: e.ts_ns, reverse=(direction == "backward"))
    kept = entries[:limit]
    return kept, Side(shown=len(kept), truncated=len(entries) > limit), body


def around(client: Client, selector: str, ts: float, span: float, limit: int = DEFAULT_AROUND_LIMIT) -> tuple[Evidence[Context], JSON]:
    """ts 앞뒤 span 의 문맥 — 창 전체 건수(count 질의) + 앞(backward [ts−span, ts)) · 뒤(forward [ts, ts+span)) 각 limit 줄."""
    if not selector.strip():
        raise ObsError("LogQL selector 가 필요하다")
    if span <= 0:
        raise ObsError("span 은 0 보다 커야 한다: %g" % span)
    if limit < 1:
        raise ObsError("limit 은 1 이상이어야 한다: %d" % limit)
    count_body = check_status(client.get("logs", "loki/api/v1/query", {"query": count_query(selector, 2 * span),
                                                                       "time": int((ts + span) * 1e9)}).body, "logs")
    total = sum(json_int(v) for s in parse_metric(count_body).series for _, v in s.values)
    before, before_side, before_body = _side(client, selector, ts - span, ts, "backward", limit)
    after, after_side, after_body = _side(client, selector, ts, ts + span, "forward", limit)
    entries = sorted([*before, *after], key=lambda e: e.ts_ns)
    value = Context(center=ts, span=span, limit=limit, total=total, before=before_side, after=after_side, entries=entries)
    raw: JSON = {"count": count_body, "before": before_body, "after": after_body}
    return Evidence(backend="logs", query=selector, window=Window(ts - span, ts + span), value=value), raw


# ── first_seen ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FirstSeen:
    template: str
    ts: float | None                    # None = 창 안에 없음
    line: str | None


def first_seen(client: Client, selector: str, template: str, window: Window) -> tuple[Evidence[FirstSeen], JSON]:
    """템플릿(패턴)에 걸리는 창 안의 첫 줄 — pattern_filter 질의를 forward · limit 1 로 한 번. 없으면 ts · line 이 None."""
    if not selector.strip():
        raise ObsError("LogQL selector 가 필요하다")
    q = pattern_filter(selector, template)
    first, body = _first_line(client, q, window)
    value = FirstSeen(template=template, ts=first.ts_ns / 1e9 if first else None, line=first.line if first else None)
    return Evidence(backend="logs", query=q, window=window, value=value), body
