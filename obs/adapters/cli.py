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
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence, TypeAlias

from obs.core import (
    JSON, Client, DoctorReport, Evidence, Lines, ObsError, Params, Response, Window, doctor, fmt_ts, load_config,
    SPILL_RAW, SPILL_SUMMARY, json_labels, json_list, json_strs,
    parse_duration, parse_time, now, spill, spill_ref, to_json, token_from_env,
)
from obs.logs import (
    DEFAULT_AROUND_LIMIT, DEFAULT_AROUND_SPAN, DEFAULT_TEMPLATES_K, Context, FirstSeen, Histogram, LogEntry, LogResult, Stream,
    Side, Templates, around, first_seen, histogram, parse_logs, templates,
)
from obs.metrics import (
    DEFAULT_BASELINE, DEFAULT_TOP_AGG, DEFAULT_TOP_K, RED_IDLE, RED_MISSING, RED_QUANTILE, TOP_AGGS, Comparison, MetricResult, Red,
    Signal, Summary, Top, auto_step, check_status, compare, parse_metric, red, summarize, top,
)
from obs.traces import (
    DEFAULT_BASELINE_RANGE, DEFAULT_EXEMPLARS_K, DEFAULT_QUANTILE, DEFAULT_SEARCH_LIMIT, CriticalPath, Edge, Exemplars, ServiceGraph, Span, SpanTime, TagScope,
    TraceDiff, TraceHit, check_json, critical_path, diff, exemplars, parse_search, parse_spans, parse_tag_values, parse_tags, service_graph,
)

DEFAULT_MAX_LINES: int = 40
DEFAULT_LOG_LIMIT: int = 30

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


def emit(lines: list[str], full: object, kind: str, max_lines: int, quiet: bool = False,
         env: Mapping[str, str] | None = None, out: Callable[[str], None] = print,
         preamble: Sequence[str] = ()) -> Emitted:
    """항상 호출 디렉터리를 만든다(원본 raw.json · 요약 전체 summary.txt). stdout 에는 앞 max_lines 줄만 —
    quiet 면 요약을 찍지 않고 경로만. 잘렸으면 몇 줄이 더 있는지 알린다.
    `preamble` 은 summary.txt 의 머리에만 쓴다(창 · 기준선 — stdout 에는 찍지 않는다)."""
    ref = spill_ref(spill(kind, full, [*preamble, *lines] if preamble else lines, env), env)
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


def _finish(ev: Evidence[Lines], raw: list[Response], kind: str, window: Window, max_lines: int, as_json: bool,
            env: Mapping[str, str] | None = None, out: Callable[[str], None] = print) -> None:
    """Evidence 를 stdout 규약으로. as_json 은 stdout 에 요약을 찍지 않고 경로만(파일은 어느 쪽이든 같다).
    summary.txt 는 머리(창) + 요약 전체다."""
    emit(ev.value.lines, raw, kind, max_lines, quiet=as_json, env=env, out=out, preamble=window_preamble(window))


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


def _spill_failed(client: Client, failed_kind: str, error: BaseException, env: Mapping[str, str] | None) -> str:
    """실패한 run 의 client.log 를 <failed_kind> 호출 디렉터리로 — summary.txt 는 오류 문구. 안내할 경로 문구를 돌려준다."""
    ref = spill_ref(spill(failed_kind, client.log, [str(error)], env), env)
    return "%s/ (%s · %s)" % (ref, SPILL_RAW, SPILL_SUMMARY)


def finish(client: Client, run: Callable[[], RunResult], failed_kind: str, max_lines: int, as_json: bool,
           env: Mapping[str, str] | None = None, out: Callable[[str], None] = print, err: Callable[[str], None] = _stderr) -> None:
    """run 을 돌리고 끝낸다 — 성공이면 client.log 를 원본으로 emit(지금의 _finish). 실패(어떤 예외든)면 client.log 가 비지 않을 때
    <failed_kind> 호출 디렉터리를 남기고(summary.txt 는 오류 문구) — ObsError 는 같은 종류로 문구 끝에 경로를 붙여 다시 올리고,
    그 외 예외는 err 로 경로 한 줄을 찍은 뒤 그대로 올린다. 보낸 호출이 없으면 디렉터리 없이 예외 그대로."""
    try:
        ev, kind, window = run()
    except ObsError as e:
        if not client.log:
            raise
        raise type(e)("%s — 원본: %s" % (e, _spill_failed(client, failed_kind, e, env))) from e
    except Exception as e:
        if client.log:
            err("원본: %s" % _spill_failed(client, failed_kind, e, env))
        raise
    _finish(ev, client.log, kind, window, max_lines, as_json, env=env, out=out)


