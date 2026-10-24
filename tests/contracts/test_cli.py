"""CLI 어댑터의 약속 — 질의 없음 · ID 없음 거부, 백엔드 오류 상태, 빈 검색의 힌트, 출력 경로(spill_call · print_summary · finish), --help 의 물리 비노출."""
from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Callable

import pytest

from obs.adapters import cli
from obs.core import JSON, BudgetExceeded, Client, Config, Evidence, ObsError, Response, Window, json_list
from tests.conftest import AT, HOUR, MINUTE, ClientFactory, MainFn, MainRunner, RouteKey, _no_physical, _vector, no_client
from tests.record import strip_physical


# ── 거부 · 오류 ──────────────────────────────────────────────────────────────────

def test_promql_requires_query(make_client: ClientFactory) -> None:
    """질의 없는 즉시 조회는 *_run 이 질의 전에 ObsError 로 거부한다 — argparse 가 아니라서 MCP 경로에서도 같은 검사가 산다."""
    client, t = make_client()
    with pytest.raises(cli.ObsError, match="PromQL 이 필요하다"):
        cli.promql_run(client, cli.PromqlArgs(), at=AT)
    assert t.calls == []


def test_promql_backend_error_status(make_client: ClientFactory) -> None:
    """HTTP 200 이어도 본문이 `status: error` 면 논리 이름 · errorType · 메시지로 ObsError 다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, {"status": "error", "errorType": "bad_data", "error": "boom"})})
    with pytest.raises(cli.ObsError) as ei:
        cli.promql_run(client, cli.PromqlArgs(query="up"), at=AT)
    assert str(ei.value).startswith("metrics 백엔드 질의 오류(bad_data): boom")


def test_rejected_response_stays_in_log(make_client: ClientFactory) -> None:
    """HTTP 200 인데 본문이 `status: error` 라 연산이 거부해도 그 응답은 client.log 에 남는다 — status 200 · body 가 그 거부된 본문."""
    # given — 200 인데 status: error
    body: dict[str, JSON] = {"status": "error", "errorType": "bad_data", "error": "boom"}
    client, _ = make_client({("metrics", "api/v1/query"): (200, body)})
    # when
    with pytest.raises(cli.ObsError):
        cli.promql_run(client, cli.PromqlArgs(query="up"), at=AT)
    # then
    assert len(client.log) == 1
    assert client.log[0].status == 200
    assert client.log[0].body == body


def test_traceql_trace_requires_id(make_client: ClientFactory) -> None:
    """trace 명령에 ID 가 없으면 질의 전에 ObsError 다."""
    client, t = make_client()
    with pytest.raises(cli.ObsError, match="traceID 가 필요하다"):
        cli.traceql_run(client, cli.TraceqlArgs(cmd="trace"), at=AT)
    assert t.calls == []


# ── 출력 경로 — 파일은 spill_call · stdout 은 print_summary ─────────────────────────────

SPILL_PAYLOAD: list[Response] = [
    Response(backend="metrics", path="api/v1/query", status=200, body={"a": 1}, params={"query": "up"}, size=8),
    Response(backend="traces", path="api/search", status=None, body=None, error="traces 백엔드에 닿지 못했다"),
]
"""spill_call 의 payload 표본 — 성공 하나 · 전송 실패 하나(섞임)."""
SPILL_SUMMARY: list[str] = ["# 창", "", "a 1"]
"""spill_call 의 summary 표본 — 셋 줄."""


def test_spill_call_names_directory_by_at(tmp_path: Path) -> None:
    """spill_call 은 <base>/tmp/obs/ 아래 디렉터리 하나를 만들고 그 이름의 시각은 인자 at 의 UTC 표기다 — 지금 시각이 아니다."""
    # given — base 는 tmp · at 은 AT
    # when
    cli.spill_call("k", SPILL_PAYLOAD, SPILL_SUMMARY, base=tmp_path, at=AT)
    # then
    dirs = sorted((tmp_path / "tmp" / "obs").iterdir())
    assert len(dirs) == 1
    assert dirs[0].name == "19700102T034640.000000Z-k"
    assert dirs[0].is_dir()


def test_spill_call_writes_payload_as_raw_json(tmp_path: Path) -> None:
    """raw.json 은 payload 와 같은 순서의 목록이고 원소마다 키 집합이 backend · path · params · status · body · error · size · truncated 로 같다 — 성공 · 실패가 섞여도."""
    # given — 성공 하나 · 전송 실패 하나
    # when
    cli.spill_call("k", SPILL_PAYLOAD, SPILL_SUMMARY, base=tmp_path, at=AT)
    # then
    items = json.loads((_only_dir(tmp_path) / "raw.json").read_text())
    assert [(i["backend"], i["path"]) for i in items] == [(r.backend, r.path) for r in SPILL_PAYLOAD]
    for item in items:
        assert set(item) == {"backend", "path", "params", "status", "body", "error", "size", "truncated"}
    assert [i["status"] for i in items] == [200, None]
    assert [i["error"] for i in items] == [None, "traces 백엔드에 닿지 못했다"]


def test_spill_call_writes_summary_lines(tmp_path: Path) -> None:
    """summary.txt 의 줄은 인자 summary 그대로다 — 순서와 수(셋)."""
    # given — summary 셋 줄(빈 줄 포함)
    # when
    cli.spill_call("k", SPILL_PAYLOAD, SPILL_SUMMARY, base=tmp_path, at=AT)
    # then
    assert (_only_dir(tmp_path) / "summary.txt").read_text(encoding="utf-8").splitlines() == SPILL_SUMMARY


def test_spill_call_returns_ref_and_where(tmp_path: Path) -> None:
    """반환 CallDir 의 ref 는 그 디렉터리의 base 기준 상대 경로이고, where 는 "<ref>/ (raw.json · summary.txt)" 다."""
    # given — base 는 tmp
    # when
    d = cli.spill_call("k", SPILL_PAYLOAD, SPILL_SUMMARY, base=tmp_path, at=AT)
    # then
    assert d.ref == str(_only_dir(tmp_path).relative_to(tmp_path))
    assert d.ref.startswith("tmp/obs/")
    assert d.ref.endswith("-k")
    assert d.where == "%s/ (raw.json · summary.txt)" % d.ref


WHERE = "tmp/obs/19700102T034640.000000Z-k/ (raw.json · summary.txt)"
"""print_summary 가 받아 그대로 찍어야 하는 안내 문구 표본."""


def test_print_summary_truncates_and_hints_rest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_lines 를 넘으면 앞 max_lines 줄만 찍고 마지막 줄은 "… N줄 더 있음 (총 M줄). 원본·요약: <where>" 이며, Printed 는 찍은 줄과 안 찍은 수다."""
    # given — 열 줄 · 상한 3 · out 은 목록 · cwd 는 tmp(파일을 만들면 보이게)
    monkeypatch.chdir(tmp_path)
    out: list[str] = []
    lines = ["l%d" % i for i in range(10)]
    # when
    res = cli.print_summary(lines, cli.OutputArgs(max_lines=3), WHERE, out=out.append)
    # then
    assert out[:3] == ["l0", "l1", "l2"]
    assert out[3] == "… 7줄 더 있음 (총 10줄). 원본·요약: %s" % WHERE
    assert len(out) == 4
    assert res.lines == ["l0", "l1", "l2"]
    assert res.cut == 7
    assert not (tmp_path / "tmp").exists()


