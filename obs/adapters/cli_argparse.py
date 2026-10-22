"""obs.adapters.cli_argparse — promql · logql · traceql 의 argparse 선언. **선언만** — 분기 · 환경 · Client 가 없다.

기본값은 여기 없다 — 모든 `add_argument` 의 `default` 는 `argparse.SUPPRESS` 라 생략한 옵션은 Namespace 에 자리가 없고,
`obs.adapters.cli` 의 `*Args` dataclass 가 기본값을 안다(`map_args`). 도움말 문구의 "기본 …" 은 그 상수를 찍는다.
배선(`*_main` · `*_run` · `finish`)은 `obs.adapters.cli` 에 있다 — 이 파일은 사용자가 보는 usage · `--help` 의 전부다.
"""
from __future__ import annotations

import argparse
from typing import TypeAlias

from obs.logs import DEFAULT_AROUND_LIMIT, DEFAULT_AROUND_SPAN, DEFAULT_TEMPLATES_K
from obs.metrics import DEFAULT_TOP_K, TOP_AGGS
from obs.traces import DEFAULT_BASELINE_RANGE, DEFAULT_EXEMPLARS_K, DEFAULT_QUANTILE, DEFAULT_SEARCH_LIMIT

DEFAULT_MAX_LINES: int = 40
DEFAULT_LOG_LIMIT: int = 30
DEFAULT_SINCE: str = "1h"

METRICS_SUBCOMMANDS: tuple[str, ...] = ("summarize", "compare", "top", "red")
LOGS_SUBCOMMANDS: tuple[str, ...] = ("histogram", "templates", "around", "first-seen")
TRACES_SUBCOMMANDS: tuple[str, ...] = ("exemplars", "critical-path", "service-graph", "diff")

FMT = argparse.RawDescriptionHelpFormatter
SubParsers: TypeAlias = "argparse._SubParsersAction[argparse.ArgumentParser]"  # pyright: ignore[reportPrivateUsage] — add_subparsers 의 반환 타입, 공개 이름이 없다


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

def output_options(ap: argparse.ArgumentParser) -> None:
    """--max-lines · --json — 여섯 명령이 공유하는 stdout 절제 옵션(dest max_lines · as_json). default 는 SUPPRESS."""
    ap.add_argument("--max-lines", type=int,
                    help="stdout 줄 수 상한. 기본 %d — 나머지는 호출 디렉터리의 summary.txt 에" % DEFAULT_MAX_LINES)
    ap.add_argument("--json", dest="as_json", action="store_true", help="stdout 에 요약을 찍지 않고 호출 디렉터리 경로만(파일은 성공 · 실패 모두 남는다)")


def _parser(prog: str, description: str) -> argparse.ArgumentParser:
    return argparse.ArgumentParser(prog=prog, description=description, formatter_class=FMT, argument_default=argparse.SUPPRESS)


def _subparsers(ap: argparse.ArgumentParser, names: tuple[str, ...]) -> SubParsers:
    return ap.add_subparsers(dest="cmd", required=True, metavar="<%s>" % "|".join(names))


def _subparser(sub: SubParsers, name: str, help: str, description: str) -> argparse.ArgumentParser:
    return sub.add_parser(name, help=help, description=description, formatter_class=FMT, argument_default=argparse.SUPPRESS)


def promql_parser() -> argparse.ArgumentParser:
    ap = _parser("promql.py", PROMQL_DOC)
    ap.add_argument("query", nargs="?", help="PromQL (또는 --labels/--label-values/--series 의 selector)")
    ap.add_argument("--end")
    ap.add_argument("--range", dest="range_")
    ap.add_argument("--step")
    ap.add_argument("--points", action="store_true", help="구간 조회에서 시각별 값을 전부 출력")
    ap.add_argument("--labels", action="store_true")
    ap.add_argument("--label-values", metavar="NAME")
    ap.add_argument("--series", metavar="SELECTOR")
    output_options(ap)
    return ap


