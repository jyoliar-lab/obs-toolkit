"""tests/record.py — fixture 녹화기 · fixture 형식 · 응답 고르기.

`FakeTransport`(conftest) 의 반대 방향이다: 진짜 `_urllib_transport` 를 감싸 **나간 호출**(백엔드 논리 이름 · 네이티브 경로 ·
query string · 기준 시각 `at`)과 **받은 응답**을 한 파일에 떨어뜨린다. `Client(transport=Recorder(...))` 로 끼운다 —
어떤 실행(`*_run` · 환원 연산)에든 끼울 수 있는 transport 이고, 코어는 고치지 않는다.

fixture 형식 — JSON 봉투. `.json` 확장자를 유지해 편집기·린터가 깨진 파일로 보지 않고, 요청·응답을 각각 기계로 꺼낸다:
  {
   "format": "obs-fixture/1",
   "recorded": "tests/record.py" | "manual",        # manual 이면 "reason" 이 재현 불가 이유를 말한다. 표시 없는 파일은 전부 녹화기가 만든 것
   "calls": [                                        # 한 실행의 호출 전부 — 순서대로. traces 는 api/search → api/traces/<id> 둘
    {"request":  {"at": <unix 초>, "at_rfc3339": "…Z", "backend": "metrics", "path": "api/v1/query",
                  "query_string": "query=up&time=…", "params": {"query": "up", "time": "…"}},
     "response": {"status": 200, "body": {…}}}
   ]
  }
- `at` 은 명시 필드다 — URL 의 time=/start= 에서 역산하지 않는다(백엔드마다 단위가 다르다: PromQL 초 · LogQL 나노초 · TraceQL 초).
  래퍼는 URL 에서 `at` 을 알 수 없으므로, 녹화 실행이 `at = now()` 를 한 번 읽어 `*_run(…, at=at)` 과 래퍼에 같은 값을 준다.
- 물리(호스트 · 프록시 접두 · 데이터소스 uid · 토큰)는 실리지 않는다 — URL 은 `Config.proxy_url(backend, "")` 접두를 잘라
  `Response.path` 와 같은 층(네이티브 경로 · query string)만 남기고, 헤더(Authorization)는 기록하지 않는다. 접두를 모르는 URL 은 거부한다.

실환경 재녹화 — 토큰(OBS_GRAFANA_TOKEN)이 있어야 돈다. `test_` 접두가 없어 pytest 가 수집하지 않는다:
  python -m tests.record --all                                  # RECIPES 전부 — 한 실행의 모든 fixture 가 같은 at
  python -m tests.record promql-instant-up.json traceql-trace.json
  python -m tests.record --list
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from obs.adapters import cli
from obs.core import (
    JSON, LOGICAL_BACKENDS, Budget, Client, Config, ObsError, Transport, Window, fmt_rfc3339, json_int, json_list, json_obj, json_str,
    load_config, now, to_dict, token_from_env,
)
from obs.traces import Span, check_json, parse_search, parse_spans
from obs.core import _urllib_transport  # pyright: ignore[reportPrivateUsage] — 녹화기는 진짜 GET 을 감싼다(코어를 고치지 않는다 — ops-workflow#103)

ROOT: Path = Path(__file__).resolve().parents[1]
FIXTURES: Path = ROOT / "fixtures"

FORMAT: str = "obs-fixture/1"
RECORDED_BY: str = "tests/record.py"   # 녹화기가 만든 파일의 표시
RECORDED_MANUAL: str = "manual"        # 재현할 수 없는 조건의 손 기록 — reason 필수

QueryParams = dict[str, str | list[str]]
"""query string 을 풀어 놓은 것 — 같은 키가 여러 번이면 목록(match[] 등)."""


# ── 형식 — dataclass 로 다루고 JSON 으로 쓴다 ──────────────────────────────────────

@dataclass(frozen=True)
class Request:
    """나간 호출 — 물리 없이. `at` 은 이 호출을 낳은 기준 시각(manual 기록에서 불명이면 None)."""
    at: float | None
    at_rfc3339: str | None
    backend: str        # 논리 이름 — metrics · logs · traces
    path: str           # 네이티브 경로(api/v1/query 등) — 호스트 · 프록시 접두 · uid 없음
    query_string: str   # 나간 그대로(URL 인코딩된 채)
    params: QueryParams

    @property
    def key(self) -> str:
        """정규화한 요청 키 — replay(tests/replay.py) 가 나간 요청의 키와 비교한다."""
        return normalize_key(self.path, self.params)


@dataclass(frozen=True)
class Reply:
    status: int
    body: JSON          # 파싱된 JSON — JSON 이 아니면 str (Client.get 과 같다)


@dataclass(frozen=True)
class RecordedCall:
    request: Request
    response: Reply


@dataclass(frozen=True)
class Fixture:
    format: str
    recorded: str            # RECORDED_BY · RECORDED_MANUAL
    reason: str | None       # manual 일 때 재현 불가 이유
    calls: list[RecordedCall]

    @property
    def manual(self) -> bool:
        return self.recorded == RECORDED_MANUAL


def parse_params(query_string: str) -> QueryParams:
    out: QueryParams = {}
    for k, v in urllib.parse.parse_qsl(query_string, keep_blank_values=True):
        prev = out.get(k)
        if prev is None:
            out[k] = v
        elif isinstance(prev, list):
            prev.append(v)
        else:
            out[k] = [prev, v]
    return out


def normalize_key(path: str, params: QueryParams) -> str:
    """요청 키 — `<네이티브 경로>?<키 정렬된 파라미터>`. 같은 키가 여럿이면 값도 정렬한다. 순서만 다른 요청은 같은 키다.
    값은 문자열 그대로 비교한다 — 녹화와 replay 가 같은 코드로 만드니 표기(1000.0 vs 1000)도 같다."""
    items = sorted((k, v) for k, vs in params.items() for v in (vs if isinstance(vs, list) else [vs]))
    return path + ("?" + urllib.parse.urlencode(items) if items else "")


def strip_physical(url: str, config: Config, at: float | None) -> Request:
    """물리 URL → Request. `Config.proxy_url(backend, "")` 접두(호스트 · 프록시 접두 · uid)를 잘라 논리 이름을 알아내고
    네이티브 경로 · query string 만 남긴다. 어느 접두에도 맞지 않으면 기록을 거부한다 — 호스트를 fixture 에 싣지 않는다."""
    for backend in LOGICAL_BACKENDS:
        prefix = config.proxy_url(backend, "")
        if url.startswith(prefix):
            path, _, qs = url[len(prefix):].partition("?")
            return Request(at=at, at_rfc3339=fmt_rfc3339(at) if at is not None else None, backend=backend,
                           path=path, query_string=qs, params=parse_params(qs))
    raise ValueError("물리 접두를 모르는 URL — 기록하지 않는다(호스트 · uid 를 fixture 에 싣지 않는다)")


def decode_body(raw: bytes) -> JSON:
    """Client.get 과 같은 규칙 — 빈 본문은 {}, JSON 이 아니면 str."""
    text = raw.decode("utf-8", "replace")
    if not text:
        return {}
    try:
        parsed: JSON = json.loads(text)
    except ValueError:
        return text
    return parsed


def write_fixture(path: Path, calls: Sequence[RecordedCall], recorded: str = RECORDED_BY, reason: str | None = None) -> Path:
    """봉투를 쓴다. manual 이면 reason 이 필수 — 표시 없는 파일은 전부 녹화기가 만든 것이어야 한다."""
    if not calls:
        raise ValueError("기록된 호출이 없다 — 쓰지 않는다")
    if recorded == RECORDED_MANUAL and not reason:
        raise ValueError("manual 기록에는 재현 불가 이유(reason)가 필요하다")
    envelope: dict[str, JSON] = {"format": FORMAT, "recorded": recorded}
    if reason:
        envelope["reason"] = reason
    envelope["calls"] = to_dict(list(calls))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def parse_fixture(data: JSON, where: str) -> Fixture:
    """외부 경계(파일의 JSON) → Fixture. 형식이 아니면 ValueError — 옛 형식(응답 본문만)은 여기서 걸린다."""
    o = json_obj(data)
    if json_str(o.get("format")) != FORMAT:
        raise ValueError("%s: fixture 형식(%s)이 아니다 — tests/record.py 로 녹화한다" % (where, FORMAT))
    calls: list[RecordedCall] = []
    for item in json_list(o.get("calls")):
        c = json_obj(item)
        rq, rs = json_obj(c.get("request")), json_obj(c.get("response"))
        at_raw = rq.get("at")
        at = float(at_raw) if isinstance(at_raw, (int, float)) and not isinstance(at_raw, bool) else None
        params: QueryParams = {}
        for k, v in json_obj(rq.get("params")).items():
            params[k] = [json_str(x) for x in v] if isinstance(v, list) else json_str(v)
        calls.append(RecordedCall(
            request=Request(at=at, at_rfc3339=json_str(rq.get("at_rfc3339")) or None, backend=json_str(rq.get("backend")),
                            path=json_str(rq.get("path")), query_string=json_str(rq.get("query_string")), params=params),
            response=Reply(status=json_int(rs.get("status")), body=rs.get("body")),
        ))
    reason = o.get("reason")
    return Fixture(format=FORMAT, recorded=json_str(o.get("recorded")), reason=json_str(reason) if reason is not None else None, calls=calls)


def read_fixture(path: Path) -> Fixture:
    return parse_fixture(json.loads(path.read_text(encoding="utf-8")), str(path))


def response_of(fx: Fixture, path: str | None, where: str) -> JSON:
    """응답만 — 호출이 하나면 그것, 여럿이면 `path`(네이티브 경로 조각)로 하나를 고른다. 못 고르면 ValueError(있는 경로를 말한다)."""
    calls = fx.calls if path is None else [c for c in fx.calls if path in c.request.path]
    if len(calls) != 1:
        raise ValueError("%s: 호출 %d개 — path=%r 로 하나를 고를 수 없다. 있는 경로: %s"
                         % (where, len(calls), path, [c.request.path for c in fx.calls]))
    return calls[0].response.body


# ── 녹화기 — Client.transport 래퍼 ───────────────────────────────────────────────

@dataclass
class Recorder:
    """(url, headers, timeout) → inner 로 보내고, 나간 호출과 받은 응답을 `calls` 에 순서대로 쌓는다. 헤더는 기록하지 않는다."""
    inner: Transport
    config: Config
    at: float
    calls: list[RecordedCall] = field(default_factory=list[RecordedCall])

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        status, raw = self.inner(url, headers, timeout)
        self.calls.append(RecordedCall(request=strip_physical(url, self.config, self.at), response=Reply(status=status, body=decode_body(raw))))
        return status, raw

    def save(self, path: Path) -> Path:
        return write_fixture(path, self.calls)


def real_client(at: float) -> tuple[Client, Recorder]:
    """실환경 Client — 토큰은 env 에서, transport 는 진짜 urllib 을 감싼 Recorder."""
    token = token_from_env()
    if not token:
        raise ObsError("OBS_GRAFANA_TOKEN 이 비어 있다 — 녹화는 실환경 토큰이 필요하다(.claude/settings.local.json 의 env)")
    config = load_config()
    rec = Recorder(inner=_urllib_transport, config=config, at=at)
    return Client(config=config, token=token, transport=rec), rec


Recipe = Callable[[Client, float], None]
"""한 fixture 를 낳는 실행 — Client 와 기준 시각 `at` 을 받아 `*_run(…, at=at)` 을 부른다. 호출이 여럿이어도 된다."""


class Unreproducible(Exception):
    """재현할 수 없는 조건 — 이 fixture 는 recorded: manual 예외 후보다."""


def record(name: str, recipe: Recipe, at: float, out: Path = FIXTURES) -> list[Path]:
    """파일 이름 · 호출(전부) · 응답이 한 번에 — recipe 를 Recorder 가 끼워진 Client 로 돌리고 `out/name` 에 쓴다.
    같은 실행의 호출 하나를 다른 이름으로도 쓰는 파생(DERIVED)이 있으면 그 파일도 쓴다. 쓴 경로 전부를 돌려준다."""
    client, rec = real_client(at)
    recipe(client, at)
    written = [rec.save(out / name)]
    for derived, (source, index) in DERIVED.items():
        if source == name:
            written.append(write_fixture(out / derived, [rec.calls[index]]))
    return written


# ── 레시피 — 기존 fixture 13개. 테스트가 보내는 것과 같은 인자로 부른다 ────────────────

CPU_QUERY: str = 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="careerhub-prod"}[5m]))'   # tests/test_metrics.py 와 같다
EMPTY_QUERY: str = CPU_QUERY.replace('namespace="careerhub-prod"', 'namespace="no-such-namespace"')          # 없는 namespace → 빈 응답
LOG_SELECTOR: str = '{namespace="careerhub-prod"}'
LOG_METRIC_QUERY: str = "sum by (detected_level) (count_over_time(%s[5m]))" % LOG_SELECTOR
EMPTY_LOG_SELECTOR: str = '{namespace="no-such-namespace"}'   # 없는 namespace → 빈 창(logs 환원 연산의 "데이터 없음" fixture)
FIRST_SEEN_TEMPLATE: str = "<_> level=error <_>"               # 빈 창의 first_seen — 템플릿은 아무것이나 창 안에 없다
TRACE_SEARCH: cli.TraceqlArgs = cli.TraceqlArgs(cmd="search", since="24h", limit=3)
UP_APISERVER: str = 'up{job="apiserver"}'   # metadata 조회(labels · series)의 selector — 시계열 셋
NO_SUCH_SERVICE: str = 'resource.service.name = "no-such-service"'   # exemplars 의 빈 표본 — 없는 서비스의 조건식


def _promql(a: cli.PromqlArgs) -> Recipe:
    def run(client: Client, at: float) -> None:
        cli.promql_run(client, a, at=at)
    return run


def _metrics(a: cli.MetricsArgs) -> Recipe:
    def run(client: Client, at: float) -> None:
        cli.metrics_run(client, a, at=at)
    return run


def _logql(a: cli.LogqlArgs) -> Recipe:
    def run(client: Client, at: float) -> None:
        cli.logql_run(client, a, at=at)
    return run


def _logs(a: cli.LogsArgs) -> Recipe:
    def run(client: Client, at: float) -> None:
        cli.logs_run(client, a, at=at)
    return run


def _traceql(a: cli.TraceqlArgs) -> Recipe:
    def run(client: Client, at: float) -> None:
        cli.traceql_run(client, a, at=at)
    return run


def _traces_op(a: cli.TracesArgs) -> Recipe:
    """traces 환원 연산(traceql 서브커맨드)의 레시피 — 녹화되는 호출은 연산이 보내는 것뿐."""
    def run(client: Client, at: float) -> None:
        cli.traces_run(client, a, at=at)
    return run


def _scratch_client() -> Client:
    """녹화되지 않는 별도 Client — fixture 에 담을 트레이스를 고르는 데만 쓴다(검색 · 후보 열기). 예산은 넉넉히."""
    token = token_from_env()
    if not token:
        raise ObsError("OBS_GRAFANA_TOKEN 이 비어 있다 — 녹화는 실환경 토큰이 필요하다(.claude/settings.local.json 의 env)")
    return Client(config=load_config(), token=token, budget=Budget(max_queries=80))


def _open(client: Client, trace_id: str) -> list[Span]:
    return parse_spans(check_json(client.get("traces", "api/traces/%s" % trace_id).body, "traces"))


def _depth_of(spans: Sequence[Span]) -> dict[str, int]:
    by_id = {s.span_id: s for s in spans}
    out: dict[str, int] = {}
    for s in spans:
        d, p = 0, s.parent_id
        while p and p in by_id and d < 64:
            d, p = d + 1, by_id[p].parent_id
        out[s.span_id] = d
    return out


def _deep_trace(at: float) -> str:
    """critical_path fixture 의 트레이스 — 가장 긴 span 이 루트이고 · 깊이 ≥ 2 인 span 이 있고 · 그중 하나가 루트 길이의 절반 이상인 것.
    24h 의 서버 span 트레이스(20ms 이상)를 열어 첫 후보를 고른다. 없으면 Unreproducible."""
    client = _scratch_client()
    w = Window.last("24h", at=at)
    body = check_json(client.get("traces", "api/search", {"q": "{ kind = server }", "start": int(w.start), "end": int(w.end), "limit": 100, "minDuration": "20ms"}).body, "traces")
    seen: set[str] = set()
    for h in parse_search(body):
        if h.trace_id in seen or len(seen) >= 40:
            continue
        seen.add(h.trace_id)
        spans = _open(client, h.trace_id)
        if not spans:
            continue
        by_id = {s.span_id: s for s in spans}
        depth = _depth_of(spans)
        longest = max(spans, key=lambda s: s.end_ns - s.start_ns)
        root_len = longest.end_ns - longest.start_ns
        is_root = not longest.parent_id or longest.parent_id not in by_id
        deep = [s for s in spans if depth[s.span_id] >= 2]
        if is_root and deep and any((s.end_ns - s.start_ns) * 2 >= root_len for s in deep):
            return h.trace_id
    raise Unreproducible("24h 안에 '루트 최장 · 깊이 ≥ 2 · 그 span 이 루트의 절반 이상' 인 트레이스가 없다 — 창을 넓히거나 recorded: manual 로")


def _same_root_pair(at: float) -> tuple[str, str]:
    """diff fixture 의 두 트레이스 — 같은 (루트 서비스, 루트 이름) 그룹 중 span 이 둘 이상인 트레이스가 가장 많은 그룹에서 가장 느린 것과 가장 빠른 것."""
    client = _scratch_client()
    w = Window.last("24h", at=at)
    body = check_json(client.get("traces", "api/search", {"q": "{ kind = server }", "start": int(w.start), "end": int(w.end), "limit": 100, "minDuration": "5ms"}).body, "traces")
    groups: dict[tuple[str, str], dict[str, float]] = {}
    for h in parse_search(body):
        try:
            ms = float(h.duration_ms)
        except ValueError:
            continue
        groups.setdefault((h.root_service, h.root_name), {})[h.trace_id] = ms
    for _, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(members) < 2:
            continue
        ordered = sorted(members.items(), key=lambda kv: -kv[1])
        slow, normal = ordered[0][0], ordered[-1][0]
        if len(_open(client, slow)) >= 2 and len(_open(client, normal)) >= 2:
            return slow, normal
    raise Unreproducible("24h 안에 같은 루트 이름의 트레이스 쌍(각각 span 둘 이상)이 없다 — 창을 넓히거나 recorded: manual 로")


def _critical_path_fixture(client: Client, at: float) -> None:
    cli.traces_run(client, cli.TracesArgs(cmd="critical-path", trace_id=_deep_trace(at)), at=at)


def _diff_fixture(client: Client, at: float) -> None:
    slow, normal = _same_root_pair(at)
    cli.traces_run(client, cli.TracesArgs(cmd="diff", slow_id=slow, normal_id=normal), at=at)


def _first_trace_id(client: Client, search: cli.TraceqlArgs, at: float) -> str:
    cli.traceql_run(client, search, at=at)
    hits = cli.parse_search(cli.check_json(client.log[-1].body, "traces"))
    if not hits:
        raise Unreproducible("search(%s, since=%s) 에 trace 가 없다 — 창을 넓히거나 recorded: manual 로" % (search.q or "", search.since))
    return hits[0].trace_id


def _search_then_trace(client: Client, at: float) -> None:
    """한 실행에 호출 둘 — api/search 로 trace id 를 얻고 api/traces/<id> 를 다시 부른다. 둘 다 순서대로 기록된다."""
    trace_id = _first_trace_id(client, TRACE_SEARCH, at)
    cli.traceql_run(client, cli.TraceqlArgs(cmd="trace", arg=trace_id), at=at)


def _error_trace(client: Client, at: float) -> None:
    """오류 trace 는 주문할 수 없다 — Tempo 검색 상한(168h) 안에서 `{ status = error }` 를 찾아 없으면 Unreproducible."""
    trace_id = _first_trace_id(client, cli.TraceqlArgs(cmd="search", q="{ status = error }", since="168h", limit=1), at)
    cli.traceql_run(client, cli.TraceqlArgs(cmd="trace", arg=trace_id), at=at)


RECIPES: dict[str, Recipe] = {
    # 파일 이름 → 그 파일을 낳는 실행. traceql-search.json 은 DERIVED — traceql-trace.json 실행의 첫 호출과 같은 것이다
    "promql-instant-up.json": _promql(cli.PromqlArgs(query="up")),
    "promql-range-sum-up.json": _promql(cli.PromqlArgs(query="sum(up)", range_="30m")),
    "promql-instant-summarize-cpu-by-pod.json": _metrics(cli.MetricsArgs(cmd="summarize", query=CPU_QUERY)),
    "promql-instant-compare-cpu-by-pod-1d.json": _metrics(cli.MetricsArgs(cmd="compare", query=CPU_QUERY, baseline="1d")),
    "promql-instant-compare-cpu-by-pod-no-baseline.json": _metrics(cli.MetricsArgs(cmd="compare", query=CPU_QUERY, baseline="60d")),   # 보존 밖 offset
    "promql-instant-empty.json": _metrics(cli.MetricsArgs(cmd="summarize", query=EMPTY_QUERY)),
    "promql-instant-topk-memory-by-pod.json": _metrics(cli.MetricsArgs(cmd="top", query="container_memory_working_set_bytes", by="pod", k=10)),
    # red — 24h 창(1h 창은 cron 시각이 아니면 전 서비스가 호출 0). 값 · 호출 0(시계열은 있음) · 시계열 없음(없는 서비스)의 세 상태
    "promql-instant-red-posting-service-24h.json": _metrics(cli.MetricsArgs(cmd="red", service="posting-service", since="24h")),
    "promql-instant-red-api-composer-24h.json": _metrics(cli.MetricsArgs(cmd="red", service="api-composer", since="24h")),
    "promql-instant-red-no-such-service-24h.json": _metrics(cli.MetricsArgs(cmd="red", service="no-such-service", since="24h")),
    "logql-matrix-count-over-time.json": _logql(cli.LogqlArgs(query=LOG_METRIC_QUERY)),
    "logql-streams-careerhub-prod.json": _logql(cli.LogqlArgs(query=LOG_SELECTOR, limit=5)),
    "traceql-trace.json": _search_then_trace,
    "traceql-tags.json": _traceql(cli.TraceqlArgs(cmd="tags")),
    "traceql-trace-error.json": _error_trace,
    # metadata 조회 — 좁은 selector 로 스냅샷을 작게 유지한다(up 전체는 시계열 65개)
    "promql-labels-up-apiserver.json": _promql(cli.PromqlArgs(query=UP_APISERVER, labels=True)),
    "promql-label-values-job.json": _promql(cli.PromqlArgs(query="up", label_values="job")),
    "promql-series-up-apiserver.json": _promql(cli.PromqlArgs(series=UP_APISERVER)),
    "logql-labels.json": _logql(cli.LogqlArgs(labels=True)),
    "logql-label-values-namespace.json": _logql(cli.LogqlArgs(label_values="namespace")),
    "logql-series-careerhub-prod.json": _logql(cli.LogqlArgs(series=LOG_SELECTOR)),
    "traceql-tag-values-service-name.json": _traceql(cli.TraceqlArgs(cmd="tag-values", arg="resource.service.name", since="24h")),   # span 태그는 1h 창에 비기 일쑤 — 24h 의 resource 태그
    # 로그 환원 연산 — 연산마다 정상 1(careerhub-prod) + 빈 창 1(no-such-namespace). first-seen 의 정상은 templates 실행의 호출 1(DERIVED)
    "logql-histogram-by-level.json": _logs(cli.LogsArgs(cmd="histogram", selector=LOG_SELECTOR)),
    "logql-histogram-empty.json": _logs(cli.LogsArgs(cmd="histogram", selector=EMPTY_LOG_SELECTOR)),
    "logql-templates-careerhub-prod.json": _logs(cli.LogsArgs(cmd="templates", selector=LOG_SELECTOR, k=5)),
    "logql-templates-empty.json": _logs(cli.LogsArgs(cmd="templates", selector=EMPTY_LOG_SELECTOR)),
    "logql-around-careerhub-prod.json": _logs(cli.LogsArgs(cmd="around", selector=LOG_SELECTOR, ts="-30m", span="1m", limit=20)),
    "logql-around-empty.json": _logs(cli.LogsArgs(cmd="around", selector=EMPTY_LOG_SELECTOR, ts="-30m", span="1m", limit=20)),
    "logql-first-seen-empty.json": _logs(cli.LogsArgs(cmd="first-seen", selector=EMPTY_LOG_SELECTOR, template=FIRST_SEEN_TEMPLATE)),
    # 트레이스 환원 연산 — exemplars 는 24h 창(1h 는 비기 일쑤) · 빈 창은 없는 서비스 / 보존 밖 창 · critical-path 와 diff 의 트레이스는 보조가 고른다(녹화 안 됨)
    "traceql-exemplars.json": _traces_op(cli.TracesArgs(cmd="exemplars", since="24h", k=3)),
    "traceql-exemplars-empty.json": _traces_op(cli.TracesArgs(cmd="exemplars", since="24h", k=3, q=NO_SUCH_SERVICE)),
    "traceql-critical-path.json": _critical_path_fixture,
    "traceql-service-graph.json": _traces_op(cli.TracesArgs(cmd="service-graph", since="24h")),
    "traceql-service-graph-empty.json": _traces_op(cli.TracesArgs(cmd="service-graph", since="1h", end="-60d")),   # 보존 밖 — 클라이언트 행 없음
    "traceql-diff.json": _diff_fixture,
}


DERIVED: dict[str, tuple[str, int]] = {
    # 파생 파일 → (원 실행의 파일, 호출 인덱스). 같은 실행의 호출을 그 이름으로도 쓴다 — 별도 실행이면 Tempo 가 limit 안의
    # hit 을 호출마다 다르게 고를 수 있어 search 의 첫 hit 과 trace 의 id 가 어긋난다. 같은 실행에서 나오면 정합이 구조로 선다.
    "traceql-search.json": ("traceql-trace.json", 0),
    # templates 의 첫 군집 예시 호출 = 같은 템플릿의 first_seen 질의 — 같은 실행에서 나와야 "templates 의 템플릿을 그대로 first_seen 에" 가 구조로 선다
    "logql-first-seen-careerhub-prod.json": ("logql-templates-careerhub-prod.json", 1),
}


def fixture_names() -> list[str]:
    """녹화기가 만드는 파일 전부 — 실행의 파일과 파생 파일."""
    return sorted([*RECIPES, *DERIVED])


def _clip(text: str, width: int = 90) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.record", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="fixture 파일 이름(RECIPES 의 키). 없으면 --all")
    ap.add_argument("--all", action="store_true", help="RECIPES 전부")
    ap.add_argument("--list", action="store_true", help="레시피 이름만 보이고 끝")
    ap.add_argument("--out", type=Path, default=FIXTURES, help="쓸 디렉터리. 기본 fixtures/")
    ap.add_argument("--at", type=float, default=None, help="기준 시각(unix 초). 기본 now — 한 실행의 모든 fixture 가 같은 at 을 받는다")
    a = ap.parse_args(argv)
    if a.list:
        print("\n".join("%s%s" % (n, "  (← %s 실행의 호출 %d)" % DERIVED[n] if n in DERIVED else "") for n in fixture_names()))
        return 0
    names: list[str] = fixture_names() if a.all or not a.names else list(a.names)
    unknown = [n for n in names if n not in RECIPES and n not in DERIVED]
    if unknown:
        print("모르는 fixture: %s — --list 로 이름을 본다" % ", ".join(unknown), file=sys.stderr)
        return 2
    runs: list[str] = []
    for n in names:
        source = DERIVED[n][0] if n in DERIVED else n
        if source not in runs:
            runs.append(source)
    at: float = now() if a.at is None else a.at
    out: Path = a.out
    print("at=%.3f (%s) → %s" % (at, fmt_rfc3339(at), out))
    failed = 0
    for name in runs:
        try:
            paths = record(name, RECIPES[name], at, out)
        except (ObsError, Unreproducible, ValueError) as e:
            print("  ✗ %s — %s" % (name, e))
            failed += 1
            continue
        for path in paths:
            for c in read_fixture(path).calls:
                print("  ✓ %s — %s %s?%s [%d]" % (path.name, c.request.backend, c.request.path, _clip(c.request.query_string), c.response.status))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