def test_print_summary_files_only_prints_only_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """files_only 면 요약을 한 줄도 찍지 않고 "원본·요약: <where>" 한 줄뿐이며, Printed.lines 는 비고 cut 은 줄 수 전부다."""
    # given — 두 줄 · files_only
    monkeypatch.chdir(tmp_path)
    out: list[str] = []
    # when
    res = cli.print_summary(["a", "b"], cli.OutputArgs(files_only=True), WHERE, out=out.append)
    # then
    assert out == ["원본·요약: %s" % WHERE]
    assert res.lines == []
    assert res.cut == 2
    assert not (tmp_path / "tmp").exists()


@pytest.mark.parametrize("lines", [["a", "b"], ["a", "b", "c"]], ids=["under", "exact"])
def test_print_summary_fits_prints_all_then_path(lines: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """상한 안(아래 · 딱 맞음)이면 줄 전부 뒤에 "원본·요약: <where>" 한 줄이고 "더 있음" 안내가 없으며 cut 은 0 이다."""
    # given — 상한 3 에 두 줄 / 셋 줄
    monkeypatch.chdir(tmp_path)
    out: list[str] = []
    # when
    res = cli.print_summary(lines, cli.OutputArgs(max_lines=3), WHERE, out=out.append)
    # then
    assert out == [*lines, "원본·요약: %s" % WHERE]
    assert not [line for line in out if "더 있음" in line]
    assert res.lines == lines
    assert res.cut == 0


# ── finish — 결정 넷과 배선: 성공은 spill_call → print_summary, 실패도 호출 디렉터리 ───────────────────────────────────

FINISH_ROUTES: dict[RouteKey, tuple[int, object]] = {("metrics", "api/v1/labels"): (200, {"data": []}), ("metrics", "api/v1/series"): (200, {"data": []})}


def _run_after(client: Client, calls: int, raise_: BaseException | None = None, lines: list[str] | None = None) -> Callable[[], cli.RunResult[list[str]]]:
    """호출 calls 개(labels · series 순)를 보낸 뒤 raise_ 를 올리거나 요약 하나(lines 를 그대로 렌더 — 기본 ["x"])를 돌려주는 run 대역."""
    def run() -> cli.RunResult[list[str]]:
        for path in ("api/v1/labels", "api/v1/series")[:calls]:
            client.get("metrics", path)
        if raise_ is not None:
            raise raise_
        return cli.RunResult(Evidence(lines if lines is not None else ["x"]), "k", Window(AT - MINUTE, AT), lambda v, w: v)
    return run


def _only_dir(root: Path) -> Path:
    """<root>/tmp/obs 의 호출 디렉터리 하나."""
    dirs = sorted((root / "tmp" / "obs").iterdir())
    assert len(dirs) == 1, "호출 디렉터리가 하나가 아니다: %r" % dirs
    return dirs[0]


def test_finish_writes_log_as_raw_json(make_client: ClientFactory, tmp_path: Path) -> None:
    """성공하면 raw.json 의 원소 수가 client.log 와 같고, 디렉터리 이름은 run 의 kind 이며(-failed 아님), out 의 마지막 줄이 그 경로다."""
    # given — 호출 둘을 보내고 성공하는 run
    client, _ = make_client(FINISH_ROUTES)
    out: list[str] = []
    # when
    cli.finish(client, _run_after(client, 2), "k-failed", cli.OutputArgs(), base=tmp_path, at=AT, out=out.append)
    # then
    assert len(client.log) == 2
    d = _only_dir(tmp_path)
    assert d.name.endswith("-k")
    assert len(json.loads((d / "raw.json").read_text())) == 2
    assert d.name in out[-1]


def test_finish_names_call_directory_in_obs_error(make_client: ClientFactory, tmp_path: Path) -> None:
    """run 이 ObsError(하위 종류 포함)를 올리면 같은 종류로 다시 올라오고 — 문구가 원래 문구로 시작하며 끝에 -failed 디렉터리 경로와 raw.json 을 말한다."""
    # given — 호출 둘 뒤 BudgetExceeded
    client, _ = make_client(FINISH_ROUTES)
    # when
    with pytest.raises(BudgetExceeded) as ei:
        cli.finish(client, _run_after(client, 2, BudgetExceeded("예산 초과 — 질의 20회 상한")), "promql-failed", cli.OutputArgs(), base=tmp_path, at=AT, out=lambda _: None)
    # then
    msg = str(ei.value)
    assert msg.startswith("예산 초과 — 질의 20회 상한")
    d = _only_dir(tmp_path)
    assert d.name.endswith("-promql-failed")
    assert d.name in msg
    assert "%s/" % d.name in msg
    assert "raw.json" in msg


def test_finish_spills_log_on_obs_error(make_client: ClientFactory, tmp_path: Path) -> None:
    """run 이 ObsError 를 올리면 그 디렉터리의 raw.json 은 client.log 전부(원소 2) · summary.txt 는 원래 오류 문구이고 out 으로는 아무것도 찍지 않는다."""
    # given — 호출 둘 뒤 ObsError
    client, _ = make_client(FINISH_ROUTES)
    out: list[str] = []
    # when
    with pytest.raises(ObsError):
        cli.finish(client, _run_after(client, 2, ObsError("metrics 백엔드가 질의를 거부했다(400)")), "promql-failed", cli.OutputArgs(), base=tmp_path, at=AT, out=out.append)
    # then
    d = _only_dir(tmp_path)
    assert len(json.loads((d / "raw.json").read_text())) == 2
    assert "metrics 백엔드가 질의를 거부했다(400)" in (d / "summary.txt").read_text()
    assert out == []


def test_finish_spills_log_and_reraises_other_exceptions(make_client: ClientFactory, tmp_path: Path) -> None:
    """run 이 ObsError 가 아닌 예외(버그)를 올리면 그대로 올라오고, err 로 -failed 경로 한 줄을 찍으며, 디렉터리의 raw.json 에 보낸 호출(원소 1)이 있다."""
    # given — 호출 하나 뒤 ValueError
    client, _ = make_client(FINISH_ROUTES)
    err: list[str] = []
    bug = ValueError("bug")
    # when
    with pytest.raises(ValueError) as ei:
        cli.finish(client, _run_after(client, 1, bug), "logql-failed", cli.OutputArgs(), base=tmp_path, at=AT, out=lambda _: None, err=err.append)
    # then
    assert ei.value is bug
    d = _only_dir(tmp_path)
    assert d.name.endswith("-logql-failed")
    assert len(err) == 1
    assert d.name in err[0]
    assert "%s/" % d.name in err[0]
    assert len(json.loads((d / "raw.json").read_text())) == 1


def test_finish_without_sent_calls_leaves_no_directory(make_client: ClientFactory, tmp_path: Path) -> None:
    """보낸 호출이 없는 실패(보내기 전 거부)는 디렉터리를 만들지 않고 ObsError 문구가 그대로다 — 경로가 붙지 않는다."""
    # given — 호출 없이 ObsError
    client, _ = make_client()
    # when
    with pytest.raises(ObsError) as ei:
        cli.finish(client, _run_after(client, 0, ObsError("PromQL 이 필요하다")), "promql-failed", cli.OutputArgs(), base=tmp_path, at=AT, out=lambda _: None)
    # then
    assert str(ei.value) == "PromQL 이 필요하다"
    assert not (tmp_path / "tmp" / "obs").exists()


def test_finish_without_sent_calls_reraises_other_exceptions_silently(make_client: ClientFactory, tmp_path: Path) -> None:
    """보낸 호출이 없는 비-ObsError 예외(버그)는 같은 객체로 그대로 올라오고, err 로 아무것도 찍지 않으며, 디렉터리도 만들지 않는다."""
    # given — 호출 없이 ValueError
    client, _ = make_client()
    err: list[str] = []
    bug = ValueError("bug")
    # when
    with pytest.raises(ValueError) as ei:
        cli.finish(client, _run_after(client, 0, bug), "promql-failed", cli.OutputArgs(), base=tmp_path, at=AT, out=lambda _: None, err=err.append)
    # then
    assert ei.value is bug
    assert err == []
    assert not (tmp_path / "tmp" / "obs").exists()


THREE_LINES: list[str] = ["a 1", "b 2", "c 3"]
"""finish 의 OutputArgs 전달 · summary 전체를 보는 run 의 요약 — 셋 줄."""


@pytest.mark.parametrize("output,shown,tail", [(cli.OutputArgs(max_lines=1), 1, "… 2줄 더 있음"), (cli.OutputArgs(files_only=True), 0, "원본·요약:")],
                         ids=["max-lines", "files-only"])
def test_finish_passes_output_to_print_summary(output: cli.OutputArgs, shown: int, tail: str, make_client: ClientFactory, tmp_path: Path) -> None:
    """finish 는 받은 OutputArgs 를 그대로 stdout 절제에 쓴다 — max_lines=1 이면 요약 1줄 + "… 2줄 더 있음" 안내, files_only 면 경로 한 줄뿐."""
    # given — 요약 셋 줄을 내는 성공 run
    client, _ = make_client(FINISH_ROUTES)
    out: list[str] = []
    # when
    cli.finish(client, _run_after(client, 1, lines=THREE_LINES), "k-failed", output, base=tmp_path, at=AT, out=out.append)
    # then
    assert len(out) == shown + 1
    assert out[:shown] == THREE_LINES[:shown]
    assert out[-1].startswith(tail)


def test_finish_summary_is_preamble_then_lines(make_client: ClientFactory, tmp_path: Path) -> None:
    """성공한 run 의 summary.txt 는 run 의 창으로 만든 window_preamble 뒤에 요약 줄 전부이고, stdout 에는 `# ` 머리 줄이 없다."""
    # given — 요약 두 줄을 내는 성공 run(창은 _run_after 의 것)
    client, _ = make_client(FINISH_ROUTES)
    out: list[str] = []
    run = _run_after(client, 1, lines=["a 1", "b 2"])
    # when
    cli.finish(client, run, "k-failed", cli.OutputArgs(), base=tmp_path, at=AT, out=out.append)
    # then
    assert _summary_text(tmp_path).splitlines() == [*cli.window_preamble(Window(AT - MINUTE, AT)), "a 1", "b 2"]
    assert out[:2] == ["a 1", "b 2"]
    assert not [line for line in out if line.startswith("# ")]


@pytest.mark.parametrize("output", [cli.OutputArgs(max_lines=1), cli.OutputArgs(files_only=True)], ids=["max-lines", "files-only"])
def test_finish_summary_keeps_all_lines(output: cli.OutputArgs, make_client: ClientFactory, tmp_path: Path) -> None:
    """stdout 을 절제해도(max_lines=1 · files_only) summary.txt 에는 창 머리 뒤 요약 셋 줄 전부가 있다."""
    # given — 요약 셋 줄을 내는 성공 run
    client, _ = make_client(FINISH_ROUTES)
    # when
    cli.finish(client, _run_after(client, 1, lines=THREE_LINES), "k-failed", output, base=tmp_path, at=AT, out=lambda _: None)
    # then
    summary = _summary_text(tmp_path).splitlines()
    assert summary[0].startswith("# 창")
    assert summary[-3:] == THREE_LINES


# ── 인터페이스 — 물리 인자가 없다 ────────────────────────────────────────────────

HELP_SCREENS: tuple[tuple[MainFn, list[str]], ...] = (
    (cli.promql_main, ["--help"]), (cli.promql_main, ["summarize", "--help"]), (cli.promql_main, ["compare", "--help"]),
    (cli.promql_main, ["top", "--help"]), (cli.logql_main, ["--help"]), (cli.traceql_main, ["--help"]),
    (cli.logql_main, ["histogram", "--help"]), (cli.logql_main, ["templates", "--help"]), (cli.logql_main, ["around", "--help"]),
    (cli.logql_main, ["first-seen", "--help"]),
    (cli.traceql_main, ["exemplars", "--help"]), (cli.traceql_main, ["critical-path", "--help"]), (cli.traceql_main, ["service-graph", "--help"]),
    (cli.traceql_main, ["diff", "--help"]), (cli.promql_main, ["red", "--help"]),
)


@pytest.mark.parametrize("main,argv", HELP_SCREENS, ids=["promql", "promql-summarize", "promql-compare", "promql-top", "logql", "traceql",
                                                         "logql-histogram", "logql-templates", "logql-around", "logql-first-seen",
                                                         "traceql-exemplars", "traceql-critical-path", "traceql-service-graph", "traceql-diff",
                                                         "promql-red"])
def test_help_has_no_physical_flags_or_strings(main: MainFn, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """모든 --help 화면에 --uid · --url · --token · --host 가 없고 물리 문자열도 없다 — 토큰 · 설정 없는 env 로, Client 는 만들어지지 않는다."""
    with pytest.raises(SystemExit) as ei:
        main(argv, {}, AT, no_client)
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
        cli.traceql_main(argv, {}, AT, no_client)
    # then
    assert ei.value.code == 0
    text = capsys.readouterr().out
    for name in cli.TRACES_SUBCOMMANDS:
        assert name in text


def test_promql_help_lists_metrics_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    """상위 promql --help 에 메트릭 환원 서브커맨드 넷의 이름이 전부 있다 — 상위 화면에서 서브커맨드를 안다."""
    # given — 상위 화면
    argv = ["--help"]
    # when
    with pytest.raises(SystemExit) as ei:
        cli.promql_main(argv, {}, AT, no_client)
    # then
    assert ei.value.code == 0
    text = capsys.readouterr().out
    for name in cli.METRICS_SUBCOMMANDS:
        assert name in text
        assert re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(name), text)


def test_logql_help_lists_logs_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    """상위 logql --help 에 로그 환원 서브커맨드 넷의 이름이 전부 있다 — 상위 화면에서 서브커맨드를 안다."""
    # given — 상위 화면
    argv = ["--help"]
    # when
    with pytest.raises(SystemExit) as ei:
        cli.logql_main(argv, {}, AT, no_client)
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


def test_window_preamble_has_window_line_only() -> None:
    """기준선 없는 창의 머리는 창의 label · start · end 를 담은 `# 창` 줄 하나와 빈 줄이다 — `# 질의` · `# 기준선` 줄이 없다."""
    # given
    w = Window(AT - MINUTE, AT)
    # when
    lines = cli.window_preamble(w)
    # then
    window_lines = [line for line in lines if line.startswith("# 창")]
    assert len(window_lines) == 1
    assert w.label() in window_lines[0]
    assert "start=%.3f end=%.3f" % (w.start, w.end) in window_lines[0]
    assert not [line for line in lines if line.startswith("# 질의")]
    assert not [line for line in lines if line.startswith("# 기준선")]
    assert lines[-1] == ""
    assert lines == [window_lines[0], ""]


def test_window_preamble_adds_baseline_line() -> None:
    """기준선이 달린 창의 머리에는 `# 창` 바로 다음에 기준선 창의 label · start · end 를 담은 `# 기준선` 줄 하나가 있다."""
    # given
    w = Window(AT - HOUR, AT).with_baseline("1d")
    assert w.baseline is not None
    b = w.baseline
    # when
    lines = cli.window_preamble(w)
    # then
    baseline_idx = [i for i, line in enumerate(lines) if line.startswith("# 기준선")]
    assert len(baseline_idx) == 1
    assert lines[baseline_idx[0] - 1].startswith("# 창")
    assert b.label() in lines[baseline_idx[0]]
    assert "start=%.3f end=%.3f" % (b.start, b.end) in lines[baseline_idx[0]]


def _summary_text(root: Path) -> str:
    """spill 디렉터리(<root>/tmp/obs) 안의 summary.txt 하나를 읽는다 — 호출이 하나뿐이어야 한다."""
    found = sorted(root.glob("tmp/obs/*/summary.txt"))
    assert len(found) == 1, "summary.txt 가 하나가 아니다: %r" % found
    return found[0].read_text(encoding="utf-8")



def test_promql_main_summarize_writes_no_assembled_query(run_main: MainRunner, base_env: dict[str, str], make_client: ClientFactory,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """promql.py summarize 의 stdout · summary.txt 어디에도 실제로 보낸 조립 질의가 없고, summary.txt 에는 `# 창` 줄이 있다."""
    # given — 빈 응답 · spill 은 명시 env(base_env)의 프로젝트 디렉터리
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    # when
    run_main(cli.promql_main, ["summarize", "up", "--since", "1h"], client)
    # then
    sent = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(t.calls[0].url).query))["query"]
    assert sent != "up"
    summary = _summary_text(Path(base_env["CLAUDE_PROJECT_DIR"]))
    assert [line for line in summary.splitlines() if line.startswith("# 창")]
    assert sent not in summary
    assert sent not in capsys.readouterr().out


