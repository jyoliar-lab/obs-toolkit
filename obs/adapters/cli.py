"""obs.adapters.cli — Claude Code(셸 있는 호스트)용 명령 4개: promql · logql · traceql · obs-doctor.py.

호스트 특정 코드는 여기(어댑터)에만 있다 — stdout 절제(max_lines) · spill 경로 · exit 코드.
코어 함수는 dataclass 를 주고받는다. 각 `*_run` 은 Evidence 를 돌려주고, `*_main` 이 그것을 stdout 에 찍는다 —
셸 없는 소비자(MCP)는 `*_run` 만 다시 쓴다.

외부 경계 규약: 백엔드 응답(JSON)은 `parse_*` 가 받는 즉시 dataclass 로 옮기고, 포맷 함수는 그 타입만 받는다.
`Any` 는 쓰지 않는다.

agent 인터페이스에 물리(URL · uid · 토큰)가 없다. `--uid` 같은 인자를 두지 않는다 — 논리 이름은 명령 자체가 정한다
(promql → metrics · logql → logs · traceql → traces). 물리는 `obs-doctor.py` 만 사람에게 보인다.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from obs.core import (
    JSON, Client, DoctorReport, Evidence, Lines, ObsError, Params, Window, doctor, fmt_ts, load_config,
    parse_duration, parse_time, now, spill, spill_ref, to_json, token_from_env,
)

DEFAULT_MAX_LINES: int = 40
DEFAULT_LOG_LIMIT: int = 30

RunResult = tuple[Evidence[Lines], JSON, str]
"""(근거, 원본 body, spill 종류 이름)"""


# ── JSON 접근자 — 외부 경계에서 dataclass 로 옮길 때만 쓴다 ─────────────────────

def _obj(v: JSON) -> dict[str, JSON]:
    return v if isinstance(v, dict) else {}


def _list(v: JSON) -> list[JSON]:
    return v if isinstance(v, list) else []


def _str(v: JSON, default: str = "") -> str:
    if v is None:
        return default
    return v if isinstance(v, str) else str(v)


def _int(v: JSON) -> int:
    if isinstance(v, bool) or v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            return int(float(v))
        except ValueError:
            return 0
    return 0


def _labels(v: JSON) -> dict[str, str]:
    return {k: _str(x) for k, x in _obj(v).items()}


def _strs(v: JSON) -> list[str]:
    return [_str(x) for x in _list(v)]


def _pair(v: JSON) -> tuple[str, str] | None:
    """[ts, value] 쌍. 둘 다 문자열로 — 해석(초·나노초·수치)은 받는 쪽이 한다."""
    items = _list(v)
    if len(items) < 2:
        return None
    return _str(items[0]), _str(items[1])


# ── 응답 dataclass + 파서 — Prometheus · Loki ────────────────────────────────────

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


def check_status(body: JSON, backend: str) -> dict[str, JSON]:
    """Prometheus·Loki 형 응답의 `status: error` 를 논리 이름의 오류로. 통과하면 객체를 돌려준다."""
    if not isinstance(body, dict):
        raise ObsError("%s 백엔드가 JSON 이 아닌 응답을 냈다: %s" % (backend, clip(_str(body), 300)))
    if body.get("status") == "error":
        raise ObsError("%s 백엔드 질의 오류(%s): %s" % (backend, _str(body.get("errorType")), _str(body.get("error"))))
    for w in _strs(body.get("warnings")):
        print("warning: %s" % w, file=sys.stderr)
    return body


def _series_items(result: JSON) -> list[Series]:
    out: list[Series] = []
    for item in _list(result):
        o = _obj(item)
        pairs: list[tuple[str, str]] = []
        single = _pair(o.get("value"))
        if single is not None:
            pairs.append(single)
        for v in _list(o.get("values")):
            pr = _pair(v)
            if pr is not None:
                pairs.append(pr)
        values: list[tuple[float, str]] = []
        for ts, val in pairs:
            try:
                values.append((float(ts), val))
            except ValueError:
                continue
        out.append(Series(labels=_labels(o.get("metric")), values=values))
    return out


def parse_metric(body: dict[str, JSON]) -> MetricResult:
    data = _obj(body.get("data"))
    rtype = _str(data.get("resultType"))
    result = data.get("result")
    if rtype in ("scalar", "string"):
        pr = _pair(result)
        return MetricResult(result_type=rtype, scalar=pr[1] if pr else None, series=[])
    return MetricResult(result_type=rtype, scalar=None, series=_series_items(result))


def parse_strings(body: dict[str, JSON]) -> list[str]:
    """labels · label values 응답 — `data: [str]`."""
    return _strs(body.get("data"))


def parse_label_sets(body: dict[str, JSON]) -> list[dict[str, str]]:
    """series 응답 — `data: [{label: value}]`."""
    return [_labels(m) for m in _list(body.get("data"))]


def parse_logs(body: dict[str, JSON]) -> LogResult:
    data = _obj(body.get("data"))
    rtype = _str(data.get("resultType"))
    if rtype == "matrix":
        return LogResult(result_type=rtype, streams=[], series=_series_items(data.get("result")))
    streams: list[Stream] = []
    for item in _list(data.get("result")):
        o = _obj(item)
        values: list[tuple[int, str]] = []
        for v in _list(o.get("values")):
            pr = _pair(v)
            if pr is not None:
                values.append((_int(pr[0]), pr[1]))
        streams.append(Stream(labels=_labels(o.get("stream")), values=values))
    return LogResult(result_type=rtype, streams=streams, series=[])


# ── 응답 dataclass + 파서 — Tempo ───────────────────────────────────────────────

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


def check_json(body: JSON, backend: str) -> dict[str, JSON]:
    if not isinstance(body, dict):
        raise ObsError("%s 백엔드가 JSON 이 아닌 응답을 냈다: %s" % (backend, clip(_str(body), 300)))
    return body


def parse_search(body: dict[str, JSON]) -> list[TraceHit]:
    hits: list[TraceHit] = []
    for t in _list(body.get("traces")):
        o = _obj(t)
        hits.append(TraceHit(
            trace_id=_str(o.get("traceID"), "?"), duration_ms=_str(o.get("durationMs"), "?"),
            start_ns=_int(o.get("startTimeUnixNano")),
            root_service=_str(o.get("rootServiceName"), "?"), root_name=_str(o.get("rootTraceName"), "?")))
    return hits


def _attr_value(v: JSON) -> str:
    o = _obj(v)
    for k in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if k in o:
            return _str(o[k])
    return _str(v)


def _attrs(items: JSON) -> dict[str, str]:
    out: dict[str, str] = {}
    for a in _list(items):
        o = _obj(a)
        out[_str(o.get("key"), "?")] = _attr_value(o.get("value"))
    return out


def parse_spans(body: dict[str, JSON]) -> list[Span]:
    """OTLP JSON — Tempo 는 `batches`(v1) 또는 `resourceSpans`(v2) 로 준다."""
    batches = body.get("batches") or body.get("resourceSpans") or _obj(body.get("trace")).get("resourceSpans")
    spans: list[Span] = []
    for b in _list(batches):
        bo = _obj(b)
        service = _attrs(_obj(bo.get("resource")).get("attributes")).get("service.name", "?")
        for ss in _list(bo.get("scopeSpans") or bo.get("instrumentationLibrarySpans")):
            for s in _list(_obj(ss).get("spans")):
                so = _obj(s)
                spans.append(Span(
                    service=service, name=_str(so.get("name"), "?"), span_id=_str(so.get("spanId")),
                    parent_id=_str(so.get("parentSpanId")),
                    start_ns=_int(so.get("startTimeUnixNano")), end_ns=_int(so.get("endTimeUnixNano")),
                    status_code=_str(_obj(so.get("status")).get("code")), attrs=_attrs(so.get("attributes")),
                ))
    return spans


def parse_tags(body: dict[str, JSON]) -> list[TagScope]:
    return [TagScope(name=_str(_obj(sc).get("name")), tags=_strs(_obj(sc).get("tags"))) for sc in _list(body.get("scopes"))]


def parse_tag_values(body: dict[str, JSON]) -> list[str]:
    return [_str(_obj(v).get("value")) for v in _list(body.get("tagValues"))]


# ── 출력 절제 ────────────────────────────────────────────────────────────────────

@dataclass
class Emitted:
    """stdout 에 찍은 것과 spill 결과 — 테스트가 검사한다."""
    lines: list[str]
    cut: int
    raw_ref: str | None


def emit(lines: list[str], full: object, kind: str, max_lines: int, force_spill: bool = False,
         env: Mapping[str, str] | None = None, out: Callable[[str], None] = print) -> Emitted:
    """앞 max_lines 줄만 stdout 에. 잘렸거나 force_spill 이면 원본을 파일로 떨어뜨리고 경로를 알린다."""
    shown = lines[:max_lines]
    for line in shown:
        out(line)
    cut = len(lines) - len(shown)
    raw_ref: str | None = None
    if cut > 0 or force_spill:
        raw_ref = spill_ref(spill(kind, full, env), env)
        if cut > 0:
            out("… %d줄 더 있음 (총 %d줄). 원본 전체: %s" % (cut, len(lines), raw_ref))
        else:
            out("원본 전체: %s" % raw_ref)
    return Emitted(lines=shown, cut=cut, raw_ref=raw_ref)


def clip(text: str, width: int) -> str:
    text = text.replace("\n", "⏎")
    return text if len(text) <= width else text[: width - 1] + "…"


def labels_str(labels: Mapping[str, str], drop: tuple[str, ...] = ()) -> str:
    items = ["%s=%s" % (k, v) for k, v in sorted(labels.items()) if k not in drop]
    return "{" + ", ".join(items) + "}"


def fmt_val(v: str | int | float) -> str:
    try:
        f = float(v)
    except ValueError:
        return str(v)
    if f != f:  # NaN
        return "NaN"
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return "%.6g" % f


def _finish(ev: Evidence[Lines], body: JSON, kind: str, max_lines: int, as_json: bool, force_spill: bool = False) -> None:
    """Evidence 를 stdout 규약으로. as_json 은 원본만 파일로."""
    if as_json:
        print("원본 전체: %s" % spill_ref(spill(kind, body)))
        return
    emit(ev.value.lines or ["(결과 없음)"], body, kind, max_lines, force_spill=force_spill)


def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES,
                    help="stdout 줄 수 상한. 기본 %d — 넘치면 원본은 tmp/obs/ 파일로" % DEFAULT_MAX_LINES)
    ap.add_argument("--json", action="store_true", help="요약 대신 원본을 파일로 떨어뜨리고 경로만 출력")


def _client() -> Client:
    return Client.from_env()


def main_guard(fn: Callable[[], None]) -> None:
    """진입점 — ObsError 는 메시지만 stderr 로, exit 1. 그 외 예외는 그대로."""
    try:
        fn()
    except ObsError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def _evidence(backend: str, query: str, window: Window, lines: list[str]) -> Evidence[Lines]:
    return Evidence(backend=backend, query=query, window=window, value=Lines(lines or ["(결과 없음)"]))


# ── promql ───────────────────────────────────────────────────────────────────────

PROMQL_DOC = """promql — 즉시 조회·구간 조회 (metrics 백엔드, GET 만).

