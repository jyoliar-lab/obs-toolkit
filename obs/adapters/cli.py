"""obs.adapters.cli — Claude Code(셸 있는 호스트)용 명령 4개: promql · logql · traceql · obs-doctor.py.

호스트 특정 코드는 여기(어댑터)에만 있다 — stdout 절제(max_lines) · spill 경로 · exit 코드.
코어 함수는 dataclass 를 주고받는다. 각 `*_run` 은 Evidence 를 돌려주고, `*_main` 이 그것을 stdout 에 찍는다 —
셸 없는 소비자(MCP)는 `*_run` 만 다시 쓴다.

외부 경계 규약: 백엔드 응답(JSON)은 `parse_*` 가 받는 즉시 dataclass 로 옮기고, 포맷 함수는 그 타입만 받는다.
`Any` 는 쓰지 않는다. 접근자(`json_obj` …)는 obs.core 에, Prometheus 형 파서(`parse_metric` · `check_status`)는
obs.metrics 에 있다 — 환원 연산이 코어에서 같은 파서를 쓴다.

환원 연산(summarize · compare · top · red — obs.metrics)은 promql.py 의 서브커맨드다: `promql.py compare '<PromQL>' --since 1h`.
`metrics_run` 이 Evidence[…] 를 받아 줄로 렌더링한다 — 계산은 코어, 표는 여기.

agent 인터페이스에 물리(URL · uid · 토큰)가 없다. `--uid` 같은 인자를 두지 않는다 — 논리 이름은 명령 자체가 정한다
(promql → metrics · logql → logs · traceql → traces). 물리는 `obs-doctor.py` 만 사람에게 보인다.
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Sequence, TypeAlias, TypeVar

from obs.adapters.cli_argparse import (
    DEFAULT_LOG_LIMIT, DEFAULT_MAX_LINES, DEFAULT_SINCE, LOGS_SUBCOMMANDS, METRICS_SUBCOMMANDS, TRACES_SUBCOMMANDS,
    logql_parser, logs_parser, metrics_parser, promql_parser, traceql_parser, traces_parser,
)
from obs.core import (
    JSON, Client, DoctorReport, Evidence, Lines, ObsError, Params, Response, Transport, Window, doctor, fmt_ts, load_config,
    SPILL_RAW, SPILL_SUMMARY, json_labels, json_list, json_strs,
    parse_duration, parse_time, project_dir, spill, spill_ref, to_json, token_from_env,
)
from obs.logs import (
    DEFAULT_AROUND_LIMIT, DEFAULT_AROUND_SPAN, DEFAULT_TEMPLATES_K, Context, FirstSeen, Histogram, LogEntry, LogResult, Stream,
    Side, Templates, around, first_seen, histogram, parse_logs, templates,
)
from obs.metrics import (
    DEFAULT_BASELINE, DEFAULT_TOP_AGG, DEFAULT_TOP_K, RED_IDLE, RED_MISSING, RED_QUANTILE, Comparison, MetricResult, Red,
    Signal, Summary, Top, auto_step, check_status, compare, parse_metric, red, summarize, top,
)
from obs.traces import (
    DEFAULT_BASELINE_RANGE, DEFAULT_EXEMPLARS_K, DEFAULT_QUANTILE, DEFAULT_SEARCH_LIMIT, CriticalPath, Edge, Exemplars, ServiceGraph, Span, SpanTime, TagScope,
    TraceDiff, TraceHit, check_json, critical_path, diff, exemplars, parse_search, parse_spans, parse_tag_values, parse_tags, service_graph,
)

RunResult: TypeAlias = "tuple[Evidence[Lines], str, Window]"   # 원본 자리 없음 — 보낸 호출의 기록은 Client.log
"""(근거, spill 종류 이름, CLI 가 인자로 계산한 창)"""


# ── 응답 dataclass + 파서 — Loki 의 로그 파서(Stream · LogResult · parse_logs)는 obs.logs 에, Prometheus 형은 obs.metrics 에 ──

def parse_strings(body: dict[str, JSON]) -> list[str]:
    """labels · label values 응답 — `data: [str]`."""
    return json_strs(body.get("data"))


def parse_label_sets(body: dict[str, JSON]) -> list[dict[str, str]]:
    """series 응답 — `data: [{label: value}]`."""
    return [json_labels(m) for m in json_list(body.get("data"))]


# ── 응답 dataclass + 파서 — Tempo 는 obs.traces 로 이동(Span · parse_spans · TraceHit · parse_search · TagScope · parse_tags · parse_tag_values · check_json) ──


# ── 출력 절제 ────────────────────────────────────────────────────────────────────

@dataclass
class Emitted:
    """stdout 에 찍은 것과 호출 디렉터리 — 테스트가 검사한다."""
    lines: list[str]
    cut: int
    ref: str          # 호출 디렉터리(프로젝트 기준 상대 경로) — raw.json · summary.txt 가 그 안에


def emit(lines: list[str], full: object, kind: str, max_lines: int, *, base: Path, at: float, quiet: bool = False,
         out: Callable[[str], None] = print, preamble: Sequence[str] = ()) -> Emitted:
    """항상 호출 디렉터리를 만든다(원본 raw.json · 요약 전체 summary.txt). stdout 에는 앞 max_lines 줄만 —
    quiet 면 요약을 찍지 않고 경로만. 잘렸으면 몇 줄이 더 있는지 알린다.
    `preamble` 은 summary.txt 의 머리에만 쓴다(창 · 기준선 — stdout 에는 찍지 않는다)."""
    ref = spill_ref(spill(kind, full, [*preamble, *lines] if preamble else lines, base, at), base)
    shown = [] if quiet else lines[:max_lines]
    for line in shown:
        out(line)
    cut = len(lines) - len(shown)
    where = "%s/ (%s · %s)" % (ref, SPILL_RAW, SPILL_SUMMARY)
    if cut > 0 and not quiet:
        out("… %d줄 더 있음 (총 %d줄). 원본·요약: %s" % (cut, len(lines), where))
    else:
        out("원본·요약: %s" % where)
    return Emitted(lines=shown, cut=cut, ref=ref)


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


def _window_line(tag: str, w: Window) -> str:
    return "# %s  %s  (start=%.3f end=%.3f)" % (tag, w.label(), w.start, w.end)


def window_preamble(window: Window) -> list[str]:
    """summary.txt 머리 — CLI 가 인자로 계산한 창(RFC3339 + unix 초)과 기준선. 보낸 질의는 원본(raw.json)에 있다."""
    lines = [_window_line("창", window)]
    if window.baseline is not None:
        lines.append(_window_line("기준선", window.baseline))
    return [*lines, ""]


def _finish(ev: Evidence[Lines], raw: list[Response], kind: str, window: Window, max_lines: int, as_json: bool, *,
            base: Path, at: float, out: Callable[[str], None] = print) -> None:
    """Evidence 를 stdout 규약으로. as_json 은 stdout 에 요약을 찍지 않고 경로만(파일은 어느 쪽이든 같다).
    summary.txt 는 머리(창) + 요약 전체다."""
    emit(ev.value.lines, raw, kind, max_lines, base=base, at=at, quiet=as_json, out=out, preamble=window_preamble(window))


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


def _spill_failed(client: Client, failed_kind: str, error: BaseException, base: Path, at: float) -> str:
    """실패한 run 의 client.log 를 <failed_kind> 호출 디렉터리로 — summary.txt 는 오류 문구. 안내할 경로 문구를 돌려준다."""
    ref = spill_ref(spill(failed_kind, client.log, [str(error)], base, at), base)
    return "%s/ (%s · %s)" % (ref, SPILL_RAW, SPILL_SUMMARY)


def finish(client: Client, run: Callable[[], RunResult], failed_kind: str, max_lines: int, as_json: bool, *,
           base: Path, at: float, out: Callable[[str], None] = print, err: Callable[[str], None] = _stderr) -> None:
    """run 을 돌리고 끝낸다 — 성공이면 client.log 를 원본으로 emit(지금의 _finish). 실패(어떤 예외든)면 client.log 가 비지 않을 때
    <failed_kind> 호출 디렉터리를 남기고(summary.txt 는 오류 문구) — ObsError 는 같은 종류로 문구 끝에 경로를 붙여 다시 올리고,
    그 외 예외는 err 로 경로 한 줄을 찍은 뒤 그대로 올린다. 보낸 호출이 없으면 디렉터리 없이 예외 그대로."""
    try:
        ev, kind, window = run()
    except ObsError as e:
        if not client.log:
            raise
        raise type(e)("%s — 원본: %s" % (e, _spill_failed(client, failed_kind, e, base, at))) from e
    except Exception as e:
        if client.log:
            err("원본: %s" % _spill_failed(client, failed_kind, e, base, at))
        raise
    _finish(ev, client.log, kind, window, max_lines, as_json, base=base, at=at, out=out)


def _run_main(client: Callable[[], Client], run: Callable[[Client], RunResult], failed_kind: str, output: OutputArgs,
              env: Mapping[str, str], at: float) -> None:
    """*_main 의 끝 — parse 뒤에 client() 로 Client 하나를 만들어 run 과 finish 가 같이 쓴다. spill 은 env 의 프로젝트 디렉터리에."""
    c = client()
    finish(c, lambda: run(c), failed_kind, output.max_lines, output.as_json, base=project_dir(env), at=at)


@dataclass(frozen=True)
class OutputArgs:
    """--max-lines · --json — 여섯 명령이 공유하는 stdout 절제 옵션. 기본값은 여기 한 곳."""
    max_lines: int = DEFAULT_MAX_LINES      # stdout 줄 상한
    as_json: bool = False                   # True = 요약 대신 호출 디렉터리 경로만


if TYPE_CHECKING:
    from _typeshed import DataclassInstance      # 타입 검사 때만 — "__dataclass_fields__ 가 있는 것" 의 Protocol

Args = TypeVar("Args", bound="DataclassInstance")
"""map_args 의 대상 — 어느 dataclass 든. 넣은 클래스가 그대로 반환 타입이다."""


def map_args(cls: type[Args], ns: argparse.Namespace) -> Args:
    """Namespace 에 있는 cls 의 필드만 모아 cls(**…) — 없는 옵션은 dataclass 의 기본값. 순수 함수: Client · 요청 · env 없음.
    Namespace 의 cls 밖 속성은 무시한다. 기본값 없는 필드가 없으면 TypeError — 파서가 필수로 막으므로 CLI 경로에서는 닿지 않는다."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in vars(ns).items() if k in names})


