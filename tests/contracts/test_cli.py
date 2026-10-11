"""CLI 어댑터의 약속 — 질의 없음 · ID 없음 거부, 백엔드 오류 상태, 빈 검색의 힌트, 출력 절제(emit), --help 의 물리 비노출."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from obs.adapters import cli
from obs.core import JSON, Client, Evidence, Query, Lines, Response, Window
from tests.conftest import AT, MINUTE, ClientFactory, _no_physical, _vector


# ── 거부 · 오류 ──────────────────────────────────────────────────────────────────

def test_promql_requires_query(make_client: ClientFactory) -> None:
    """질의 없는 즉시 조회는 *_run 이 질의 전에 ObsError 로 거부한다 — argparse 가 아니라서 MCP 경로에서도 같은 검사가 산다."""
    client, t = make_client()
    with pytest.raises(cli.ObsError, match="PromQL 이 필요하다"):
        cli.promql_run(client, cli.PromqlArgs())
    assert t.calls == []


def test_promql_backend_error_status(make_client: ClientFactory) -> None:
    """HTTP 200 이어도 본문이 `status: error` 면 논리 이름 · errorType · 메시지로 ObsError 다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, {"status": "error", "errorType": "bad_data", "error": "boom"})})
    with pytest.raises(cli.ObsError) as ei:
        cli.promql_run(client, cli.PromqlArgs(query="up"))
    assert str(ei.value).startswith("metrics 백엔드 질의 오류(bad_data): boom")


def test_traceql_search_empty_hints_wider_window(make_client: ClientFactory) -> None:
    """search 결과가 비면 창을 넓히라는 힌트 한 줄을 낸다."""
    client, _ = make_client({("traces", "api/search"): (200, {"traces": []})})
    ev, _, _ = cli.traceql_run(client, cli.TraceqlArgs(cmd="search"))
    assert ev.value.lines == ["(결과 없음 — 트레이스는 드물다. 창을 넓혀 다시: --since 24h · 7d)"]


def test_traceql_trace_requires_id(make_client: ClientFactory) -> None:
    """trace 명령에 ID 가 없으면 질의 전에 ObsError 다."""
    client, t = make_client()
    with pytest.raises(cli.ObsError, match="traceID 가 필요하다"):
        cli.traceql_run(client, cli.TraceqlArgs(cmd="trace"))
    assert t.calls == []


# ── 출력 절제 ────────────────────────────────────────────────────────────────────

def test_emit_truncates_and_spills(tmp_path: Path) -> None:
    """emit 은 max_lines 를 넘으면 앞부분만 찍고 "N줄 더 있음" 과 호출 디렉터리를 안내하며, raw.json 과 summary.txt 에는 전체가 남는다."""
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    out: list[str] = []
    lines = ["l%d" % i for i in range(10)]
    res = cli.emit(lines, {"full": True}, "k", max_lines=3, env=env, out=out.append)
    assert res.lines == ["l0", "l1", "l2"]
    assert res.cut == 7
    assert res.ref.startswith("tmp/obs/")
    assert res.ref.endswith("-k")
    assert out[-1] == "… 7줄 더 있음 (총 10줄). 원본·요약: %s/ (raw.json · summary.txt)" % res.ref
    assert json.loads((tmp_path / res.ref / "raw.json").read_text()) == {"full": True}
    assert (tmp_path / res.ref / "summary.txt").read_text().splitlines() == lines


def test_emit_quiet_writes_files_prints_only_path(tmp_path: Path) -> None:
    """quiet 이면 stdout 에 요약 없이 경로만 찍고 파일은 보통 때와 같다."""
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    out: list[str] = []
    res = cli.emit(["a", "b"], {"x": 1}, "k", max_lines=3, quiet=True, env=env, out=out.append)
    assert res.lines == []
    assert out == ["원본·요약: %s/ (raw.json · summary.txt)" % res.ref]
    assert (tmp_path / res.ref / "summary.txt").read_text() == "a\nb\n"