사용:
  promql.py '<PromQL>'                                  즉시 조회
  promql.py '<PromQL>' --range 1h [--step 60s]           구간 조회 — 시계열별 요약
  promql.py '<PromQL>' --range 1h --points               구간 조회 — 시각별 값 전부 (많다 — 파일로 떨어진다)
  promql.py --labels [<selector>]                        레이블 이름 목록
  promql.py --label-values <name> [<selector>]           레이블 값 목록
  promql.py --series '<selector>' [--range 1h]           시계열 목록

옵션:
  --end <시각>      기준 시각 (now · -10m · unix초 · RFC3339). 기본 now
  --range <기간>    구간 길이 (5m · 1h · 2d). 있으면 구간 조회
  --step <기간>     구간 해상도. 기본 range/60 에 가까운 값
  --max-lines <n>   stdout 줄 수 상한. 기본 40 — 넘치면 원본은 tmp/obs/ 파일로
  --json            요약 대신 원본을 파일로 떨어뜨리고 경로만 출력

출력은 요약이다 — 즉시 조회는 `값  {레이블}` 한 줄씩, 구간 조회는 시계열마다
`n=점수 min max first last {레이블}` 한 줄. 원본이 필요하면 --json.
"""


def instant_lines(res: MetricResult) -> list[str]:
    if res.scalar is not None:
        return [fmt_val(res.scalar)]
    lines: list[str] = []
    for s in res.series:
        if len(s.values) == 1:
            lines.append("%s  %s" % (fmt_val(s.values[0][1]), labels_str(s.labels)))
        elif s.values:   # 즉시 조회에 range vector 를 준 경우
            vals = [fmt_val(v) for _, v in s.values]
            lines.append("[%s]  %s" % (", ".join(vals[:10]) + (" …" if len(vals) > 10 else ""), labels_str(s.labels)))
    return lines or ["(결과 없음)"]


def range_lines(res: MetricResult, points: bool) -> list[str]:
    lines: list[str] = []
    for s in res.series:
        labels = labels_str(s.labels)
        if points:
            lines.append(labels)
            for ts, v in s.values:
                lines.append("  %s  %s" % (fmt_ts(ts), fmt_val(v)))
            continue
        nums: list[float] = []
        for _, v in s.values:
            try:
                nums.append(float(v))
            except ValueError:
                pass
        if not nums:
            lines.append("n=0  %s" % labels)
            continue
        lines.append("n=%d  min=%s  max=%s  first=%s  last=%s  %s" % (
            len(nums), fmt_val(min(nums)), fmt_val(max(nums)), fmt_val(nums[0]), fmt_val(nums[-1]), labels))
    return lines or ["(결과 없음)"]


def auto_step(range_sec: float) -> str:
    return "%ds" % max(15, int(range_sec / 60))


@dataclass
class PromqlArgs:
    query: str | None = None
    end: str | None = None
    range_: str | None = None
    step: str | None = None
    points: bool = False
    labels: bool = False
    label_values: str | None = None
    series: str | None = None


def promql_run(client: Client, a: PromqlArgs, at: float | None = None) -> RunResult:
    """→ (Evidence, 원본 body, kind). stdout 을 만지지 않는다."""
    end = parse_time(a.end, now() if at is None else at)
    start = end - parse_duration(a.range_) if a.range_ else None
    window = Window(start=start if start is not None else end, end=end)
    match = a.query

    if a.labels:
        params: Params = {"match[]": match, "start": start, "end": end}
        body = check_status(client.get("metrics", "api/v1/labels", params).body, "metrics")
        lines = sorted(parse_strings(body))
        kind, query = "promql-labels", "labels(%s)" % (match or "")
    elif a.label_values:
        params = {"match[]": match, "start": start, "end": end}
        body = check_status(client.get("metrics", "api/v1/label/%s/values" % a.label_values, params).body, "metrics")
        lines = sorted(parse_strings(body))
        kind, query = "promql-label-values", "label_values(%s, %s)" % (a.label_values, match or "")
    elif a.series:
        params = {"match[]": a.series, "start": start if start is not None else end - 3600, "end": end}
        body = check_status(client.get("metrics", "api/v1/series", params).body, "metrics")
        lines = [labels_str(m) for m in parse_label_sets(body)]
        kind, query = "promql-series", "series(%s)" % a.series
    elif not match:
        raise ObsError("PromQL 이 필요하다")
    elif start is not None:
        step = a.step or auto_step(end - start)
        params = {"query": match, "start": start, "end": end, "step": step}
        body = check_status(client.get("metrics", "api/v1/query_range", params).body, "metrics")
        lines = range_lines(parse_metric(body), a.points)
        kind, query = "promql-range", match
    else:
        body = check_status(client.get("metrics", "api/v1/query", {"query": match, "time": end}).body, "metrics")
        lines = instant_lines(parse_metric(body))
        kind, query = "promql", match

    return _evidence("metrics", query, window, lines), body, kind


def promql_main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="promql.py", description=PROMQL_DOC,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?", help="PromQL (또는 --labels/--label-values/--series 의 selector)")
    ap.add_argument("--end", default=None)
    ap.add_argument("--range", dest="range_", default=None)
    ap.add_argument("--step", default=None)
    ap.add_argument("--points", action="store_true", help="구간 조회에서 시각별 값을 전부 출력")
    ap.add_argument("--labels", action="store_true")
    ap.add_argument("--label-values", metavar="NAME", default=None)
    ap.add_argument("--series", metavar="SELECTOR", default=None)
    _common(ap)
    a = ap.parse_args(argv)
    args = PromqlArgs(query=a.query, end=a.end, range_=a.range_, step=a.step, points=a.points,
                      labels=a.labels, label_values=a.label_values, series=a.series)
    ev, body, kind = promql_run(_client(), args)
    _finish(ev, body, kind, a.max_lines, a.json)


# ── logql ────────────────────────────────────────────────────────────────────────

LOGQL_DOC = """logql — 로그 조회 (logs 백엔드, GET 만).