def test_promql_main_red_writes_no_query_and_raw_has_one_element_per_call(run_main: MainRunner, base_env: dict[str, str], make_client: ClientFactory,
                                                                           capsys: pytest.CaptureFixture[str]) -> None:
    """promql.py red 의 stdout · summary.txt 어디에도 보낸 조립 질의가 없고, raw.json 의 원소 수가 보낸 호출 수(1)와 같다."""
    # given — 빈 응답 · spill 은 명시 env(base_env)의 프로젝트 디렉터리
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    # when
    run_main(cli.promql_main, ["red", "svc", "--since", "1h"], client)
    # then — 호출 1회 · 그 질의는 서비스 이름 그대로가 아니다
    assert len(t.calls) == 1
    sent = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(t.calls[0].url).query))["query"]
    assert sent != "svc"
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("원본·요약: tmp/obs/")
    assert out[-1].endswith("-promql-red/ (raw.json · summary.txt)")
    assert not any(sent in line for line in out)
    summary = _summary_text(Path(base_env["CLAUDE_PROJECT_DIR"]))
    assert [line for line in summary.splitlines() if line.startswith("# 창")]
    assert sent not in summary
    raw_body: JSON = json.loads((Path(base_env["CLAUDE_PROJECT_DIR"]) / out[-1].split()[1].rstrip("/") / "raw.json").read_text(encoding="utf-8"))
    assert isinstance(raw_body, list)
    assert len(json_list(raw_body)) == len(t.calls)