def main_guard(fn: Callable[[], None]) -> None:
    """진입점 — ObsError 는 메시지만 stderr 로, exit 1. 그 외 예외는 그대로."""
    try:
        fn()
    except ObsError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def _evidence(lines: list[str]) -> Evidence[Lines]:
    return Evidence(value=Lines(lines or ["(결과 없음)"]))


# ── promql ───────────────────────────────────────────────────────────────────────


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


def promql_run(client: Client, a: PromqlArgs, at: float) -> RunResult:
    """→ (Evidence, 원본 Response 목록, kind, 창). stdout 을 만지지 않는다."""
    end = parse_time(a.end, at)
    start = end - parse_duration(a.range_) if a.range_ else None
    window = Window(start=start if start is not None else end, end=end)
    match = a.query

    if a.labels:
        params: Params = {"match[]": match, "start": start, "end": end}
        resp = client.get("metrics", "api/v1/labels", params)
        body = check_status(resp.body, "metrics")
        lines = sorted(parse_strings(body))
        kind = "promql-labels"
    elif a.label_values:
        params = {"match[]": match, "start": start, "end": end}
        resp = client.get("metrics", "api/v1/label/%s/values" % a.label_values, params)
        body = check_status(resp.body, "metrics")
        lines = sorted(parse_strings(body))
        kind = "promql-label-values"
    elif a.series:
        params = {"match[]": a.series, "start": start if start is not None else end - 3600, "end": end}
        resp = client.get("metrics", "api/v1/series", params)
        body = check_status(resp.body, "metrics")
        lines = [labels_str(m) for m in parse_label_sets(body)]
        kind = "promql-series"
    elif not match:
        raise ObsError("PromQL 이 필요하다")
    elif start is not None:
        step = a.step or auto_step(end - start)
        params = {"query": match, "start": start, "end": end, "step": step}
        resp = client.get("metrics", "api/v1/query_range", params)
        body = check_status(resp.body, "metrics")
        lines = range_lines(parse_metric(body), a.points)
        kind = "promql-range"
    else:
        resp = client.get("metrics", "api/v1/query", {"query": match, "time": end})
        body = check_status(resp.body, "metrics")
        lines = instant_lines(parse_metric(body))
        kind = "promql"

    return _evidence(lines), kind, window