사용:
  logql.py '<LogQL>' [--since 1h] [--limit 30]           로그 줄
  logql.py '<metric LogQL>' --since 1h                    메트릭 질의(count_over_time 등) — 시계열 요약
  logql.py --labels                                       레이블 이름
  logql.py --label-values <name> [--since 1h]             레이블 값
  logql.py --series '<selector>' [--since 1h]             스트림 목록

옵션:
  --since <기간>     조회 구간 길이 (5m · 1h · 2d). 기본 1h
  --end <시각>       구간 끝 (now · -10m · unix초 · RFC3339). 기본 now
  --limit <n>        돌려받을 최대 줄 수. 기본 30 — 낮게 둔다. 더 필요하면 명시적으로 올린다
  --direction        backward(최신부터, 기본) · forward
  --width <n>        한 줄 표시 폭. 기본 200 — 넘치면 자른다
  --max-lines <n>    stdout 줄 수 상한. 기본 40 — 넘치면 원본은 tmp/obs/ 파일로
  --json             원본을 파일로 떨어뜨리고 경로만 출력

테넌트는 서버가 얹는다 — 여기서 다루지 않는다.
출력은 `시각  {스트림 레이블 차이}  줄` — 모든 스트림에 공통인 레이블은 머리에 한 번만 찍는다.
"""


def common_labels(streams: Sequence[Stream]) -> dict[str, str]:
    if not streams:
        return {}
    common = dict(streams[0].labels)
    for s in streams[1:]:
        for k in list(common):
            if s.labels.get(k) != common[k]:
                del common[k]
    return common


@dataclass(frozen=True)
class _Entry:
    ts_ns: int
    labels: dict[str, str]
    line: str


def stream_lines(res: LogResult, direction: str, width: int) -> list[str]:
    common = common_labels(res.streams)
    entries: list[_Entry] = []
    for s in res.streams:
        labels = {k: v for k, v in s.labels.items() if k not in common}
        for ts, line in s.values:
            entries.append(_Entry(ts, labels, line))
    entries.sort(key=lambda e: e.ts_ns, reverse=(direction == "backward"))
    lines: list[str] = []
    if common:
        lines.append("공통 레이블 %s" % labels_str(common))
    for e in entries:
        tag = (" " + labels_str(e.labels)) if e.labels else ""
        lines.append("%s%s  %s" % (fmt_ts(e.ts_ns / 1e9), tag, clip(e.line, width)))
    if not entries:
        lines.append("(결과 없음)")
    return lines


def matrix_lines(res: LogResult) -> list[str]:
    lines: list[str] = []
    for s in res.series:
        nums: list[float] = []
        for _, v in s.values:
            try:
                nums.append(float(v))
            except ValueError:
                pass
        labels = labels_str(s.labels)
        if nums:
            lines.append("n=%d  min=%g  max=%g  last=%g  %s" % (len(nums), min(nums), max(nums), nums[-1], labels))
        else:
            lines.append("n=0  %s" % labels)
    return lines or ["(결과 없음)"]


@dataclass
class LogqlArgs:
    query: str | None = None
    since: str = "1h"
    end: str | None = None
    limit: int = DEFAULT_LOG_LIMIT
    direction: str = "backward"
    width: int = 200
    labels: bool = False
    label_values: str | None = None
    series: str | None = None


def logql_run(client: Client, a: LogqlArgs, at: float | None = None) -> RunResult:
    window = Window.last(a.since, a.end, at)
    ns: Params = {"start": int(window.start * 1e9), "end": int(window.end * 1e9)}

    if a.labels:
        body = check_status(client.get("logs", "loki/api/v1/labels", ns).body, "logs")
        lines = sorted(parse_strings(body))
        kind, query = "logql-labels", "labels()"
    elif a.label_values:
        body = check_status(client.get("logs", "loki/api/v1/label/%s/values" % a.label_values, ns).body, "logs")
        lines = sorted(parse_strings(body))
        kind, query = "logql-label-values", "label_values(%s)" % a.label_values
    elif a.series:
        body = check_status(client.get("logs", "loki/api/v1/series", {**ns, "match[]": a.series}).body, "logs")
        lines = [labels_str(m) for m in parse_label_sets(body)]
        kind, query = "logql-series", "series(%s)" % a.series
    elif not a.query:
        raise ObsError("LogQL 이 필요하다")
    else:
        params: Params = {**ns, "query": a.query, "limit": a.limit, "direction": a.direction}
        body = check_status(client.get("logs", "loki/api/v1/query_range", params).body, "logs")
        res = parse_logs(body)
        lines = matrix_lines(res) if res.result_type == "matrix" else stream_lines(res, a.direction, a.width)
        kind, query = "logql", a.query

    return _evidence("logs", query, window, lines), body, kind


def logql_main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="logql.py", description=LOGQL_DOC,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?", help="LogQL")
    ap.add_argument("--since", default="1h")
    ap.add_argument("--end", default=None)
    ap.add_argument("--limit", type=int, default=DEFAULT_LOG_LIMIT)
    ap.add_argument("--direction", choices=("backward", "forward"), default="backward")
    ap.add_argument("--width", type=int, default=200)
    ap.add_argument("--labels", action="store_true")
    ap.add_argument("--label-values", metavar="NAME", default=None)
    ap.add_argument("--series", metavar="SELECTOR", default=None)
    _common(ap)
    a = ap.parse_args(argv)
    args = LogqlArgs(query=a.query, since=a.since, end=a.end, limit=a.limit, direction=a.direction,
                     width=a.width, labels=a.labels, label_values=a.label_values, series=a.series)
    ev, body, kind = logql_run(_client(), args)
    _finish(ev, body, kind, a.max_lines, a.json)


# ── traceql ──────────────────────────────────────────────────────────────────────

TRACEQL_DOC = """traceql — 트레이스 검색·조회 (traces 백엔드, GET 만).