def _common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES,
                    help="stdout 줄 수 상한. 기본 %d — 나머지는 호출 디렉터리의 summary.txt 에" % DEFAULT_MAX_LINES)
    ap.add_argument("--json", action="store_true", help="stdout 에 요약을 찍지 않고 호출 디렉터리 경로만(파일은 성공 · 실패 모두 남는다)")


def _client() -> Client:
    return Client.from_env()


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

PROMQL_DOC = """promql — 즉시 조회·구간 조회 (metrics 백엔드, GET 만).

사용:
  promql.py '<PromQL>'                                  즉시 조회
  promql.py '<PromQL>' --range 1h [--step 60s]           구간 조회 — 시계열별 요약
  promql.py '<PromQL>' --range 1h --points               구간 조회 — 시각별 값 전부 (많다 — 파일로 떨어진다)
  promql.py --labels [<selector>]                        레이블 이름 목록
  promql.py --label-values <name> [<selector>]           레이블 값 목록
  promql.py --series '<selector>' [--range 1h]           시계열 목록

환원 연산 — 계산은 백엔드(PromQL)가 하고 결과만 온다(시계열이 컨텍스트에 실리지 않는다):
  promql.py summarize '<PromQL>' --since 1h              시계열별 n · min · p50 · p95 · max · last · 기울기 (*_over_time · deriv)
  promql.py compare '<PromQL>' --since 1h [--baseline 1d]  지금 창 vs 기준선 창(같은 식의 offset) — 시계열별 p95 차이(비율 · 절대)만
                                                         --baseline: 1d(기본 — 1일 전 같은 시각) · 1w · prev(직전 창)
  promql.py top '<PromQL>' --by pod [--k 10] [--agg sum]  레이블 축 상위 k — topk 로 감싸 서버가 k 개만 돌려준다.
                                                         --end 시각의 즉시 조회(창이 없다 — --since 를 받지 않는다)
  promql.py red <서비스> --since 1h [--baseline 1d]      서비스 하나의 R(호출 수) · E(오류율) · D(p95) — 지금 창 vs 기준선 창(spanmetrics 서버 span)
  summarize · compare · red 의 시간은 --since/--end (logql 과 같다). 연산당 질의 1회. 창(compare 는 기준선도)은 호출 디렉터리의
  summary.txt 머리에 남는다. 각 서브커맨드의 설명·옵션 전부는 `promql.py <서브커맨드> --help`.

옵션:
  --end <시각>      기준 시각 (now · -10m · unix초 · RFC3339). 기본 now
  --range <기간>    구간 길이 (5m · 1h · 2d). 있으면 구간 조회
  --step <기간>     구간 해상도. 기본 range/60 에 가까운 값
  --max-lines <n>   stdout 줄 수 상한. 기본 40 — 나머지는 호출 디렉터리의 summary.txt 에
  --json            stdout 에 요약을 찍지 않고 경로만(파일은 항상 남는다)

실패해도(HTTP 오류 · 전송 실패 · 예산 초과) 호출 디렉터리가 남는다 — tmp/obs/<시각>-<명령>-failed/ 에 보낸 호출 전부(실패 포함)와 오류 문구, 오류 메시지 끝이 그 경로다.

출력은 요약이다 — 즉시 조회는 `값  {레이블}` 한 줄씩, 구간 조회는 시계열마다
`n=점수 min max first last {레이블}` 한 줄. 원본은 호출 디렉터리의 raw.json.
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
    """→ (Evidence, 원본 Response 목록, kind, 창). stdout 을 만지지 않는다."""
    end = parse_time(a.end, now() if at is None else at)
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


def promql_main(argv: Sequence[str] | None = None) -> None:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in METRICS_SUBCOMMANDS:
        metrics_main(args_list)
        return
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
    client = _client()
    finish(client, lambda: promql_run(client, args), "promql-failed", a.max_lines, a.json)


# ── 환원 연산 — promql.py 의 서브커맨드 (계산은 obs.metrics, 표는 여기) ─────────────────

METRICS_SUBCOMMANDS: tuple[str, ...] = ("summarize", "compare", "top", "red")

METRICS_COMMON_DOC = """계산은 백엔드가 한다 — 코어는 질의를 조립해 결과만 받는다(연산당 질의 1회). 창(compare 는 기준선도)은 호출 디렉터리의
summary.txt 머리에 남는다. stdout 은 요약이고 --max-lines 에서 잘린다. --json 은 경로만.
실패해도(HTTP 오류 · 전송 실패 · 예산 초과) 호출 디렉터리가 남는다 — tmp/obs/<시각>-<명령>-failed/ 에 보낸 호출 전부(실패 포함)와 오류 문구, 오류 메시지 끝이 그 경로다."""

SUMMARIZE_DOC = """promql.py summarize '<PromQL>' --since 1h [--end <시각>] [--step 60s]

