"""CLI 어댑터의 약속 — 질의 없음 · ID 없음 거부, 백엔드 오류 상태, 빈 검색의 힌트, 출력 절제(emit), --help 의 물리 비노출."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from obs.adapters import cli
from obs.core import JSON, Client
from tests.conftest import AT, MINUTE, ClientFactory, _no_physical, _vector


# ── 거부 · 오류 ──────────────────────────────────────────────────────────────────

def test_promql_requires_query(make_client: ClientFactory) -> None:
    """질의 없는 즉시 조회는 *_run 이 ObsError 로 거부한다 — argparse 가 아니라서 MCP 경로에서도 같은 검사가 산다."""
    client, _ = make_client()
    with pytest.raises(cli.ObsError):
        cli.promql_run(client, cli.PromqlArgs())


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
    """trace 명령에 ID 가 없으면 ObsError 다."""
    client, _ = make_client()
    with pytest.raises(cli.ObsError):
        cli.traceql_run(client, cli.TraceqlArgs(cmd="trace"))


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


# ── 인터페이스 — 물리 인자가 없다 ────────────────────────────────────────────────

HELP_SCREENS: tuple[tuple[Callable[[list[str]], None], list[str]], ...] = (
    (cli.promql_main, ["--help"]), (cli.promql_main, ["summarize", "--help"]), (cli.promql_main, ["compare", "--help"]),
    (cli.promql_main, ["top", "--help"]), (cli.logql_main, ["--help"]), (cli.traceql_main, ["--help"]),
)


@pytest.mark.parametrize("main,argv", HELP_SCREENS, ids=["promql", "promql-summarize", "promql-compare", "promql-top", "logql", "traceql"])
def test_help_has_no_physical_flags_or_strings(main: Callable[[list[str]], None], argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """모든 --help 화면에 --uid · --url · --token · --host 가 없고 물리 문자열도 없다."""
    with pytest.raises(SystemExit) as ei:
        main(argv)
    assert ei.value.code == 0
    text = capsys.readouterr().out
    for flag in ("--uid", "--url", "--token", "--host"):
        assert flag not in text
    _no_physical(text)


# ── 파서 — 손으로 만든 형태 ───────────────────────────────────────────────────────

def test_parse_metric_scalar_goes_to_scalar_field() -> None:
    """scalar 응답은 scalar 필드로 옮겨지고 한 줄로 렌더링된다."""
    scalar = cli.parse_metric({"data": {"resultType": "scalar", "result": [1.0, "42"]}})
    assert scalar.scalar == "42"
    assert cli.instant_lines(scalar) == ["42"]


def test_parse_metric_broken_result_is_empty() -> None:
    """형태가 깨진 result 는 예외가 아니라 빈 시계열 목록이다."""
    assert cli.parse_metric({"data": {"resultType": "vector", "result": "garbage"}}).series == []


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