사용:
  traceql.py search [--q '<TraceQL>'] [--since 1h] [--limit 20]     최근 트레이스 목록
  traceql.py trace <traceID>                                        트레이스 하나 — 요약
  traceql.py tags [--scope span|resource|intrinsic]                 검색 가능한 태그
  traceql.py tag-values <tag> [--q '<TraceQL>']                     태그 값

옵션:
  --q <TraceQL>      검색식. 예: '{ resource.service.name = "posting-service" && status = error }'
                     비우면 구간 안의 모든 트레이스(최근순)
  --since <기간>     조회 구간 길이. 기본 1h        --end <시각>  구간 끝. 기본 now
  --limit <n>        search 결과 상한. 기본 20
  --min-duration     예: 500ms · 2s (search)
  --top <n>          trace 요약에서 보이는 느린 span 수. 기본 15
  --max-lines <n>    stdout 줄 수 상한. 기본 40 — 넘치면 원본은 tmp/obs/ 파일로
  --json             원본을 파일로 떨어뜨리고 경로만 출력

trace 는 원본이 항상 크므로 요약(서비스·span 수·오류·느린 span)만 찍고 원본은 늘 파일로 떨어진다.
"""


def search_lines(hits: Sequence[TraceHit]) -> list[str]:
    lines = ["%s  %6sms  %s  %s / %s" % (h.trace_id, h.duration_ms, fmt_ts(h.start_ns / 1e9) if h.start_ns else "?",
                                         h.root_service, h.root_name) for h in hits]
    return lines or ["(결과 없음 — 트레이스는 드물다. 창을 넓혀 다시: --since 24h · 7d)"]


def trace_lines(trace_id: str, spans: Sequence[Span], top: int) -> list[str]:
    if not spans:
        return ["트레이스 %s — span 없음" % trace_id]
    start = min(s.start_ns for s in spans)
    end = max(s.end_ns for s in spans)
    roots = [s for s in spans if not s.parent_id]
    errors = [s for s in spans if s.is_error]
    services: dict[str, int] = {}
    for s in spans:
        services[s.service] = services.get(s.service, 0) + 1
    lines = [
        "트레이스 %s  총 %.1fms  span %d개  시작 %s" % (trace_id, (end - start) / 1e6, len(spans), fmt_ts(start / 1e9)),
        "루트: " + ("; ".join("%s / %s" % (r.service, r.name) for r in roots) or "(없음)"),
        "서비스: " + ", ".join("%s(%d)" % kv for kv in sorted(services.items(), key=lambda kv: -kv[1])),
        "오류 span: %d" % len(errors),
    ]
    for s in errors[:10]:
        msg = s.attrs.get("exception.message") or s.attrs.get("error.message") or s.attrs.get("http.status_code") or ""
        lines.append("  ✗ %s / %s  %.1fms  %s" % (s.service, s.name, s.duration_ms, clip(msg, 120)))
    lines.append("느린 span top %d:" % top)
    for s in sorted(spans, key=lambda s: -s.duration_ms)[:top]:
        offset = (s.start_ns - start) / 1e6
        lines.append("  %8.1fms  +%7.1fms  %s / %s" % (s.duration_ms, offset, s.service, clip(s.name, 80)))
    return lines


def tags_lines(scopes: Sequence[TagScope]) -> list[str]:
    return ["[%s] %s" % (sc.name, ", ".join(sorted(sc.tags))) for sc in scopes] or ["(결과 없음)"]


@dataclass
class TraceqlArgs:
    cmd: str
    arg: str | None = None
    q: str | None = None
    since: str = "1h"
    end: str | None = None
    limit: int = 20
    min_duration: str | None = None
    scope: str | None = None
    top: int = 15


def traceql_run(client: Client, a: TraceqlArgs, at: float | None = None) -> RunResult:
    window = Window.last(a.since, a.end, at)
    w: Params = {"start": int(window.start), "end": int(window.end)}

    if a.cmd == "search":
        params: Params = {**w, "limit": a.limit, "q": a.q, "minDuration": a.min_duration}
        body = check_json(client.get("traces", "api/search", params).body, "traces")
        lines = search_lines(parse_search(body))
        kind, query = "traceql-search", "search(%s)" % (a.q or "")
    elif a.cmd == "trace":
        if not a.arg:
            raise ObsError("traceID 가 필요하다")
        body = check_json(client.get("traces", "api/traces/%s" % a.arg).body, "traces")
        lines = trace_lines(a.arg, parse_spans(body), a.top)
        kind, query = "traceql-trace", "trace(%s)" % a.arg
    elif a.cmd == "tags":
        body = check_json(client.get("traces", "api/v2/search/tags", {**w, "scope": a.scope}).body, "traces")
        lines = tags_lines(parse_tags(body))
        kind, query = "traceql-tags", "tags(%s)" % (a.scope or "")
    elif a.cmd == "tag-values":
        if not a.arg:
            raise ObsError("태그 이름이 필요하다")
        body = check_json(client.get("traces", "api/v2/search/tag/%s/values" % a.arg, {**w, "q": a.q}).body, "traces")
        lines = sorted(parse_tag_values(body))
        kind, query = "traceql-tag-values", "tag_values(%s, %s)" % (a.arg, a.q or "")
    else:
        raise ObsError("알 수 없는 명령 %r — search · trace · tags · tag-values" % a.cmd)

    return _evidence("traces", query, window, lines), body, kind


def traceql_main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="traceql.py", description=TRACEQL_DOC,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("search", "trace", "tags", "tag-values"))
    ap.add_argument("arg", nargs="?", help="trace: traceID · tag-values: 태그 이름")
    ap.add_argument("--q", default=None)
    ap.add_argument("--since", default="1h")
    ap.add_argument("--end", default=None)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--min-duration", default=None)
    ap.add_argument("--scope", choices=("span", "resource", "intrinsic"), default=None)
    ap.add_argument("--top", type=int, default=15)
    _common(ap)
    a = ap.parse_args(argv)
    args = TraceqlArgs(cmd=a.cmd, arg=a.arg, q=a.q, since=a.since, end=a.end, limit=a.limit,
                       min_duration=a.min_duration, scope=a.scope, top=a.top)
    ev, body, kind = traceql_run(_client(), args)
    _finish(ev, body, kind, a.max_lines, a.json, force_spill=(a.cmd == "trace"))


# ── obs-doctor.py — 사람용. 물리가 보이는 유일한 출력 ───────────────────────────────

DOCTOR_DOC = """obs-doctor.py — 사람용 진단. 해석된 물리 경로(URL·uid) · 설정 출처 · 연결 · 인증 상태를 보여준다.