def promql_main(argv: Sequence[str], env: Mapping[str, str], at: float, client: Callable[[], Client]) -> None:
    if argv and argv[0] in METRICS_SUBCOMMANDS:
        metrics_main(argv, env, at, client)
        return
    a = promql_parser().parse_args(argv)
    args = map_args(PromqlArgs, a)
    _run_main(client, lambda c: promql_run(c, args, at), "promql-failed", map_args(OutputArgs, a), env, at)


# ── 환원 연산 — promql.py 의 서브커맨드 (계산은 obs.metrics, 표는 여기) ─────────────────


def fmt_ratio(r: float) -> str:
    return "%.3g" % r


def fmt_signed(v: float) -> str:
    return ("+" if v >= 0 else "") + fmt_val(v)


def window_str(w: Window) -> str:
    """요약 줄의 창 표기 — 스킬이 약속한 `MM-DD HH:MM:SSZ`(fmt_ts) 로. RFC3339 전체는 summary.txt 머리에."""
    return "%s ~ %s" % (fmt_ts(w.start), fmt_ts(w.end))


def summary_lines(value: Summary, window: Window) -> list[str]:
    if not value.series:
        return ["(데이터 없음 — %s 에 시계열이 없다. step=%s)" % (window_str(window), value.step)]
    lines = ["창 %s  step=%s  (n=서브쿼리 점 수 · p50/p95=quantile_over_time · slope=deriv 값/초)" % (window_str(window), value.step)]
    lines += ["n=%d  min=%s  p50=%s  p95=%s  max=%s  last=%s  slope=%s/s  %s" % (
        s.n, fmt_val(s.min), fmt_val(s.p50), fmt_val(s.p95), fmt_val(s.max), fmt_val(s.last), fmt_signed(s.slope),
        labels_str(s.labels)) for s in value.series]
    return lines


def compare_lines(value: Comparison, window: Window) -> list[str]:
    base = window.baseline
    base_label = window_str(base) if base is not None else "?"
    if value.now_empty and value.baseline_empty:
        return ["(데이터 없음 — 지금 %s · 기준선 %s 모두 시계열이 없다. step=%s)" % (window_str(window), base_label, value.step)]
    lines = ["지금 %s  기준선 %s (offset %s)  step=%s  (p95 비교)" % (window_str(window), base_label, value.offset, value.step)]
    if value.baseline_empty:
        lines.append("기준선 없음 — %s 에 시계열이 없다. 비율을 내지 않는다(0 으로 나누지 않는다)" % base_label)
    for d in value.deltas:
        labels = labels_str(d.labels)
        if d.now is not None and d.base is not None:
            if d.ratio is not None and d.delta is not None:
                lines.append("×%s  Δ%s  now p95=%s  base p95=%s  %s" % (
                    fmt_ratio(d.ratio), fmt_signed(d.delta), fmt_val(d.now), fmt_val(d.base), labels))
            else:
                lines.append("×—  Δ%s  now p95=%s  base p95=0  (기준선 0)  %s" % (
                    fmt_signed(d.delta if d.delta is not None else d.now), fmt_val(d.now), labels))
        elif d.now is not None:
            lines.append("기준선 없음  now p95=%s  %s" % (fmt_val(d.now), labels))
        elif d.base is not None:
            lines.append("지금 없음  base p95=%s  %s" % (fmt_val(d.base), labels))
    return lines