def test_emit_always_writes_even_when_fits(tmp_path: Path) -> None:
    """상한 안에 들어가도 호출 디렉터리는 만들고 stdout 은 요약 뒤에 경로 한 줄이다."""
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    out: list[str] = []
    res = cli.emit(["a", "b"], {"x": 1}, "k", max_lines=3, env=env, out=out.append)
    assert res.cut == 0
    assert out == ["a", "b", "원본·요약: %s/ (raw.json · summary.txt)" % res.ref]
    assert sorted(q.name for q in (tmp_path / res.ref).iterdir()) == ["raw.json", "summary.txt"]


def test_emit_writes_responses_as_items_with_five_keys(tmp_path: Path) -> None:
    """Response 목록은 raw.json 에 같은 순서의 목록으로 쓰이고 항목마다 backend · path · params · status · body 키가 있다 — params 가 비어도."""
    # given — 하나는 파라미터 없이 보낸 호출
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    raw = [Response(backend="metrics", path="api/v1/query", status=200, body={"a": 1}, params={"query": "up"}),
           Response(backend="traces", path="api/traces/abc", status=200, body={"b": 2})]
    # when
    res = cli.emit(["x"], raw, "k", max_lines=3, env=env, out=lambda _: None)
    # then
    items = json.loads((tmp_path / res.ref / "raw.json").read_text())
    assert [(i["backend"], i["path"]) for i in items] == [(r.backend, r.path) for r in raw]
    for item in items:
        assert set(item) == {"backend", "path", "params", "status", "body"}


# ── 인터페이스 — 물리 인자가 없다 ────────────────────────────────────────────────

HELP_SCREENS: tuple[tuple[Callable[[list[str]], None], list[str]], ...] = (
    (cli.promql_main, ["--help"]), (cli.promql_main, ["summarize", "--help"]), (cli.promql_main, ["compare", "--help"]),
    (cli.promql_main, ["top", "--help"]), (cli.logql_main, ["--help"]), (cli.traceql_main, ["--help"]),
    (cli.logql_main, ["histogram", "--help"]), (cli.logql_main, ["templates", "--help"]), (cli.logql_main, ["around", "--help"]),
    (cli.logql_main, ["first-seen", "--help"]),
    (cli.traceql_main, ["exemplars", "--help"]), (cli.traceql_main, ["critical-path", "--help"]), (cli.traceql_main, ["service-graph", "--help"]),
    (cli.traceql_main, ["diff", "--help"]),
)


@pytest.mark.parametrize("main,argv", HELP_SCREENS, ids=["promql", "promql-summarize", "promql-compare", "promql-top", "logql", "traceql",
                                                         "logql-histogram", "logql-templates", "logql-around", "logql-first-seen",
                                                         "traceql-exemplars", "traceql-critical-path", "traceql-service-graph", "traceql-diff"])