시계열마다 `n min p50 p95 max last slope` 한 줄 — 서브쿼리 `(Q)[<창>:<step>]` 위의 count/min/quantile(0.5·0.95)/
max/last_over_time · deriv(slope, 값/초)를 한 질의로. 첫 줄은 창·step 헤더. 시계열이 없으면 "데이터 없음"(0 과 다르다).
step 은 창 길이/60(최소 15s) — 긴 창(--since 7d)이면 해상도가 거칠어지므로 헤더의 step 을 같이 읽는다.

""" + METRICS_COMMON_DOC

COMPARE_DOC = """promql.py compare '<PromQL>' --since 1h [--end <시각>] [--baseline 1d] [--step 60s]

같은 식의 p95 를 지금 창과 `offset <기준선>` 에서 한 질의로 받아 시계열별 차이만:
`×비율  Δ절대  now p95  base p95  {레이블}` — 비율 내림차순, 그 뒤에 기준선 0 · 기준선 없음 · 지금 없음.
--baseline: 1d(기본 — 1일 전 같은 시각, 주기성 있는 지표용) · 1w(1주 전) · prev(직전 창 — 급변 감지용).
기준선 창에 시계열이 없으면(보존 밖 · 그때 없던 pod) "기준선 없음" — 0 으로 나누지 않는다.

""" + METRICS_COMMON_DOC

TOP_DOC = """promql.py top '<PromQL>' --by pod [--k 10] [--agg sum] [--end <시각>]

레이블 축 상위 k — `topk(k, agg by (by) (query))` 로 감싸 --end 시각에 즉시 조회한다. 창이 없다(--since 를 받지 않는다).
서버가 k 개만 돌려주므로 카디널리티 폭발이 없고, 출력은 k 줄 이하(헤더 없음).
--by 는 쉼표로 여러 축(namespace,pod). --agg: sum(기본) · max · min · avg · count — 축으로 접는 집계.

""" + METRICS_COMMON_DOC


RED_DOC = """promql.py red <서비스> --since 1h [--end <시각>] [--baseline 1d]

서비스 하나의 R · E · D — spanmetrics 의 서버 span 행 위에서 지금 창과 같은 식의 `offset <기준선>` 을 한 질의로.
첫 줄은 서비스 · 창 · 기준선 헤더, 그 뒤 세 줄: R(창 안 호출 수) · E(오류율 = 오류 호출 / 호출) · D(p95 초, native histogram).
각 줄은 `×비율  now=…  base=…` — 비율은 두 창 모두 값이 있고 기준선이 0 이 아닐 때만(0 으로 나누지 않는다).
"시계열 없음"(그 창에 호출 카운터가 없다)과 "호출 0"(시계열은 있고 창 안 호출 0)은 다르다. 기준선 쪽은 "기준선 없음" · "기준선 0".
--baseline: 1d(기본 — 1일 전 같은 시각) · 1w(1주 전) · prev(직전 창).