def red_lines(value: Red, window: Window) -> list[str]:
    """헤더 한 줄 + R · E · D 세 줄 — 상태(시계열 없음 · 호출 0 · 기준선 없음 · 기준선 0)의 문구."""
    base = window.baseline
    base_label = window_str(base) if base is not None else "?"
    lines = ["%s  지금 %s  기준선 %s (offset %s)  (R=창 안 호출 수 · E=오류율 · D=p%d 초)" % (
        value.service, window_str(window), base_label, value.offset, round(RED_QUANTILE * 100))]
    for s in value.signals:
        ratio = ["×%s" % fmt_ratio(s.ratio)] if s.ratio is not None else []
        lines.append("  ".join([s.name, *ratio, _red_now_cell(s), _red_base_cell(s)]))
    return lines


def _red_now_cell(s: Signal) -> str:
    """지금 창 칸 — "시계열 없음"(missing)과 "호출 0"(idle)을 가른다. D 의 idle 은 값이 없어 — 로."""
    if s.now_state == RED_MISSING:
        return "지금 시계열 없음"
    shown = fmt_val(s.now) if s.now is not None else "—"
    return "now=%s (호출 0)" % shown if s.now_state == RED_IDLE else "now=%s" % shown


def _red_base_cell(s: Signal) -> str:
    """기준선 칸 — "기준선 없음"(missing) · "기준선 0"(idle, 또는 값이 0) · 값."""
    if s.base_state == RED_MISSING:
        return "기준선 없음"
    if s.base_state == RED_IDLE:
        return "기준선 0 (호출 0)"
    return "기준선 0" if s.base == 0 else "base=%s" % (fmt_val(s.base) if s.base is not None else "—")


def top_lines(value: Top, window: Window) -> list[str]:
    if not value.entries:
        return ["(데이터 없음 — %s 시각에 %s 축의 시계열이 없다)" % (fmt_ts(window.end), ",".join(value.by))]
    return ["%s  %s" % (fmt_val(e.value), labels_str(e.labels)) for e in value.entries[: value.k]]


@dataclass
class MetricsArgs:
    cmd: str                    # summarize · compare · top · red
    query: str = ""             # summarize · compare · top 의 PromQL
    service: str = ""           # red 의 서비스 이름
    since: str = DEFAULT_SINCE
    end: str | None = None
    step: str | None = None
    baseline: str = DEFAULT_BASELINE
    k: int = DEFAULT_TOP_K
    by: str = "pod"
    agg: str = DEFAULT_TOP_AGG


def metrics_run(client: Client, a: MetricsArgs, at: float) -> RunResult:
    """→ (Evidence[Lines], 원본, kind, 창). 계산은 obs.metrics 가, 표는 *_lines 가. stdout 을 만지지 않는다.
    top 은 창이 없다 — --end 시각 하나(since 는 쓰이지 않는다)."""
    window = Window.last(a.since, a.end, at=at) if a.cmd != "top" else Window(parse_time(a.end, at), parse_time(a.end, at))
    if a.cmd == "summarize":
        ev = summarize(client, a.query, window, a.step)
        return _evidence(summary_lines(ev.value, window)), "promql-summarize", window
    if a.cmd == "compare":
        w = window.with_baseline(a.baseline)
        ev_c = compare(client, a.query, w, a.step)
        return _evidence(compare_lines(ev_c.value, w)), "promql-compare", w
    if a.cmd == "top":
        ev_t = top(client, a.query, window, k=a.k, by=a.by.split(","), agg=a.agg)
        return _evidence(top_lines(ev_t.value, window)), "promql-top", window
    if a.cmd == "red":
        w = window.with_baseline(a.baseline)
        ev_r = red(client, a.service, w)
        return _evidence(red_lines(ev_r.value, w)), "promql-red", w
    raise ObsError("알 수 없는 명령 %r — %s" % (a.cmd, " · ".join(METRICS_SUBCOMMANDS)))


def metrics_main(argv: Sequence[str], env: Mapping[str, str], at: float, client: Callable[[], Client]) -> None:
    a = metrics_parser().parse_args(argv)
    args = map_args(MetricsArgs, a)
    _run_main(client, lambda c: metrics_run(c, args, at), "promql-%s-failed" % a.cmd, map_args(OutputArgs, a), env, at)


# ── logql ────────────────────────────────────────────────────────────────────────


def common_labels(streams: Sequence[Stream]) -> dict[str, str]:
    if not streams:
        return {}
    common = dict(streams[0].labels)
    for s in streams[1:]:
        for k in list(common):
            if s.labels.get(k) != common[k]:
                del common[k]
    return common


def stream_lines(res: LogResult, direction: str, width: int) -> list[str]:
    common = common_labels(res.streams)
    entries: list[LogEntry] = []
    for s in res.streams:
        labels = {k: v for k, v in s.labels.items() if k not in common}
        for ts, line in s.values:
            entries.append(LogEntry(ts, labels, line))
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
    since: str = DEFAULT_SINCE
    end: str | None = None
    limit: int = DEFAULT_LOG_LIMIT
    direction: str = "backward"
    width: int = 200
    labels: bool = False
    label_values: str | None = None
    series: str | None = None