def test_help_has_no_physical_flags_or_strings(main: Callable[[list[str]], None], argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """모든 --help 화면에 --uid · --url · --token · --host 가 없고 물리 문자열도 없다."""
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 0
    text = capsys.readouterr().out
    for flag in ("--uid", "--url", "--token", "--host"):
        assert flag not in text
    _no_physical(text)


def test_traceql_help_lists_traces_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    """상위 traceql --help 에 트레이스 환원 서브커맨드 넷의 이름이 전부 있다."""
    # given — 상위 화면
    argv = ["--help"]
    # when
    with pytest.raises(SystemExit) as ei:
        cli.traceql_main(argv)
    # then
    assert ei.value.code == 0
    text = capsys.readouterr().out
    for name in cli.TRACES_SUBCOMMANDS:
        assert name in text


def test_logql_help_lists_logs_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    """상위 logql --help 에 로그 환원 서브커맨드 넷의 이름이 전부 있다 — 상위 화면에서 서브커맨드를 안다."""
    # given — 상위 화면
    argv = ["--help"]
    # when
    with pytest.raises(SystemExit) as ei:
        cli.logql_main(argv)
    # then
    assert ei.value.code == 0
    text = capsys.readouterr().out
    for name in cli.LOGS_SUBCOMMANDS:
        assert name in text


# ── 파서 — 손으로 만든 형태 ───────────────────────────────────────────────────────

def test_parse_metric_scalar_goes_to_scalar_field() -> None:
    """scalar 응답은 scalar 필드로 옮겨지고 한 줄로 렌더링된다."""
    scalar = cli.parse_metric({"data": {"resultType": "scalar", "result": [1.0, "42"]}})
    assert scalar.scalar == "42"
    assert cli.instant_lines(scalar) == ["42"]


def test_parse_metric_broken_result_is_empty() -> None:
    """형태가 깨진 result 는 예외가 아니라 빈 시계열 목록이다."""
    assert cli.parse_metric({"data": {"resultType": "vector", "result": "garbage"}}).series == []


@pytest.mark.parametrize("argv,expected", [
    (["histogram", '{a="b"}', "--by", "app", "--since", "2h"], {"cmd": "histogram", "selector": '{a="b"}', "by": "app", "since": "2h"}),
    (["first-seen", '{a="b"}', "<_> x", "--since", "2h"], {"cmd": "first-seen", "selector": '{a="b"}', "template": "<_> x", "since": "2h"}),
], ids=["histogram", "first-seen"])
def test_logql_main_routes_subcommand_to_logs_run(argv: list[str], expected: dict[str, str], monkeypatch: pytest.MonkeyPatch,
                                                  make_client: ClientFactory, tmp_path: Path) -> None:
    """logql.py 의 첫 인자가 서브커맨드면 logs_run 으로 간다 — argv 의 cmd · selector · 옵션(by · since) · 위치 인자(template) 가 LogsArgs 로 들어간다."""
    # given — logs_run 대역이 받은 Args 를 기록한다
    seen: list[cli.LogsArgs] = []

    def fake_run(client: Client, a: cli.LogsArgs, at: float | None = None) -> cli.RunResult:
        seen.append(a)
        return Evidence(backend="logs", query=a.selector, window=Window(AT - MINUTE, AT), value=Lines(["x"])), [], "logql-histogram"

    client, _ = make_client()
    monkeypatch.setattr(cli, "logs_run", fake_run)
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    # when
    cli.logql_main(argv)
    # then
    assert len(seen) == 1
    for name, value in expected.items():
        assert getattr(seen[0], name) == value


@pytest.mark.parametrize("argv,expected", [
    (["exemplars", "--q", 'resource.service.name = "a"', "--k", "2", "--quantile", "0.9", "--baseline-range", "3d"],
     cli.TracesArgs(cmd="exemplars", q='resource.service.name = "a"', k=2, quantile=0.9, baseline_range="3d")),
    (["critical-path", "abc"], cli.TracesArgs(cmd="critical-path", trace_id="abc")),
    (["service-graph", "--quantile", "0.9", "--baseline-range", "3d"], cli.TracesArgs(cmd="service-graph", quantile=0.9, baseline_range="3d")),
    (["diff", "s1", "n1"], cli.TracesArgs(cmd="diff", slow_id="s1", normal_id="n1")),
], ids=["exemplars", "critical-path", "service-graph", "diff"])
def test_traceql_main_routes_subcommand_to_traces_run(argv: list[str], expected: cli.TracesArgs, monkeypatch: pytest.MonkeyPatch,
                                                      make_client: ClientFactory, tmp_path: Path) -> None:
    """traceql.py 의 첫 인자가 서브커맨드면 traces_run 으로 간다 — cmd · 위치 인자(traceID) · 옵션(k · quantile · baseline-range) 이 TracesArgs 로 들어가고 나머지 필드는 전부 기본값이다."""
    # given — traces_run 대역이 받은 Args 를 기록한다
    seen: list[cli.TracesArgs] = []

    def fake_run(client: Client, a: cli.TracesArgs, at: float | None = None) -> cli.RunResult:
        seen.append(a)
        return Evidence(backend="traces", query=a.cmd, window=Window(AT - MINUTE, AT), value=Lines(["x"])), [], "traceql-" + a.cmd

    client, _ = make_client()
    monkeypatch.setattr(cli, "traces_run", fake_run)
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    # when
    cli.traceql_main(argv)
    # then
    assert len(seen) == 1
    assert seen[0] == expected


def test_traceql_main_still_routes_legacy_commands_to_traceql_run(monkeypatch: pytest.MonkeyPatch, make_client: ClientFactory, tmp_path: Path) -> None:
    """서브커맨드가 아닌 기존 명령(tags)은 그대로 traceql_run 이 받는다."""
    # given — traceql_run 대역
    seen: list[cli.TraceqlArgs] = []

    def fake_run(client: Client, a: cli.TraceqlArgs, at: float | None = None) -> cli.RunResult:
        seen.append(a)
        return Evidence(backend="traces", query="tags()", window=Window(AT - MINUTE, AT), value=Lines(["x"])), [], "traceql-tags"

    client, _ = make_client()
    monkeypatch.setattr(cli, "traceql_run", fake_run)
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    # when
    cli.traceql_main(["tags"])
    # then
    assert [a.cmd for a in seen] == ["tags"]


def test_evidence_preamble_lists_every_query_with_its_backend() -> None:
    """queries 가 둘인 Evidence 의 summary.txt 머리에는 질의 줄이 둘이고 각각 `# 질의  <backend>  <query>` 꼴이다."""
    # given
    w = Window(AT - MINUTE, AT)
    ev = Evidence(backend="traces", query="Q-traces", window=w, value=Lines(["x"]), queries=[Query("metrics", "Q-metrics", w), Query("traces", "Q-traces", w)])
    # when
    lines = cli.evidence_preamble(ev)
    query_lines = [line for line in lines if line.startswith("# 질의")]
    # then
    assert query_lines == ["# 질의  metrics  Q-metrics", "# 질의  traces  Q-traces"]


def test_evidence_preamble_without_extra_queries_is_unchanged() -> None:
    """queries 가 비면 머리는 전과 같다 — `# 질의  <query>` 한 줄뿐이다."""
    # given
    ev = Evidence(backend="metrics", query="up", window=Window(AT - MINUTE, AT), value=Lines(["x"]))
    # when
    lines = cli.evidence_preamble(ev)
    # then
    assert lines[0] == "# 질의  up"
    assert sum(1 for line in lines if line.startswith("# 질의")) == 1


def test_promql_main_top_rejects_since() -> None:
    """top 은 창이 없으므로 --since 를 받지 않는다 — argparse 가 거부한다(exit 2)."""
    with pytest.raises(SystemExit) as ei:
        cli.promql_main(["top", "x", "--since", "1h"])
    assert ei.value.code == 2


# ── 실행의 약속 — 손 입력 ─────────────────────────────────────────────────────────

EMPTY_MATRIX: dict[str, JSON] = {"status": "success", "data": {"resultType": "matrix", "result": []}}
STREAMS: dict[str, JSON] = {"status": "success", "data": {"resultType": "streams", "result": [
    {"stream": {"namespace": "n", "pod": "p"}, "values": [["1700000000000000000", "x" * 100], ["1700000001000000000", "short"]]}]}}
SEARCH: dict[str, JSON] = {"traces": [{"traceID": "abc123", "rootServiceName": "svc", "rootTraceName": "op", "startTimeUnixNano": "1700000000000000000",
                                       "durationMs": 12}]}


def test_promql_range_window_and_auto_step(make_client: ClientFactory) -> None:
    """--range 는 창 길이를 계산하고 step 을 자동(range/60, 최소 15s)으로 붙여 query_range 를 부른다."""
    client, t = make_client({("metrics", "api/v1/query_range"): (200, EMPTY_MATRIX)})
    ev, _, kind = cli.promql_run(client, cli.PromqlArgs(query="sum(up)", range_="30m"), at=AT)
    assert kind == "promql-range"
    assert ev.window is not None
    assert ev.window.seconds == 30 * MINUTE
    assert "step=30s" in t.calls[0].url
    assert "start=%s" % (AT - 30 * MINUTE) in t.calls[0].url


def test_logql_width_clips_log_body(make_client: ClientFactory) -> None:
    """--width 는 로그 본문을 지정 폭에서 자른다."""
    client, _ = make_client({("logs", "loki/api/v1/query_range"): (200, STREAMS)})
    ev, _, _ = cli.logql_run(client, cli.LogqlArgs(query='{namespace="n"}', width=60), at=AT)
    assert all(len(l.split("  ", 2)[-1]) <= 60 for l in ev.value.lines[1:])


def test_logql_common_labels_go_to_the_header(make_client: ClientFactory) -> None:
    """스트림 출력의 첫 줄은 공통 레이블이고 그 뒤가 로그 줄이다 — 줄 수는 값 수 + 1."""
    client, _ = make_client({("logs", "loki/api/v1/query_range"): (200, STREAMS)})
    ev, _, _ = cli.logql_run(client, cli.LogqlArgs(query='{namespace="n"}'), at=AT)
    assert ev.value.lines[0].startswith("공통 레이블 {")
    assert len(ev.value.lines) == 3


def _lines_of(client: Client, backend: str) -> list[str]:
    if backend == "promql":
        return cli.promql_run(client, cli.PromqlArgs(query="up"), at=AT)[0].value.lines
    if backend == "logql":
        return cli.logql_run(client, cli.LogqlArgs(query='{namespace="n"}'), at=AT)[0].value.lines
    if backend == "traceql":
        return cli.traceql_run(client, cli.TraceqlArgs(cmd="search"), at=AT)[0].value.lines
    return cli.metrics_run(client, cli.MetricsArgs(cmd="summarize", query="up"), at=AT)[0].value.lines


@pytest.mark.parametrize("backend", ["promql", "logql", "traceql", "metrics"])
def test_run_outputs_carry_no_physical(make_client: ClientFactory, backend: str) -> None:
    """네 실행의 출력 줄에 물리 문자열(호스트 · 프록시 접두 · uid)이 없다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector(({"__name__": "up", "job": "j"}, "1"))),
                             ("logs", "loki/api/v1/query_range"): (200, STREAMS), ("traces", "api/search"): (200, SEARCH)})
    _no_physical("\n".join(_lines_of(client, backend)))


# ── 원본 — 단일 호출 분기마다 Response 하나 ───────────────────────────────────────

STRINGS: dict[str, JSON] = {"status": "success", "data": ["a"]}
LABEL_SETS: dict[str, JSON] = {"status": "success", "data": [{"a": "b"}]}

RUN_BRANCHES: list[tuple[str, Callable[[Client], cli.RunResult], str, str, dict[str, JSON]]] = [
    ("promql-labels", lambda c: cli.promql_run(c, cli.PromqlArgs(labels=True), at=AT), "metrics", "api/v1/labels", STRINGS),
    ("promql-label-values", lambda c: cli.promql_run(c, cli.PromqlArgs(label_values="job"), at=AT), "metrics", "api/v1/label/job/values", STRINGS),
    ("promql-series", lambda c: cli.promql_run(c, cli.PromqlArgs(series='{a="b"}'), at=AT), "metrics", "api/v1/series", LABEL_SETS),
    ("promql-range", lambda c: cli.promql_run(c, cli.PromqlArgs(query="up", range_="1h"), at=AT), "metrics", "api/v1/query_range", EMPTY_MATRIX),
    ("promql-instant", lambda c: cli.promql_run(c, cli.PromqlArgs(query="up"), at=AT), "metrics", "api/v1/query", _vector(({"job": "j"}, "1"))),
    ("logql-labels", lambda c: cli.logql_run(c, cli.LogqlArgs(labels=True), at=AT), "logs", "loki/api/v1/labels", STRINGS),
    ("logql-label-values", lambda c: cli.logql_run(c, cli.LogqlArgs(label_values="app"), at=AT), "logs", "loki/api/v1/label/app/values", STRINGS),
    ("logql-series", lambda c: cli.logql_run(c, cli.LogqlArgs(series='{a="b"}'), at=AT), "logs", "loki/api/v1/series", LABEL_SETS),
    ("logql-query", lambda c: cli.logql_run(c, cli.LogqlArgs(query='{namespace="n"}'), at=AT), "logs", "loki/api/v1/query_range", STREAMS),
    ("traceql-search", lambda c: cli.traceql_run(c, cli.TraceqlArgs(cmd="search"), at=AT), "traces", "api/search", SEARCH),
    ("traceql-trace", lambda c: cli.traceql_run(c, cli.TraceqlArgs(cmd="trace", arg="abc123"), at=AT), "traces", "api/traces/abc123", {"batches": []}),
    ("traceql-tags", lambda c: cli.traceql_run(c, cli.TraceqlArgs(cmd="tags"), at=AT), "traces", "api/v2/search/tags", {"scopes": []}),
    ("traceql-tag-values", lambda c: cli.traceql_run(c, cli.TraceqlArgs(cmd="tag-values", arg="service.name"), at=AT), "traces",
     "api/v2/search/tag/service.name/values", {"tagValues": []}),
]


@pytest.mark.parametrize("run,backend,path,body", [b[1:] for b in RUN_BRANCHES], ids=[b[0] for b in RUN_BRANCHES])
def test_single_call_run_raw_is_one_response(make_client: ClientFactory, run: Callable[[Client], cli.RunResult], backend: str, path: str,
                                             body: dict[str, JSON]) -> None:
    """단일 호출 분기의 원본은 그 호출 하나의 Response 를 담은 길이 1 목록이다 — 그 분기의 경로와 받은 응답이 실린다."""
    # given
    client, _ = make_client({(backend, path): (200, body)})
    # when
    _, raw, _ = run(client)
    # then
    assert isinstance(raw, list)
    assert len(raw) == 1
    assert isinstance(raw[0], Response)
    assert raw[0].path == path
    assert raw[0].body == body


# ── 값의 표기 — fmt_val · fmt_signed 의 경계 ─────────────────────────────────────

# ── trace 의 오류 원인 — 손으로 만든 Span ────────────────────────────────────────

def _span(status_message: str = "", attrs: dict[str, str] | None = None, error: bool = True) -> cli.Span:
    return cli.Span(service="svc", name="op", span_id="1", parent_id="", start_ns=0, end_ns=2_000_000,
                    status_code="STATUS_CODE_ERROR" if error else "", status_message=status_message, attrs=attrs or {})


def test_trace_lines_count_and_mark_error_spans() -> None:
    """오류 span 은 `오류 span: N` 으로 세어지고 ✗ 줄로 표시된다."""
    lines = cli.trace_lines("abc", [_span("boom"), _span(error=False)], top=5)
    assert lines[3] == "오류 span: 1"
    assert lines[4].startswith("  ✗ svc / op")
    assert "boom" in lines[4]


def test_error_reason_is_first_line_of_status_message_without_stack() -> None:
    """error_reason 은 status.message 의 첫 줄만 — 개행 · 탭 뒤의 스택은 뺀다."""
    assert cli.error_reason(_span("connection EOF\n\tStackTrace: a.go:1")) == "connection EOF"
    assert cli.error_reason(_span("  EOF\tStackTrace: a.go:1")) == "EOF"


def test_error_reason_falls_back_to_attrs() -> None:
    """status.message 가 비거나 스택뿐이면 exception.message → error.message → http.status_code 순으로 속성에서 찾는다."""
    assert cli.error_reason(_span("", {"exception.message": "boom"})) == "boom"
    assert cli.error_reason(_span("\n\tStackTrace: a.go:1", {"exception.message": "boom"})) == "boom"
    assert cli.error_reason(_span("", {"error.message": "e", "http.status_code": "500"})) == "e"
    assert cli.error_reason(_span("", {"http.status_code": "500"})) == "500"
    assert cli.error_reason(_span("")) == ""