""" + METRICS_COMMON_DOC


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
    since: str = "1h"
    end: str | None = None
    step: str | None = None
    baseline: str = DEFAULT_BASELINE
    k: int = DEFAULT_TOP_K
    by: str = "pod"
    agg: str = DEFAULT_TOP_AGG


def metrics_run(client: Client, a: MetricsArgs, at: float | None = None) -> RunResult:
    """→ (Evidence[Lines], 원본, kind, 창). 계산은 obs.metrics 가, 표는 *_lines 가. stdout 을 만지지 않는다.
    top 은 창이 없다 — --end 시각 하나(since 는 쓰이지 않는다)."""
    base_ts = now() if at is None else at
    window = Window.last(a.since, a.end, at) if a.cmd != "top" else Window(parse_time(a.end, base_ts), parse_time(a.end, base_ts))
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


def metrics_main(argv: Sequence[str]) -> None:
    ap = argparse.ArgumentParser(prog="promql.py", description="메트릭 환원 연산 — summarize · compare · top · red. 각 서브커맨드의 --help 에 설명 전부.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<%s>" % "|".join(METRICS_SUBCOMMANDS))
    fmt = argparse.RawDescriptionHelpFormatter

    def common(sp: argparse.ArgumentParser, end_help: str) -> None:
        sp.add_argument("query", help="PromQL")
        sp.add_argument("--end", default=None, help=end_help)
        _common(sp)

    sp_sum = sub.add_parser("summarize", help="시계열별 n·min·p50·p95·max·last·기울기", description=SUMMARIZE_DOC, formatter_class=fmt)
    sp_sum.add_argument("--since", default="1h", help="창 길이. 기본 1h")
    common(sp_sum, "창 끝 (now · -10m · unix초 · RFC3339). 기본 now")
    sp_sum.add_argument("--step", default=None, help="서브쿼리 해상도. 기본 창 길이/60 (최소 15s)")
    sp_cmp = sub.add_parser("compare", help="지금 창 vs 기준선 창 — 시계열별 p95 차이만", description=COMPARE_DOC, formatter_class=fmt)
    sp_cmp.add_argument("--since", default="1h", help="창 길이. 기본 1h")
    common(sp_cmp, "창 끝 (now · -10m · unix초 · RFC3339). 기본 now")
    sp_cmp.add_argument("--baseline", default=DEFAULT_BASELINE,
                        help="기준선 창 — 같은 식의 offset: 1d(기본 — 1일 전 같은 시각) · 1w · prev(직전 창)")
    sp_cmp.add_argument("--step", default=None, help="서브쿼리 해상도(두 창에 같다). 기본 창 길이/60 (최소 15s)")
    sp_top = sub.add_parser("top", help="레이블 축 상위 k (--end 시각의 즉시 조회)", description=TOP_DOC, formatter_class=fmt)
    common(sp_top, "평가 시각 (now · -10m · unix초 · RFC3339). 기본 now — top 은 창이 없다")
    sp_top.add_argument("--by", default="pod", help="레이블 축. 쉼표로 여러 개. 기본 pod")
    sp_top.add_argument("--k", type=int, default=DEFAULT_TOP_K, help="상위 k. 기본 %d" % DEFAULT_TOP_K)
    sp_top.add_argument("--agg", choices=TOP_AGGS, default=DEFAULT_TOP_AGG, help="축으로 접는 집계. 기본 sum")
    sp_red = sub.add_parser("red", help="서비스 하나의 R · E · D — 지금 창 vs 기준선 창", description=RED_DOC, formatter_class=fmt)
    sp_red.add_argument("service", help="서비스 이름(spanmetrics 의 service_name)")
    sp_red.add_argument("--since", default="1h", help="창 길이. 기본 1h")
    sp_red.add_argument("--end", default=None, help="창 끝 (now · -10m · unix초 · RFC3339). 기본 now")
    _common(sp_red)
    sp_red.add_argument("--baseline", default=DEFAULT_BASELINE,
                        help="기준선 창 — 같은 식의 offset: 1d(기본 — 1일 전 같은 시각) · 1w · prev(직전 창)")
    a = ap.parse_args(argv)
    args = MetricsArgs(cmd=a.cmd, query=getattr(a, "query", ""), service=getattr(a, "service", ""), since=getattr(a, "since", "1h"), end=a.end, step=getattr(a, "step", None),
                       baseline=getattr(a, "baseline", DEFAULT_BASELINE), k=getattr(a, "k", DEFAULT_TOP_K),
                       by=getattr(a, "by", "pod"), agg=getattr(a, "agg", DEFAULT_TOP_AGG))
    client = _client()
    finish(client, lambda: metrics_run(client, args), "promql-%s-failed" % a.cmd, a.max_lines, a.json)


# ── logql ────────────────────────────────────────────────────────────────────────

LOGQL_DOC = """logql — 로그 조회 (logs 백엔드, GET 만).

사용:
  logql.py '<LogQL>' [--since 1h] [--limit 30]           로그 줄
  logql.py '<metric LogQL>' --since 1h                    메트릭 질의(count_over_time 등) — 시계열 요약
  logql.py --labels                                       레이블 이름
  logql.py --label-values <name> [--since 1h]             레이블 값
  logql.py --series '<selector>' [--since 1h]             스트림 목록

환원 연산 — 서브커맨드. 각각의 --help 에 설명 전부:
  logql.py histogram '<selector>' [--by detected_level]   by 값별 · 구간별 건수 표
  logql.py templates '<selector>' [--k 10]                로그 군집 상위 k + 예시
  logql.py around '<selector>' --ts <시각>                시각 앞뒤의 문맥
  logql.py first-seen '<selector>' '<패턴>'               템플릿의 첫 발생

옵션:
  --since <기간>     조회 구간 길이 (5m · 1h · 2d). 기본 1h
  --end <시각>       구간 끝 (now · -10m · unix초 · RFC3339). 기본 now
  --limit <n>        돌려받을 최대 줄 수. 기본 30 — 낮게 둔다. 더 필요하면 명시적으로 올린다
  --direction        backward(최신부터, 기본) · forward
  --width <n>        한 줄 표시 폭. 기본 200 — 넘치면 자른다
  --max-lines <n>    stdout 줄 수 상한. 기본 40 — 나머지는 호출 디렉터리의 summary.txt 에
  --json             stdout 에 요약을 찍지 않고 경로만(파일은 항상 남는다)