def test_promql_main_top_rejects_since() -> None:
    """top 은 창이 없으므로 --since 를 받지 않는다 — argparse 가 거부한다(exit 2)."""
    with pytest.raises(SystemExit) as ei:
        cli.promql_main(["top", "x", "--since", "1h"], {}, AT, no_client)
    assert ei.value.code == 2


# ── 실행의 약속 — 손 입력 ─────────────────────────────────────────────────────────

EMPTY_MATRIX: dict[str, JSON] = {"status": "success", "data": {"resultType": "matrix", "result": []}}
STREAMS: dict[str, JSON] = {"status": "success", "data": {"resultType": "streams", "result": [
    {"stream": {"namespace": "n", "pod": "p"}, "values": [["1700000000000000000", "x" * 100], ["1700000001000000000", "short"]]}]}}
SEARCH: dict[str, JSON] = {"traces": [{"traceID": "abc123", "rootServiceName": "svc", "rootTraceName": "op", "startTimeUnixNano": "1700000000000000000",
                                       "durationMs": 12}]}


DISPATCH_CASES: list[tuple[str, MainFn, list[str], RouteKey, dict[RouteKey, tuple[int, object]], dict[str, str]]] = [
    # (id, main, argv, 첫 요청의 (backend, path), 라우트, 요청에 보여야 하는 창 파라미터) — 창은 Window.last("2h", "-10m", at=AT) = [92200, 99400]
    ("logs", cli.logql_main, ["histogram", '{a="b"}', "--since", "2h", "--end=-10m"], ("logs", "loki/api/v1/query_range"),
     {("logs", "loki/api/v1/query_range"): (200, EMPTY_MATRIX)}, {"start": str(int((AT - 600 - 7200) * 1e9)), "end": str(int((AT - 600) * 1e9))}),
    ("traces", cli.traceql_main, ["exemplars", "--since", "2h", "--end=-10m"], ("metrics", "api/v1/query"),
     {("metrics", "api/v1/query"): (200, _vector()), ("traces", "api/search"): (200, {"traces": []})}, {"time": str(AT - 600)}),
    ("traceql", cli.traceql_main, ["tags"], ("traces", "api/v2/search/tags"),
     {("traces", "api/v2/search/tags"): (200, {"scopes": []})}, {"start": str(int(AT - 3600)), "end": str(int(AT))}),
    ("metrics", cli.promql_main, ["red", "svc", "--since", "2h", "--end=-10m"], ("metrics", "api/v1/query"),
     {("metrics", "api/v1/query"): (200, _vector())}, {"time": str(AT - 600)}),
]