def logql_run(client: Client, a: LogqlArgs, at: float) -> RunResult:
    window = Window.last(a.since, a.end, at=at)
    ns: Params = {"start": int(window.start * 1e9), "end": int(window.end * 1e9)}

    if a.labels:
        resp = client.get("logs", "loki/api/v1/labels", ns)
        body = check_status(resp.body, "logs")
        lines = sorted(parse_strings(body))
        kind = "logql-labels"
    elif a.label_values:
        resp = client.get("logs", "loki/api/v1/label/%s/values" % a.label_values, ns)
        body = check_status(resp.body, "logs")
        lines = sorted(parse_strings(body))
        kind = "logql-label-values"
    elif a.series:
        resp = client.get("logs", "loki/api/v1/series", {**ns, "match[]": a.series})
        body = check_status(resp.body, "logs")
        lines = [labels_str(m) for m in parse_label_sets(body)]
        kind = "logql-series"
    elif not a.query:
        raise ObsError("LogQL 이 필요하다")
    else:
        params: Params = {**ns, "query": a.query, "limit": a.limit, "direction": a.direction}
        resp = client.get("logs", "loki/api/v1/query_range", params)
        body = check_status(resp.body, "logs")
        res = parse_logs(body)
        lines = matrix_lines(res) if res.result_type == "matrix" else stream_lines(res, a.direction, a.width)
        kind = "logql"

    return _evidence(lines), kind, window


def logql_main(argv: Sequence[str], env: Mapping[str, str], at: float, client: Callable[[], Client]) -> None:
    if argv and argv[0] in LOGS_SUBCOMMANDS:
        logs_main(argv, env, at, client)
        return
    a = logql_parser().parse_args(argv)
    args = map_args(LogqlArgs, a)
    _run_main(client, lambda c: logql_run(c, args, at), "logql-failed", map_args(OutputArgs, a), env, at)


# ── 환원 연산 — logql.py 의 서브커맨드 (계산은 obs.logs, 표는 여기) ────────────────────


def histogram_lines(value: Histogram, window: Window) -> list[str]:
    """헤더 한 줄 + 행(by 값)마다 `합  {레이블}  구간별 건수…` 한 줄 + 접힌 행이 있으면 꼬리 한 줄."""
    if not value.groups:
        return ["(데이터 없음 — %s 에 로그가 없다. step=%s)" % (window_str(window), value.step)]
    span = "%s ~ %s" % (fmt_ts(value.times[0]), fmt_ts(value.times[-1])) if value.times else "-"
    lines = ["창 %s  by %s  step=%s  구간 %d개(%s)  (합  {레이블}  구간별 건수 — 오래된 것부터)" % (
        window_str(window), ",".join(value.by), value.step, len(value.times), span)]
    lines += ["%d  %s  %s" % (g.total, labels_str(g.labels), " ".join(str(c) for c in g.counts)) for g in value.groups]
    if value.folded:
        lines.append("… %d행 더 — 합이 작아 표에서 뺐다" % value.folded)
    return lines


def templates_lines(value: Templates, window: Window) -> list[str]:
    """헤더 한 줄 + 군집마다 두 줄(`건수  비율  첫~마지막  템플릿` · 예시) + other 가 있으면 꼬리 한 줄."""
    if not value.clusters:
        return ["(데이터 없음 — %s 에 로그 군집이 없다. step=%s)" % (window_str(window), value.step)]
    lines = ["창 %s  총 %d건  군집 상위 %d  step=%s  (건수  비율  첫~마지막 발생  템플릿)" % (
        window_str(window), value.total, value.k, value.step)]
    for c in value.clusters:
        lines.append("%d  %.1f%%  %s ~ %s  %s" % (c.count, c.ratio * 100, fmt_ts(c.first_ts), fmt_ts(c.last_ts), clip(c.template, 200)))
        lines.append("    예: %s" % (clip(c.example, 200) if c.example is not None else "(없음)"))
    if value.other:
        lines.append("그 밖 %d건 — 상위 %d 밖의 군집" % (value.other, value.k))
    return lines


def around_lines(value: Context, window: Window) -> list[str]:
    """헤더(총 · 보임 · 앞/뒤와 잘림) 한 줄 + 보인 줄마다 `시각  {레이블 차이}  줄` 한 줄."""
    if not value.entries:
        return ["(데이터 없음 — %s 에 로그가 없다)" % window_str(window)]

    def side(name: str, s: Side) -> str:
        return "%s %d%s" % (name, s.shown, " 잘림" if s.truncated else "")

    lines = ["중심 %s  ±%s  총 %d  보임 %d (%s · %s, 쪽별 상한 %d)" % (
        fmt_ts(value.center), fmt_val(value.span) + "s", value.total, value.shown, side("앞", value.before), side("뒤", value.after), value.limit)]
    common = common_labels([Stream(e.labels, []) for e in value.entries])
    for e in value.entries:
        diff = {k: v for k, v in e.labels.items() if k not in common}
        tag = (" " + labels_str(diff)) if diff else ""
        lines.append("%s%s  %s" % (fmt_ts(e.ts_ns / 1e9), tag, clip(e.line, 200)))
    return lines