실패해도(HTTP 오류 · 전송 실패 · 예산 초과) 호출 디렉터리가 남는다 — tmp/obs/<시각>-<명령>-failed/ 에 보낸 호출 전부(실패 포함)와 오류 문구, 오류 메시지 끝이 그 경로다.
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


def logql_main(argv: Sequence[str] | None = None) -> None:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in LOGS_SUBCOMMANDS:
        logs_main(args_list)
        return
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
    client = _client()
    finish(client, lambda: logql_run(client, args), "logql-failed", a.max_lines, a.json)


# ── 환원 연산 — logql.py 의 서브커맨드 (계산은 obs.logs, 표는 여기) ────────────────────

LOGS_SUBCOMMANDS: tuple[str, ...] = ("histogram", "templates", "around", "first-seen")


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
    since: str = "1h"
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


def logs_run(client: Client, a: LogsArgs, at: float | None = None) -> RunResult:
    """→ (Evidence[Lines], 원본, kind, 창). 검사(validate)가 요청 앞이다. 계산은 obs.logs 가, 표는 *_lines 가. stdout 을 만지지 않는다."""
    a.validate()
    window = Window.last(a.since, a.end, at)
    if a.cmd == "histogram":
        ev_h = histogram(client, a.selector, window, by=a.by.split(","), step=a.step)
        return _evidence(histogram_lines(ev_h.value, window)), "logql-histogram", window
    if a.cmd == "templates":
        ev_t = templates(client, a.selector, window, k=a.k, step=a.step)
        return _evidence(templates_lines(ev_t.value, window)), "logql-templates", window
    if a.cmd == "around":
        ts = parse_time(a.ts, now() if at is None else at)
        span = parse_duration(a.span)
        ev_a = around(client, a.selector, ts, span, limit=a.limit)
        w = Window(ts - span, ts + span)
        return _evidence(around_lines(ev_a.value, w)), "logql-around", w
    ev_f = first_seen(client, a.selector, a.template or "", window)
    return _evidence(first_seen_lines(ev_f.value, window)), "logql-first-seen", window


LOGS_COMMON_DOC = """계산은 백엔드(Loki)가 한다 — count_over_time · Patterns API 의 환원 결과만 받고, 원본 로그 줄은 예시 · 첫 발생 · 문맥에만 온다.
창(around 는 --ts ± --span)은 호출 디렉터리의 summary.txt 머리에 남는다. stdout 은 요약이고 --max-lines 에서 잘린다. --json 은 경로만.
실패해도(HTTP 오류 · 전송 실패 · 예산 초과) 호출 디렉터리가 남는다 — tmp/obs/<시각>-<명령>-failed/ 에 보낸 호출 전부(실패 포함)와 오류 문구, 오류 메시지 끝이 그 경로다."""

HISTOGRAM_DOC = """logql.py histogram '<selector>' [--by detected_level] [--since 1h] [--end <시각>] [--step 60s]

by 값별 · 구간별 건수 표 — `sum by (<by>) (count_over_time(<selector>[<step>]))` 를 구간 조회 한 번으로.
첫 줄은 창 · step · 구간 수 헤더, 그 뒤 행마다 `합  {레이블}  구간별 건수(오래된 것부터)`. 행은 합 내림차순이고 상한을 넘으면 꼬리에 뺀 행 수.
로그가 없으면 "데이터 없음"(0 과 다르다 — 건수 0 인 행은 0 으로 실린다).

""" + LOGS_COMMON_DOC

TEMPLATES_DOC = """logql.py templates '<selector>' [--k 10] [--since 1h] [--end <시각>] [--step 60s]

로그 군집(패턴) 상위 k — Patterns API 한 번 + 군집마다 예시 한 줄. 군집마다 두 줄:
`건수  비율  첫~마지막 발생(정밀도 step)  템플릿` · `예: <그 패턴에 걸린 첫 줄>`. 상위 k 밖의 건수는 꼬리 한 줄.
템플릿을 그대로 first-seen 에 넣으면 정확한 첫 발생 시각이 나온다.

""" + LOGS_COMMON_DOC

AROUND_DOC = """logql.py around '<selector>' --ts <시각> [--span 5m] [--limit 50]

시각 ts 앞뒤 span 의 문맥 — 창(ts±span) 전체 건수 한 번 + 앞(ts 이전) · 뒤(ts 부터) 각각 ts 에 가까운 줄 limit 개.
첫 줄은 `총 N  보임 M (앞 a · 뒤 b, 잘림 표시)` 헤더, 그 뒤 보인 줄을 시각 오름차순으로. 줄이 없으면 "데이터 없음".

""" + LOGS_COMMON_DOC