@pytest.mark.parametrize("main,argv,route,routes,expected", [c[1:] for c in DISPATCH_CASES], ids=[c[0] for c in DISPATCH_CASES])
def test_main_dispatches_subcommand_to_its_run(main: MainFn, argv: list[str], route: RouteKey, routes: dict[RouteKey, tuple[int, object]],
                                               expected: dict[str, str], run_main: MainRunner, make_client: ClientFactory, config: Config) -> None:
    """argv 의 첫 토큰이 맞는 *_run 을 고른다 — 첫 요청이 그 *_run 의 백엔드 · 경로로 나가고, 요청의 창 파라미터(start · end 또는 time)가 --since · --end 로 계산한 창과 같다."""
    # given — 그 *_run 의 첫 요청에 빈 응답을 주는 라우트 · at 은 AT
    client, t = make_client(routes)
    # when
    run_main(main, argv, client)
    # then
    rq = strip_physical(t.calls[0].url, config, None)
    assert (rq.backend, rq.path) == route
    assert {k: str(v) for k, v in rq.params.items() if k in expected} == expected


def test_promql_range_window_and_auto_step(make_client: ClientFactory) -> None:
    """--range 는 창 길이를 계산하고 step 을 자동(range/60, 최소 15s)으로 붙여 query_range 를 부른다."""
    client, t = make_client({("metrics", "api/v1/query_range"): (200, EMPTY_MATRIX)})
    r = cli.promql_run(client, cli.PromqlArgs(query="sum(up)", range_="30m"), at=AT)
    assert r.kind == "promql-range"
    assert "step=30s" in t.calls[0].url
    assert "start=%s" % (AT - 30 * MINUTE) in t.calls[0].url