def metrics_parser() -> argparse.ArgumentParser:
    ap = _parser("promql.py", "메트릭 환원 연산 — summarize · compare · top · red. 각 서브커맨드의 --help 에 설명 전부.")
    sub = _subparsers(ap, METRICS_SUBCOMMANDS)

    def common(sp: argparse.ArgumentParser, end_help: str) -> None:
        sp.add_argument("query", help="PromQL")
        sp.add_argument("--end", help=end_help)
        output_options(sp)

    sp_sum = _subparser(sub, "summarize", "시계열별 n·min·p50·p95·max·last·기울기", SUMMARIZE_DOC)
    sp_sum.add_argument("--since", help="창 길이. 기본 %s" % DEFAULT_SINCE)
    common(sp_sum, "창 끝 (now · -10m · unix초 · RFC3339). 기본 now")
    sp_sum.add_argument("--step", help="서브쿼리 해상도. 기본 창 길이/60 (최소 15s)")
    sp_cmp = _subparser(sub, "compare", "지금 창 vs 기준선 창 — 시계열별 p95 차이만", COMPARE_DOC)
    sp_cmp.add_argument("--since", help="창 길이. 기본 %s" % DEFAULT_SINCE)
    common(sp_cmp, "창 끝 (now · -10m · unix초 · RFC3339). 기본 now")
    sp_cmp.add_argument("--baseline", help="기준선 창 — 같은 식의 offset: 1d(기본 — 1일 전 같은 시각) · 1w · prev(직전 창)")
    sp_cmp.add_argument("--step", help="서브쿼리 해상도(두 창에 같다). 기본 창 길이/60 (최소 15s)")
    sp_top = _subparser(sub, "top", "레이블 축 상위 k (--end 시각의 즉시 조회)", TOP_DOC)
    common(sp_top, "평가 시각 (now · -10m · unix초 · RFC3339). 기본 now — top 은 창이 없다")
    sp_top.add_argument("--by", help="레이블 축. 쉼표로 여러 개. 기본 pod")
    sp_top.add_argument("--k", type=int, help="상위 k. 기본 %d" % DEFAULT_TOP_K)
    sp_top.add_argument("--agg", choices=TOP_AGGS, help="축으로 접는 집계. 기본 sum")
    sp_red = _subparser(sub, "red", "서비스 하나의 R · E · D — 지금 창 vs 기준선 창", RED_DOC)
    sp_red.add_argument("service", help="서비스 이름(spanmetrics 의 service_name)")
    sp_red.add_argument("--since", help="창 길이. 기본 %s" % DEFAULT_SINCE)
    sp_red.add_argument("--end", help="창 끝 (now · -10m · unix초 · RFC3339). 기본 now")
    output_options(sp_red)
    sp_red.add_argument("--baseline", help="기준선 창 — 같은 식의 offset: 1d(기본 — 1일 전 같은 시각) · 1w · prev(직전 창)")
    return ap


def logql_parser() -> argparse.ArgumentParser:
    ap = _parser("logql.py", LOGQL_DOC)
    ap.add_argument("query", nargs="?", help="LogQL")
    ap.add_argument("--since")
    ap.add_argument("--end")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--direction", choices=("backward", "forward"))
    ap.add_argument("--width", type=int)
    ap.add_argument("--labels", action="store_true")
    ap.add_argument("--label-values", metavar="NAME")
    ap.add_argument("--series", metavar="SELECTOR")
    output_options(ap)
    return ap