def first_seen_lines(value: FirstSeen, window: Window) -> list[str]:
    """`첫 발생 <시각>  <템플릿>` + 그 줄, 또는 `창 안에 없음 …` 한 줄."""
    if value.ts is None:
        return ["창 안에 없음 — %s 에 템플릿 %s 에 걸리는 줄이 없다" % (window_str(window), clip(value.template, 200))]
    return ["첫 발생 %s  %s" % (fmt_ts(value.ts), clip(value.template, 200)), "    %s" % clip(value.line or "", 200)]


@dataclass
class LogsArgs:
    cmd: str                    # histogram · templates · around · first-seen
    selector: str
    since: str = DEFAULT_SINCE
    end: str | None = None
    by: str = "detected_level"          # histogram
    step: str | None = None             # histogram · templates
    k: int = DEFAULT_TEMPLATES_K        # templates
    ts: str | None = None               # around — None = 거부
    span: str = DEFAULT_AROUND_SPAN     # around
    limit: int = DEFAULT_AROUND_LIMIT   # around
    template: str | None = None         # first-seen — None = 거부

    def validate(self) -> None:
        """요청 전 검사 — 모르는 cmd · around 의 ts 없음 · first-seen 의 template 없음은 ObsError."""
        if self.cmd not in LOGS_SUBCOMMANDS:
            raise ObsError("알 수 없는 명령 %r — %s" % (self.cmd, " · ".join(LOGS_SUBCOMMANDS)))
        if self.cmd == "around" and self.ts is None:
            raise ObsError("around 는 중심 시각(--ts)이 필요하다")
        if self.cmd == "first-seen" and self.template is None:
            raise ObsError("first-seen 은 템플릿이 필요하다")


def logs_run(client: Client, a: LogsArgs, at: float) -> RunResult:
    """→ (Evidence[Lines], 원본, kind, 창). 검사(validate)가 요청 앞이다. 계산은 obs.logs 가, 표는 *_lines 가. stdout 을 만지지 않는다."""
    a.validate()
    window = Window.last(a.since, a.end, at=at)
    if a.cmd == "histogram":
        ev_h = histogram(client, a.selector, window, by=a.by.split(","), step=a.step)
        return _evidence(histogram_lines(ev_h.value, window)), "logql-histogram", window
    if a.cmd == "templates":
        ev_t = templates(client, a.selector, window, k=a.k, step=a.step)
        return _evidence(templates_lines(ev_t.value, window)), "logql-templates", window
    if a.cmd == "around":
        ts = parse_time(a.ts, at)
        span = parse_duration(a.span)
        ev_a = around(client, a.selector, ts, span, limit=a.limit)
        w = Window(ts - span, ts + span)
        return _evidence(around_lines(ev_a.value, w)), "logql-around", w
    ev_f = first_seen(client, a.selector, a.template or "", window)
    return _evidence(first_seen_lines(ev_f.value, window)), "logql-first-seen", window


def logs_main(argv: Sequence[str], env: Mapping[str, str], at: float, client: Callable[[], Client]) -> None:
    a = logs_parser().parse_args(argv)
    args = map_args(LogsArgs, a)
    _run_main(client, lambda c: logs_run(c, args, at), "logql-%s-failed" % a.cmd, map_args(OutputArgs, a), env, at)


# ── traceql ──────────────────────────────────────────────────────────────────────


def search_lines(hits: Sequence[TraceHit]) -> list[str]:
    lines = ["%s  %6sms  %s  %s / %s" % (h.trace_id, h.duration_ms, fmt_ts(h.start_ns / 1e9) if h.start_ns else "?",
                                         h.root_service, h.root_name) for h in hits]
    return lines or ["(결과 없음 — 트레이스는 드물다. 창을 넓혀 다시: --since 24h · 7d)"]


def error_reason(s: Span) -> str:
    """오류 span 의 원인 한 줄 — status.message 의 첫 줄(스택은 개행·탭 뒤에 붙는다), 없으면 예외·HTTP 속성.
    원본은 어차피 파일로 떨어진다 — 여기서는 jq 왕복 없이 원인이 보이면 된다."""
    first = re.split(r"[\n\t]", s.status_message, maxsplit=1)[0].strip()
    return first or s.attrs.get("exception.message") or s.attrs.get("error.message") or s.attrs.get("http.status_code") or ""


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
        lines.append("  ✗ %s / %s  %.1fms  %s" % (s.service, s.name, s.duration_ms, clip(error_reason(s), 160)))
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
    since: str = DEFAULT_SINCE
    end: str | None = None
    limit: int = 20
    min_duration: str | None = None
    scope: str | None = None
    top: int = 15


def traceql_run(client: Client, a: TraceqlArgs, at: float) -> RunResult:
    window = Window.last(a.since, a.end, at=at)
    w: Params = {"start": int(window.start), "end": int(window.end)}

    if a.cmd == "search":
        params: Params = {**w, "limit": a.limit, "q": a.q, "minDuration": a.min_duration}
        resp = client.get("traces", "api/search", params)
        body = check_json(resp.body, "traces")
        lines = search_lines(parse_search(body))
        kind = "traceql-search"
    elif a.cmd == "trace":
        if not a.arg:
            raise ObsError("traceID 가 필요하다")
        resp = client.get("traces", "api/traces/%s" % a.arg)
        body = check_json(resp.body, "traces")
        lines = trace_lines(a.arg, parse_spans(body), a.top)
        kind = "traceql-trace"
    elif a.cmd == "tags":
        resp = client.get("traces", "api/v2/search/tags", {**w, "scope": a.scope})
        body = check_json(resp.body, "traces")
        lines = tags_lines(parse_tags(body))
        kind = "traceql-tags"
    elif a.cmd == "tag-values":
        if not a.arg:
            raise ObsError("태그 이름이 필요하다")
        resp = client.get("traces", "api/v2/search/tag/%s/values" % a.arg, {**w, "q": a.q})
        body = check_json(resp.body, "traces")
        lines = sorted(parse_tag_values(body))
        kind = "traceql-tag-values"
    else:
        raise ObsError("알 수 없는 명령 %r — search · trace · tags · tag-values" % a.cmd)

    return _evidence(lines), kind, window