EMPTY_STREAMS: dict[str, JSON] = {"status": "success", "data": {"resultType": "streams", "result": []}}
EMPTY_PATTERNS: dict[str, JSON] = {"status": "success", "data": []}

WINDOW_RUNS: list[tuple[str, Callable[[Client], cli.Renderable], dict[RouteKey, tuple[int, object]]]] = [
    ("promql-range", lambda c: cli.promql_run(c, cli.PromqlArgs(query="up", range_="30m", end="-10m"), at=AT),
     {("metrics", "api/v1/query_range"): (200, EMPTY_MATRIX)}),
    ("logql", lambda c: cli.logql_run(c, cli.LogqlArgs(query='{a="b"}', since="30m", end="-10m"), at=AT),
     {("logs", "loki/api/v1/query_range"): (200, EMPTY_STREAMS)}),
    ("summarize", lambda c: cli.metrics_run(c, cli.MetricsArgs(cmd="summarize", query="up", since="30m", end="-10m"), at=AT),
     {("metrics", "api/v1/query"): (200, _vector())}),
    ("histogram", lambda c: cli.logs_run(c, cli.LogsArgs(cmd="histogram", selector='{a="b"}', since="30m", end="-10m"), at=AT),
     {("logs", "loki/api/v1/query_range"): (200, EMPTY_MATRIX)}),
    ("templates", lambda c: cli.logs_run(c, cli.LogsArgs(cmd="templates", selector='{a="b"}', since="30m", end="-10m"), at=AT),
     {("logs", "loki/api/v1/patterns"): (200, EMPTY_PATTERNS)}),
    ("first-seen", lambda c: cli.logs_run(c, cli.LogsArgs(cmd="first-seen", selector='{a="b"}', template="<_> x", since="30m", end="-10m"), at=AT),
     {("logs", "loki/api/v1/query_range"): (200, EMPTY_STREAMS)}),
    ("exemplars", lambda c: cli.traces_run(c, cli.TracesArgs(cmd="exemplars", since="30m", end="-10m"), at=AT),
     {("metrics", "api/v1/query"): (200, _vector()), ("traces", "api/search"): (200, {"traces": []})}),
    ("service-graph", lambda c: cli.traces_run(c, cli.TracesArgs(cmd="service-graph", since="30m", end="-10m"), at=AT),
     {("metrics", "api/v1/query"): (200, _vector())}),
]