def logs_parser() -> argparse.ArgumentParser:
    ap = _parser("logql.py", "로그 환원 연산 — %s. 각 서브커맨드의 --help 에 설명 전부." % " · ".join(LOGS_SUBCOMMANDS))
    sub = _subparsers(ap, LOGS_SUBCOMMANDS)

    def common(sp: argparse.ArgumentParser, window: bool = True) -> None:
        sp.add_argument("selector", help="LogQL 스트림 selector — 예: '{namespace=\"x\"}'")
        if window:
            sp.add_argument("--since", help="창 길이. 기본 %s" % DEFAULT_SINCE)
            sp.add_argument("--end", help="창 끝 (now · -10m · unix초 · RFC3339). 기본 now")
        output_options(sp)

    sp_h = _subparser(sub, "histogram", "by 값별 · 구간별 건수 표", HISTOGRAM_DOC)
    common(sp_h)
    sp_h.add_argument("--by", help="행의 레이블 축. 쉼표로 여러 개. 기본 detected_level")
    sp_h.add_argument("--step", help="구간 폭. 기본 창 길이/60 (최소 15s)")
    sp_t = _subparser(sub, "templates", "로그 군집 상위 k + 예시", TEMPLATES_DOC)
    common(sp_t)
    sp_t.add_argument("--k", type=int, help="상위 k. 기본 %d" % DEFAULT_TEMPLATES_K)
    sp_t.add_argument("--step", help="sample 구간 폭. 기본 창 길이/60 (최소 15s)")
    sp_a = _subparser(sub, "around", "시각 앞뒤의 문맥", AROUND_DOC)
    common(sp_a, window=False)
    sp_a.add_argument("--ts", help="중심 시각 (--ts=-30m · unix초 · RFC3339). 필수")
    sp_a.add_argument("--span", help="한 쪽 폭. 기본 %s" % DEFAULT_AROUND_SPAN)
    sp_a.add_argument("--limit", type=int, help="한 쪽 줄 수 상한. 기본 %d" % DEFAULT_AROUND_LIMIT)
    sp_f = _subparser(sub, "first-seen", "템플릿의 첫 발생", FIRST_SEEN_DOC)
    common(sp_f)
    sp_f.add_argument("template", help="패턴 — templates 출력의 템플릿 그대로")
    return ap


def traceql_parser() -> argparse.ArgumentParser:
    ap = _parser("traceql.py", TRACEQL_DOC)
    ap.add_argument("cmd", choices=("search", "trace", "tags", "tag-values"))
    ap.add_argument("arg", nargs="?", help="trace: traceID · tag-values: 태그 이름")
    ap.add_argument("--q")
    ap.add_argument("--since")
    ap.add_argument("--end")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--min-duration")
    ap.add_argument("--scope", choices=("span", "resource", "intrinsic"))
    ap.add_argument("--top", type=int)
    output_options(ap)
    return ap


def traces_parser() -> argparse.ArgumentParser:
    ap = _parser("traceql.py", "트레이스 환원 연산 — %s. 각 서브커맨드의 --help 에 설명 전부." % " · ".join(TRACES_SUBCOMMANDS))
    sub = _subparsers(ap, TRACES_SUBCOMMANDS)

    def window(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--since", help="창 길이. 기본 %s" % DEFAULT_SINCE)
        sp.add_argument("--end", help="창 끝 (now · -10m · unix초 · RFC3339). 기본 now")

    def distribution(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--quantile", type=float, help="분위수 (0, 1). 기본 %s" % DEFAULT_QUANTILE)
        sp.add_argument("--baseline-range", help="평소 분포의 구간 — 조사 창 바로 앞. 기본 %s" % DEFAULT_BASELINE_RANGE)

    sp_e = _subparser(sub, "exemplars", "느림 · 오류 · 정상 묶음별 대표 트레이스", EXEMPLARS_DOC)
    sp_e.add_argument("--q", help="TraceQL 조건식(중괄호 없이). 비우면 서버 span 전부")
    window(sp_e)
    sp_e.add_argument("--k", type=int, help="묶음별 상위 k. 기본 %d" % DEFAULT_EXEMPLARS_K)
    distribution(sp_e)
    sp_e.add_argument("--limit", type=int, help="검색 트레이스 상한. 기본 %d" % DEFAULT_SEARCH_LIMIT)
    output_options(sp_e)
    sp_c = _subparser(sub, "critical-path", "트레이스 하나의 임계경로와 병목", CRITICAL_PATH_DOC)
    sp_c.add_argument("trace_id", help="traceID")
    output_options(sp_c)
    sp_g = _subparser(sub, "service-graph", "엔드포인트별 호출 엣지 — 건수 · 오류율 · p95 배율", SERVICE_GRAPH_DOC)
    window(sp_g)
    distribution(sp_g)
    output_options(sp_g)
    sp_d = _subparser(sub, "diff", "같은 루트의 두 트레이스 — span 별 시간 차이", DIFF_DOC)
    sp_d.add_argument("slow_id", help="느린 트레이스의 traceID")
    sp_d.add_argument("normal_id", help="정상 트레이스의 traceID")
    output_options(sp_d)
    return ap