agent 가 받는 오류("metrics 백엔드 인증 실패(401) — 사람에게")의 원인을 사람이 여기서 본다.
토큰 값은 절대 보이지 않는다 — 있음/없음만.

  obs-doctor.py            진단 실행
  obs-doctor.py --json     같은 내용을 JSON 으로
"""


def doctor_lines(rep: DoctorReport) -> list[str]:
    lines = [
        "grafana_url   %s  (%s)" % (rep.grafana_url.value, rep.grafana_url.source),
        "proxy_prefix  %s  (%s)" % (rep.proxy_prefix.value, rep.proxy_prefix.source),
    ]
    for name, res in rep.uids.items():
        lines.append("uid %-8s  %s  (%s)" % (name, res.value, res.source))
    lines.append("config_file   %s" % (rep.config_path or "(없음 — 후보: %s)" % ", ".join(rep.config_candidates)))
    lines.append("token         %s" % ("있음 (env)" if rep.token_present else "없음 — 사람에게: OBS_GRAFANA_TOKEN"))
    lines.append("spill_dir     %s" % rep.spill_dir)
    lines.append("")
    for p in rep.probes:
        mark = "✓" if p.ok else "✗"
        status = ("HTTP %d" % p.status) if p.status is not None else "-"
        lines.append("%s %-8s %s  %s" % (mark, p.backend, status, p.url))
        if not p.ok:
            lines.append("    %s" % p.detail)
    lines.append("")
    lines.append("결론: %s" % ("정상" if rep.ok else "문제 있음 — 위 ✗ 항목"))
    return lines


def doctor_main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="obs-doctor.py", description=DOCTOR_DOC,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = doctor(load_config(), token_from_env())
    if a.json:
        print(to_json(rep))
    else:
        for line in doctor_lines(rep):
            print(line)
    sys.exit(0 if rep.ok else 1)