@pytest.mark.parametrize("run,routes", [(r, rt) for _, r, rt in WINDOW_RUNS], ids=[name for name, _, _ in WINDOW_RUNS])
def test_run_window_is_cli_window(make_client: ClientFactory, run: Callable[[Client], cli.Renderable],
                                  routes: dict[RouteKey, tuple[int, object]]) -> None:
    """창 인자(--since · --range · --end)를 받는 명령의 RunResult 의 window 는 그 인자로 계산한 창이다 — 기준선이 없다."""
    # given — 빈 응답
    client, _ = make_client(routes)
    # when
    window = run(client).window
    # then
    assert window == Window.last("30m", "-10m", at=AT)


def _lines_of(client: Client, backend: str) -> list[str]:
    if backend == "promql":
        return cli.promql_run(client, cli.PromqlArgs(query="up"), at=AT).lines()
    if backend == "logql":
        return cli.logql_run(client, cli.LogqlArgs(query='{namespace="n"}'), at=AT).lines()
    if backend == "traceql":
        return cli.traceql_run(client, cli.TraceqlArgs(cmd="search"), at=AT).lines()
    return cli.metrics_run(client, cli.MetricsArgs(cmd="summarize", query="up"), at=AT).lines()


@pytest.mark.parametrize("backend", ["promql", "logql", "traceql", "metrics"])
def test_run_outputs_carry_no_physical(make_client: ClientFactory, backend: str) -> None:
    """네 실행의 출력 줄에 물리 문자열(호스트 · 프록시 접두 · uid)이 없다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector(({"__name__": "up", "job": "j"}, "1"))),
                             ("logs", "loki/api/v1/query_range"): (200, STREAMS), ("traces", "api/search"): (200, SEARCH)})
    _no_physical("\n".join(_lines_of(client, backend)))


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


# ── RunResult — 값 · 렌더러 · 줄 ───────────────────────────────────────────────────

def test_run_result_lines_are_render_output_without_marker() -> None:
    """RunResult.lines() 는 render(evidence.value, window) 그대로다 — 렌더러가 빈 목록을 내면 빈 목록이고 "(결과 없음)" 을 붙이지 않는다(그 규칙은 finish 의 것)."""
    # given — 빈 목록을 내는 렌더러 · 값을 그대로 내는 렌더러
    w = Window(AT - MINUTE, AT)
    empty: cli.RunResult[list[str]] = cli.RunResult(Evidence([]), "k", w, lambda v, w: [])
    same: cli.RunResult[list[str]] = cli.RunResult(Evidence(["a", "b"]), "k", w, lambda v, w: v)
    # given — 값과 창을 둘 다 쓰는 렌더러(값을 그대로 돌려주거나 창을 넘기지 않으면 갈린다)
    shaped: cli.RunResult[list[str]] = cli.RunResult(Evidence(["a", "b"]), "k", w, lambda v, w: ["%s@%d" % (x, w.start) for x in v])
    # when · then
    assert empty.lines() == []
    assert same.lines() == ["a", "b"]
    assert shaped.lines() == ["a@%d" % w.start, "b@%d" % w.start]


def test_search_lines_empty_hints_wider_window() -> None:
    """빈 hit 목록의 search_lines 는 창을 넓히라는 힌트 한 줄이다 — 빈 목록이 아니다."""
    # given — hit 없음
    hits: list[cli.TraceHit] = []
    # when
    lines = cli.search_lines(hits)
    # then
    assert len(lines) == 1
    assert lines[0].startswith("(결과 없음 — 트레이스는 드물다")
    assert "--since 24h" in lines[0]


def test_stream_lines_width_clips_and_leads_with_common_labels() -> None:
    """스트림 출력은 공통 레이블 한 줄 뒤에 값 수만큼의 로그 줄이고, width 는 각 줄의 본문을 그 폭에서 자른다."""
    # given — 100자 본문과 짧은 본문을 가진 스트림 하나(STREAMS) · 폭 60
    res = cli.parse_logs(STREAMS)
    # when
    lines = cli.stream_lines(res, "backward", 60)
    # then
    assert lines[0].startswith("공통 레이블 {")
    assert len(lines) == 1 + sum(len(st.values) for st in res.streams)
    for line in lines[1:]:
        assert len(line.split("  ", 2)[-1]) <= 60