def traceql_main(argv: Sequence[str], env: Mapping[str, str], at: float, client: Callable[[], Client]) -> None:
    if argv and argv[0] in TRACES_SUBCOMMANDS:
        traces_main(argv, env, at, client)
        return
    a = traceql_parser().parse_args(argv)
    args = map_args(TraceqlArgs, a)
    _run_main(client, lambda c: traceql_run(c, args, at), "traceql-failed", map_args(OutputArgs, a), env, at)


# ── 환원 연산 — traceql.py 의 서브커맨드 (계산은 obs.traces, 표는 여기) ─────────────────


def exemplars_lines(value: Exemplars, window: Window) -> list[str]:
    """헤더 + 요약 한 줄(표본 N · 임계 초과 M · 평소 기대) + 묶음마다 머리줄(건수 / 없음) + 건 줄."""
    if value.sample == 0:
        return ["(데이터 없음 — %s 에 걸리는 서버 span 이 없다. 창을 넓혀 다시: --since 24h · 7d)" % window_str(window)]
    lines = ["창 %s  묶음별 상위 %d  (오류 → 임계 초과(느림) → 정상 · 임계 출처: baseline=평소 p%s · window=조사 창 · sample=표본)" % (
        window_str(window), value.k, fmt_val(value.quantile * 100))]
    lines.append("표본 %d%s  임계 초과 %d  (평소 기대 %s%%)" % (
        value.sample, " (절단 — 검색 상한을 넘어 앞 트레이스만)" if value.truncated else "", value.slow.total, "%.3g" % ((1 - value.quantile) * 100)))
    for b in (value.slow, value.error, value.normal):
        lines.append("%s  %s" % (b.kind, "%d건" % b.total if b.total else "없음"))
        for e in b.items:
            lines.append("  %s  %.1fms  ×%.2f  임계 %sms(%s)  %s / %s  %s  %s" % (
                e.trace_id, e.duration_ms, e.ratio, fmt_val(e.threshold_ms), e.threshold_source, e.service, e.endpoint, e.pod, fmt_ts(e.start_ts)))
    return lines


def critical_path_lines(value: CriticalPath, window: Window) -> list[str]:
    """헤더 + 경로 span 마다 한 줄(잘림 표시) + 병목(self 내림차순) 줄. 루트가 없으면 헤더 한 줄."""
    if value.spans and not value.roots:
        return ["트레이스 %s  span %d개  임계경로 없음 — 루트 없음(parent 가 빈 span 이 없다), 고아 %d개" % (value.trace_id, value.spans, value.orphans)]
    incomplete = "  불완전한 트레이스 — 루트 %d개, 먼저 시작한 것을 따랐다" % value.roots if value.roots > 1 else ""
    orphans = "  고아 %d개 — 부모 span 유실, 경로에 없다" % value.orphans if value.orphans else ""
    lines = ["트레이스 %s  총 %.1fms  span %d개  임계경로 %d개  부모 밖 서브트리 %d개 버림%s%s  (+시작 오프셋  길이  self  서비스 / 이름)" % (
        value.trace_id, value.total_ms, value.spans, len(value.path), value.dropped, incomplete, orphans)]
    for p in value.path:
        lines.append("%s+%.1fms  %.1fms  self %.1fms  %s / %s%s" % (
            "  " * p.depth, p.offset_ms, p.duration_ms, p.self_ms, p.service, p.name, "  (잘림 — 부모 경계 밖 +%.1fms)" % p.clipped_ms if p.clipped_ms > 0 else ""))
    top = value.bottlenecks[:3]
    lines.append("병목 — self 내림차순 상위 %d:" % len(top))
    lines += ["  self %.1fms  깊이 %d  %s / %s" % (p.self_ms, p.depth, p.service, p.name) for p in top]
    return lines


def service_graph_lines(value: ServiceGraph, window: Window) -> list[str]:
    """헤더 + (caller → callee) 머리줄 아래 엔드포인트 줄. 데이터 없음 · 호출 0 · 미확인을 가른다."""
    if not value.edges and not value.idle:
        return ["(데이터 없음 — %s 에 클라이언트 span 행이 없다. 창을 넓혀 다시: --since 24h)" % window_str(window)]
    if not value.edges:
        return ["호출 0 — %s 에 알려진 클라이언트 호출 %d개가 창 안에서 0건이다" % (window_str(window), value.idle)]
    lines = ["창 %s  p%s  평소 %s  유휴 %d  (×배율=클라이언트 p95/평소 p95  건수  오류율  클라이언트 p95(평소)  서버 p95  엔드포인트)" % (
        window_str(window), fmt_val(value.quantile * 100), value.baseline_range, value.idle)]
    groups: dict[tuple[str, str | None], list[Edge]] = {}
    for e in value.edges:
        groups.setdefault((e.caller, e.callee), []).append(e)
    for (caller, callee), edges in groups.items():
        lines.append("%s → %s" % (caller, callee if callee is not None else "미확인(서버 행 없음 · 모호)"))
        for e in edges:
            lines.append("  ×%s  %d건  오류 %.1f%%  %s(평소 %s)  서버 %s  %s" % (
                fmt_ratio(e.ratio) if e.ratio is not None else "—", e.count, e.error_rate * 100, _ms_str(e.client_p95_ms),
                _ms_str(e.client_base_p95_ms), _ms_str(e.server_p95_ms), e.endpoint))
    return lines