FIRST_SEEN_DOC = """logql.py first-seen '<selector>' '<패턴>' [--since 1h] [--end <시각>]

템플릿(패턴 — templates 의 출력 그대로, `<_>` 가 자리표시)에 걸리는 창 안의 첫 줄 — `|>` 패턴 필터를 forward · limit 1 로 한 번.
`첫 발생 <시각>  <템플릿>` 과 그 줄, 창 안에 없으면 `창 안에 없음`.

""" + LOGS_COMMON_DOC


def logs_main(argv: Sequence[str]) -> None:
    ap = argparse.ArgumentParser(prog="logql.py", description="로그 환원 연산 — %s. 각 서브커맨드의 --help 에 설명 전부." % " · ".join(LOGS_SUBCOMMANDS),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<%s>" % "|".join(LOGS_SUBCOMMANDS))
    fmt = argparse.RawDescriptionHelpFormatter

    def common(sp: argparse.ArgumentParser, window: bool = True) -> None:
        sp.add_argument("selector", help="LogQL 스트림 selector — 예: '{namespace=\"x\"}'")
        if window:
            sp.add_argument("--since", default="1h", help="창 길이. 기본 1h")
            sp.add_argument("--end", default=None, help="창 끝 (now · -10m · unix초 · RFC3339). 기본 now")
        _common(sp)

    sp_h = sub.add_parser("histogram", help="by 값별 · 구간별 건수 표", description=HISTOGRAM_DOC, formatter_class=fmt)
    common(sp_h)
    sp_h.add_argument("--by", default="detected_level", help="행의 레이블 축. 쉼표로 여러 개. 기본 detected_level")
    sp_h.add_argument("--step", default=None, help="구간 폭. 기본 창 길이/60 (최소 15s)")
    sp_t = sub.add_parser("templates", help="로그 군집 상위 k + 예시", description=TEMPLATES_DOC, formatter_class=fmt)
    common(sp_t)
    sp_t.add_argument("--k", type=int, default=DEFAULT_TEMPLATES_K, help="상위 k. 기본 %d" % DEFAULT_TEMPLATES_K)
    sp_t.add_argument("--step", default=None, help="sample 구간 폭. 기본 창 길이/60 (최소 15s)")
    sp_a = sub.add_parser("around", help="시각 앞뒤의 문맥", description=AROUND_DOC, formatter_class=fmt)
    common(sp_a, window=False)
    sp_a.add_argument("--ts", default=None, help="중심 시각 (--ts=-30m · unix초 · RFC3339). 필수")
    sp_a.add_argument("--span", default=DEFAULT_AROUND_SPAN, help="한 쪽 폭. 기본 %s" % DEFAULT_AROUND_SPAN)
    sp_a.add_argument("--limit", type=int, default=DEFAULT_AROUND_LIMIT, help="한 쪽 줄 수 상한. 기본 %d" % DEFAULT_AROUND_LIMIT)
    sp_f = sub.add_parser("first-seen", help="템플릿의 첫 발생", description=FIRST_SEEN_DOC, formatter_class=fmt)
    common(sp_f)
    sp_f.add_argument("template", help="패턴 — templates 출력의 템플릿 그대로")
    a = ap.parse_args(argv)
    args = LogsArgs(cmd=a.cmd, selector=a.selector, since=getattr(a, "since", "1h"), end=getattr(a, "end", None),
                    by=getattr(a, "by", "detected_level"), step=getattr(a, "step", None), k=getattr(a, "k", DEFAULT_TEMPLATES_K),
                    ts=getattr(a, "ts", None), span=getattr(a, "span", DEFAULT_AROUND_SPAN), limit=getattr(a, "limit", DEFAULT_AROUND_LIMIT),
                    template=getattr(a, "template", None))
    client = _client()
    finish(client, lambda: logs_run(client, args), "logql-%s-failed" % a.cmd, a.max_lines, a.json)


# ── traceql ──────────────────────────────────────────────────────────────────────

TRACEQL_DOC = """traceql — 트레이스 검색·조회 (traces 백엔드, GET 만).

사용:
  traceql.py search [--q '<TraceQL>'] [--since 1h] [--limit 20]     최근 트레이스 목록
  traceql.py trace <traceID>                                        트레이스 하나 — 요약
  traceql.py tags [--scope span|resource|intrinsic]                 검색 가능한 태그
  traceql.py tag-values <tag> [--q '<TraceQL>']                     태그 값

환원 연산 — 서브커맨드. 각각의 --help 에 설명 전부:
  traceql.py exemplars [--q '<조건식>'] [--k 3]                     느림 · 오류 · 정상 묶음별 대표 트레이스
  traceql.py critical-path <traceID>                                임계경로와 병목
  traceql.py service-graph [--since 1h]                             엔드포인트별 호출 엣지 · p95 배율
  traceql.py diff <느린 traceID> <정상 traceID>                     두 트레이스의 span 별 시간 차이

옵션:
  --q <TraceQL>      검색식. 예: '{ resource.service.name = "posting-service" && status = error }'
                     비우면 구간 안의 모든 트레이스(최근순)
  --since <기간>     조회 구간 길이. 기본 1h        --end <시각>  구간 끝. 기본 now
  --limit <n>        search 결과 상한. 기본 20
  --min-duration     예: 500ms · 2s (search)
  --top <n>          trace 요약에서 보이는 느린 span 수. 기본 15
  --max-lines <n>    stdout 줄 수 상한. 기본 40 — 나머지는 호출 디렉터리의 summary.txt 에
  --json             stdout 에 요약을 찍지 않고 경로만(파일은 항상 남는다)

실패해도(HTTP 오류 · 전송 실패 · 예산 초과) 호출 디렉터리가 남는다 — tmp/obs/<시각>-<명령>-failed/ 에 보낸 호출 전부(실패 포함)와 오류 문구, 오류 메시지 끝이 그 경로다.
trace 는 원본이 크다 — stdout 은 요약(서비스·span 수·오류·느린 span)이고 원본은 호출 디렉터리의 raw.json.
"""


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


def traceql_main(argv: Sequence[str] | None = None) -> None:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list and args_list[0] in TRACES_SUBCOMMANDS:
        traces_main(args_list)
        return
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
    client = _client()
    finish(client, lambda: traceql_run(client, args), "traceql-failed", a.max_lines, a.json)


# ── 환원 연산 — traceql.py 의 서브커맨드 (계산은 obs.traces, 표는 여기) ─────────────────

TRACES_SUBCOMMANDS: tuple[str, ...] = ("exemplars", "critical-path", "service-graph", "diff")


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


TRACES_COMMON_DOC = """원본(span 트리 · 검색 응답)은 호출 디렉터리의 raw.json 에 남고 stdout 은 환원물이다. 창은 summary.txt 머리에
남는다. stdout 은 --max-lines 에서 잘린다. --json 은 경로만.
실패해도(HTTP 오류 · 전송 실패 · 예산 초과) 호출 디렉터리가 남는다 — tmp/obs/<시각>-<명령>-failed/ 에 보낸 호출 전부(실패 포함)와 오류 문구, 오류 메시지 끝이 그 경로다."""

EXEMPLARS_DOC = """traceql.py exemplars [--q '<조건식>'] [--since 1h] [--k 3] [--quantile 0.95] [--baseline-range 7d] [--limit 100]

서버 span 을 오류 · 느림 · 정상 셋으로 나눠 묶음마다 대표 트레이스 k 개 — 트레이스를 열지 않는다.
느림의 임계는 엔드포인트(서비스, 서버 span 이름)마다: 평소 p95(조사 창 직전 --baseline-range 의 spanmetrics) → 조사 창 p95 →
표본 p95 순으로 있는 것. 건마다 적용한 임계와 그 출처(baseline · window · sample)가 붙는다. metrics 1 회 + traces 검색 1 회.
요약 줄은 표본 수 · 임계 초과 수 · 평소 기대 비율((1 − 분위수) × 100%). 검색이 --limit 을 넘으면 절단 표시.

""" + TRACES_COMMON_DOC

CRITICAL_PATH_DOC = """traceql.py critical-path <traceID>

트레이스 하나의 임계경로(CRISP — 마지막에 끝난 자식을 거슬러 따라간다)와 병목(self 시간 내림차순 상위 3).
경로 줄은 `+시작 오프셋  길이  self  서비스 / 이름` — 깊이만큼 들여 쓴다. 부모보다 늦게 끝난 자식은 부모 끝에서 잘린다(잘림 표시),
부모 밖에서 통째로 돈 서브트리는 버리고 그 수를 헤더에. 루트가 둘 이상이면 불완전한 트레이스 표시. traces 1 회.

""" + TRACES_COMMON_DOC

SERVICE_GRAPH_DOC = """traceql.py service-graph [--since 1h] [--quantile 0.95] [--baseline-range 7d]

호출 그래프 — spanmetrics 의 클라이언트 span 행을 (호출한 서비스 → 불린 서비스) 아래 엔드포인트마다 한 줄:
`×배율  건수  오류율  클라이언트 p95(평소)  서버 p95  엔드포인트`. 배율 = 조사 창 p95 / 평소 p95, 내림차순.
불린 서비스는 같은 이름의 서버 span 행으로 찾는다 — 없거나 둘 이상이면 미확인. metrics 2 회(traces 를 부르지 않는다).
창 안 호출이 0 이면 "호출 0", 행 자체가 없으면 "데이터 없음".

""" + TRACES_COMMON_DOC

DIFF_DOC = """traceql.py diff <느린 traceID> <정상 traceID>

같은 루트 이름의 두 트레이스를 (서비스, 이름) 으로 짝지어 시간 차이만: `Δ  느린 합ms×건수  정상 합ms×건수  서비스 / 이름`.
한쪽에만 있는 span 은 그 표시. 루트 이름이 다르면 거부한다. traces 2 회.

""" + TRACES_COMMON_DOC


@dataclass
class TracesArgs:
    cmd: str                                 # exemplars · critical-path · service-graph · diff
    q: str = ""                              # exemplars 의 조건식(중괄호 없이)
    since: str = "1h"
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


def traces_run(client: Client, a: TracesArgs, at: float | None = None) -> RunResult:
    """→ (Evidence[Lines], 원본, kind, 창). validate 가 요청 앞. 계산은 obs.traces 가, 표는 *_lines 가. stdout 을 만지지 않는다."""
    a.validate()
    window = Window.last(a.since, a.end, at)
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


def traces_main(argv: Sequence[str]) -> None:
    """argparse 서브파서 → TracesArgs → traces_run → _finish."""
    ap = argparse.ArgumentParser(prog="traceql.py", description="트레이스 환원 연산 — %s. 각 서브커맨드의 --help 에 설명 전부." % " · ".join(TRACES_SUBCOMMANDS),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<%s>" % "|".join(TRACES_SUBCOMMANDS))
    fmt = argparse.RawDescriptionHelpFormatter

    def window(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--since", default="1h", help="창 길이. 기본 1h")
        sp.add_argument("--end", default=None, help="창 끝 (now · -10m · unix초 · RFC3339). 기본 now")

    def distribution(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--quantile", type=float, default=DEFAULT_QUANTILE, help="분위수 (0, 1). 기본 %s" % DEFAULT_QUANTILE)
        sp.add_argument("--baseline-range", default=DEFAULT_BASELINE_RANGE,
                        help="평소 분포의 구간 — 조사 창 바로 앞. 기본 %s" % DEFAULT_BASELINE_RANGE)

    sp_e = sub.add_parser("exemplars", help="느림 · 오류 · 정상 묶음별 대표 트레이스", description=EXEMPLARS_DOC, formatter_class=fmt)
    sp_e.add_argument("--q", default="", help="TraceQL 조건식(중괄호 없이). 비우면 서버 span 전부")
    window(sp_e)
    sp_e.add_argument("--k", type=int, default=DEFAULT_EXEMPLARS_K, help="묶음별 상위 k. 기본 %d" % DEFAULT_EXEMPLARS_K)
    distribution(sp_e)
    sp_e.add_argument("--limit", type=int, default=DEFAULT_SEARCH_LIMIT, help="검색 트레이스 상한. 기본 %d" % DEFAULT_SEARCH_LIMIT)
    _common(sp_e)
    sp_c = sub.add_parser("critical-path", help="트레이스 하나의 임계경로와 병목", description=CRITICAL_PATH_DOC, formatter_class=fmt)
    sp_c.add_argument("trace_id", help="traceID")
    _common(sp_c)
    sp_g = sub.add_parser("service-graph", help="엔드포인트별 호출 엣지 — 건수 · 오류율 · p95 배율", description=SERVICE_GRAPH_DOC, formatter_class=fmt)
    window(sp_g)
    distribution(sp_g)
    _common(sp_g)
    sp_d = sub.add_parser("diff", help="같은 루트의 두 트레이스 — span 별 시간 차이", description=DIFF_DOC, formatter_class=fmt)
    sp_d.add_argument("slow_id", help="느린 트레이스의 traceID")
    sp_d.add_argument("normal_id", help="정상 트레이스의 traceID")
    _common(sp_d)
    a = ap.parse_args(argv)
    args = TracesArgs(cmd=a.cmd, q=getattr(a, "q", ""), since=getattr(a, "since", "1h"), end=getattr(a, "end", None),
                      k=getattr(a, "k", DEFAULT_EXEMPLARS_K), quantile=getattr(a, "quantile", DEFAULT_QUANTILE),
                      baseline_range=getattr(a, "baseline_range", DEFAULT_BASELINE_RANGE), limit=getattr(a, "limit", DEFAULT_SEARCH_LIMIT),
                      trace_id=getattr(a, "trace_id", None), slow_id=getattr(a, "slow_id", None), normal_id=getattr(a, "normal_id", None))
    client = _client()
    finish(client, lambda: traces_run(client, args), "traceql-%s-failed" % a.cmd, a.max_lines, a.json)


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