def _ms_str(ms: float | None) -> str:
    return "%.1fms" % ms if ms is not None else "—"


def diff_lines(value: TraceDiff, window: Window) -> list[str]:
    """헤더 + 행마다 `Δ  slow  normal  서비스 / 이름`, 한쪽에만 있으면 그 표시."""
    lines = ["느린 %s  vs  정상 %s  루트 %s  (Δ = 느린 − 정상 합 · 합ms×건수 · 서비스 / 이름)" % (value.slow_id, value.normal_id, value.root_name)]
    for d in value.deltas:
        if d.delta_ms is not None:
            head = "Δ%+.1fms" % d.delta_ms
        else:
            head = "한쪽에만 있음(%s)" % ("느린 쪽" if d.slow is not None else "정상 쪽")
        lines.append("%s  느린 %s  정상 %s  %s / %s" % (head, _span_time(d.slow), _span_time(d.normal), d.service, d.name))
    return lines


def _span_time(t: SpanTime | None) -> str:
    return "%.1fms×%d" % (t.total_ms, t.n) if t is not None else "—"


@dataclass
class TracesArgs:
    cmd: str                                 # exemplars · critical-path · service-graph · diff
    q: str = ""                              # exemplars 의 조건식(중괄호 없이)
    since: str = DEFAULT_SINCE
    end: str | None = None
    k: int = DEFAULT_EXEMPLARS_K             # exemplars
    quantile: float = DEFAULT_QUANTILE       # exemplars · service-graph
    baseline_range: str = DEFAULT_BASELINE_RANGE   # exemplars · service-graph — 평소 분포 구간
    limit: int = DEFAULT_SEARCH_LIMIT        # exemplars 검색 limit
    trace_id: str | None = None              # critical-path — None = 거부
    slow_id: str | None = None               # diff — None = 거부
    normal_id: str | None = None             # diff — None = 거부

    def validate(self) -> None:
        """요청 전 검사 — 모르는 cmd · 필수 id 없음은 ObsError."""
        if self.cmd not in TRACES_SUBCOMMANDS:
            raise ObsError("알 수 없는 명령 %r — %s" % (self.cmd, " · ".join(TRACES_SUBCOMMANDS)))
        if self.cmd == "critical-path" and self.trace_id is None:
            raise ObsError("critical-path 는 traceID 가 필요하다")
        if self.cmd == "diff" and (self.slow_id is None or self.normal_id is None):
            raise ObsError("diff 는 traceID 두 개(느린 것 · 정상인 것)가 필요하다")


def traces_run(client: Client, a: TracesArgs, at: float) -> RunResult:
    """→ (Evidence[Lines], 원본, kind, 창). validate 가 요청 앞. 계산은 obs.traces 가, 표는 *_lines 가. stdout 을 만지지 않는다."""
    a.validate()
    window = Window.last(a.since, a.end, at=at)
    kind = "traceql-" + a.cmd
    if a.cmd == "exemplars":
        ev_e = exemplars(client, a.q, window, k=a.k, quantile=a.quantile, baseline_range=a.baseline_range, limit=a.limit)
        return _evidence(exemplars_lines(ev_e.value, window)), kind, window
    if a.cmd == "critical-path":
        ev_c = critical_path(client, a.trace_id or "", window)
        return _evidence(critical_path_lines(ev_c.value, window)), kind, window
    if a.cmd == "service-graph":
        ev_g = service_graph(client, window, quantile=a.quantile, baseline_range=a.baseline_range)
        return _evidence(service_graph_lines(ev_g.value, window)), kind, window
    ev_d = diff(client, a.slow_id or "", a.normal_id or "", window)
    return _evidence(diff_lines(ev_d.value, window)), kind, window


def traces_main(argv: Sequence[str], env: Mapping[str, str], at: float, client: Callable[[], Client]) -> None:
    """argparse 서브파서 → TracesArgs → traces_run → _finish."""
    a = traces_parser().parse_args(argv)
    args = map_args(TracesArgs, a)
    _run_main(client, lambda c: traces_run(c, args, at), "traceql-%s-failed" % a.cmd, map_args(OutputArgs, a), env, at)


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


def doctor_main(argv: Sequence[str], env: Mapping[str, str], transport: Transport | None) -> None:
    ap = argparse.ArgumentParser(prog="obs-doctor.py", description=DOCTOR_DOC,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rep = doctor(load_config(env), token_from_env(env), env, transport)
    if a.json:
        print(to_json(rep))
    else:
        for line in doctor_lines(rep):
            print(line)
    sys.exit(0 if rep.ok else 1)
