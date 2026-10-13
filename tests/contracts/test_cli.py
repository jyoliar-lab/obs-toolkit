"""CLI 어댑터의 약속 — 질의 없음 · ID 없음 거부, 백엔드 오류 상태, 빈 검색의 힌트, 출력 절제(emit), --help 의 물리 비노출."""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Callable

import pytest

from obs.adapters import cli
from obs.core import JSON, BudgetExceeded, Client, Evidence, Lines, ObsError, Response, Window
from tests.conftest import AT, HOUR, MINUTE, ClientFactory, RouteKey, _no_physical, _vector


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


def test_rejected_response_stays_in_log(make_client: ClientFactory) -> None:
    """HTTP 200 인데 본문이 `status: error` 라 연산이 거부해도 그 응답은 client.log 에 남는다 — status 200 · body 가 그 거부된 본문."""
    # given — 200 인데 status: error
    body: dict[str, JSON] = {"status": "error", "errorType": "bad_data", "error": "boom"}
    client, _ = make_client({("metrics", "api/v1/query"): (200, body)})
    # when
    with pytest.raises(cli.ObsError):
        cli.promql_run(client, cli.PromqlArgs(query="up"))
    # then
    assert len(client.log) == 1
    assert client.log[0].status == 200
    assert client.log[0].body == body


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


def test_emit_writes_log_items_with_one_key_set(tmp_path: Path) -> None:
    """log(성공 · 실패 섞임)는 raw.json 에 같은 순서의 목록으로 쓰이고 원소마다 키 집합이 backend · path · params · status · body · error · size · truncated 로 같다."""
    # given — 성공 하나 · 전송 실패 하나
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    log = [Response(backend="metrics", path="api/v1/query", status=200, body={"a": 1}, params={"query": "up"}, size=8),
           Response(backend="traces", path="api/search", status=None, body=None, error="traces 백엔드에 닿지 못했다")]
    # when
    res = cli.emit(["x"], log, "k", max_lines=3, env=env, out=lambda _: None)
    # then
    items = json.loads((tmp_path / res.ref / "raw.json").read_text())
    assert [(i["backend"], i["path"]) for i in items] == [(r.backend, r.path) for r in log]
    for item in items:
        assert set(item) == {"backend", "path", "params", "status", "body", "error", "size", "truncated"}
    assert [i["status"] for i in items] == [200, None]
    assert [i["error"] for i in items] == [None, "traces 백엔드에 닿지 못했다"]


# ── finish — 성공은 emit, 실패도 호출 디렉터리 ───────────────────────────────────

FINISH_ROUTES: dict[RouteKey, tuple[int, object]] = {("metrics", "api/v1/labels"): (200, {"data": []}), ("metrics", "api/v1/series"): (200, {"data": []})}


def _run_after(client: Client, calls: int, raise_: BaseException | None = None) -> Callable[[], cli.RunResult]:
    """호출 calls 개(labels · series 순)를 보낸 뒤 raise_ 를 올리거나 요약 하나를 돌려주는 run 대역."""
    def run() -> cli.RunResult:
        for path in ("api/v1/labels", "api/v1/series")[:calls]:
            client.get("metrics", path)
        if raise_ is not None:
            raise raise_
        return Evidence(value=Lines(["x"])), "k", Window(AT - MINUTE, AT)
    return run


def _only_dir(root: Path) -> Path:
    """<root>/tmp/obs 의 호출 디렉터리 하나."""
    dirs = sorted((root / "tmp" / "obs").iterdir())
    assert len(dirs) == 1, "호출 디렉터리가 하나가 아니다: %r" % dirs
    return dirs[0]


def test_finish_writes_log_as_raw_json(make_client: ClientFactory, tmp_path: Path) -> None:
    """성공하면 raw.json 의 원소 수가 client.log 와 같고, 디렉터리 이름은 run 의 kind 이며(-failed 아님), out 의 마지막 줄이 그 경로다."""
    # given — 호출 둘을 보내고 성공하는 run
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    client, _ = make_client(FINISH_ROUTES)
    out: list[str] = []
    # when
    cli.finish(client, _run_after(client, 2), "k-failed", 40, False, env=env, out=out.append)
    # then
    assert len(client.log) == 2
    d = _only_dir(tmp_path)
    assert d.name.endswith("-k")
    assert len(json.loads((d / "raw.json").read_text())) == 2
    assert d.name in out[-1]


def test_finish_names_call_directory_in_obs_error(make_client: ClientFactory, tmp_path: Path) -> None:
    """run 이 ObsError(하위 종류 포함)를 올리면 같은 종류로 다시 올라오고 — 문구가 원래 문구로 시작하며 끝에 -failed 디렉터리 경로와 raw.json 을 말한다."""
    # given — 호출 둘 뒤 BudgetExceeded
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    client, _ = make_client(FINISH_ROUTES)
    # when
    with pytest.raises(BudgetExceeded) as ei:
        cli.finish(client, _run_after(client, 2, BudgetExceeded("예산 초과 — 질의 20회 상한")), "promql-failed", 40, False, env=env, out=lambda _: None)
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
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    client, _ = make_client(FINISH_ROUTES)
    out: list[str] = []
    # when
    with pytest.raises(ObsError):
        cli.finish(client, _run_after(client, 2, ObsError("metrics 백엔드가 질의를 거부했다(400)")), "promql-failed", 40, False, env=env, out=out.append)
    # then
    d = _only_dir(tmp_path)
    assert len(json.loads((d / "raw.json").read_text())) == 2
    assert "metrics 백엔드가 질의를 거부했다(400)" in (d / "summary.txt").read_text()
    assert out == []


def test_finish_spills_log_and_reraises_other_exceptions(make_client: ClientFactory, tmp_path: Path) -> None:
    """run 이 ObsError 가 아닌 예외(버그)를 올리면 그대로 올라오고, err 로 -failed 경로 한 줄을 찍으며, 디렉터리의 raw.json 에 보낸 호출(원소 1)이 있다."""
    # given — 호출 하나 뒤 ValueError
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    client, _ = make_client(FINISH_ROUTES)
    err: list[str] = []
    bug = ValueError("bug")
    # when
    with pytest.raises(ValueError) as ei:
        cli.finish(client, _run_after(client, 1, bug), "logql-failed", 40, False, env=env, out=lambda _: None, err=err.append)
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
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    client, _ = make_client()
    # when
    with pytest.raises(ObsError) as ei:
        cli.finish(client, _run_after(client, 0, ObsError("PromQL 이 필요하다")), "promql-failed", 40, False, env=env, out=lambda _: None)
    # then
    assert str(ei.value) == "PromQL 이 필요하다"
    assert not (tmp_path / "tmp" / "obs").exists()


def test_finish_without_sent_calls_reraises_other_exceptions_silently(make_client: ClientFactory, tmp_path: Path) -> None:
    """보낸 호출이 없는 비-ObsError 예외(버그)는 같은 객체로 그대로 올라오고, err 로 아무것도 찍지 않으며, 디렉터리도 만들지 않는다."""
    # given — 호출 없이 ValueError
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    client, _ = make_client()
    err: list[str] = []
    bug = ValueError("bug")
    # when
    with pytest.raises(ValueError) as ei:
        cli.finish(client, _run_after(client, 0, bug), "promql-failed", 40, False, env=env, out=lambda _: None, err=err.append)
    # then
    assert ei.value is bug
    assert err == []
    assert not (tmp_path / "tmp" / "obs").exists()


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
        return Evidence(value=Lines(["x"])), "logql-histogram", Window(AT - MINUTE, AT)

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
    (["exemplars", "--q", 'resource.service.name = "a"', "--k", "2", "--quantile", "0.9", "--baseline-range", "3d", "--limit", "50", "--since", "6h", "--end=-10m"],
     cli.TracesArgs(cmd="exemplars", q='resource.service.name = "a"', since="6h", end="-10m", k=2, quantile=0.9, baseline_range="3d", limit=50)),
    (["critical-path", "abc"], cli.TracesArgs(cmd="critical-path", trace_id="abc")),
    (["service-graph", "--quantile", "0.9", "--baseline-range", "3d"], cli.TracesArgs(cmd="service-graph", quantile=0.9, baseline_range="3d")),
    (["diff", "s1", "n1"], cli.TracesArgs(cmd="diff", slow_id="s1", normal_id="n1")),
], ids=["exemplars", "critical-path", "service-graph", "diff"])
def test_traceql_main_routes_subcommand_to_traces_run(argv: list[str], expected: cli.TracesArgs, monkeypatch: pytest.MonkeyPatch,
                                                      make_client: ClientFactory, tmp_path: Path) -> None:
    """traceql.py 의 첫 인자가 서브커맨드면 traces_run 으로 간다 — cmd · 위치 인자(traceID) · 옵션(k · quantile · baseline-range · limit · since · end) 이 TracesArgs 로 들어가고 나머지 필드는 전부 기본값이다."""
    # given — traces_run 대역이 받은 Args 를 기록한다
    seen: list[cli.TracesArgs] = []

    def fake_run(client: Client, a: cli.TracesArgs, at: float | None = None) -> cli.RunResult:
        seen.append(a)
        return Evidence(value=Lines(["x"])), "traceql-" + a.cmd, Window(AT - MINUTE, AT)

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
        return Evidence(value=Lines(["x"])), "traceql-tags", Window(AT - MINUTE, AT)

    client, _ = make_client()
    monkeypatch.setattr(cli, "traceql_run", fake_run)
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    # when
    cli.traceql_main(["tags"])
    # then
    assert [a.cmd for a in seen] == ["tags"]


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


def test_finish_writes_window_preamble_then_lines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """_finish 의 summary.txt 는 window_preamble(창) 뒤에 요약 줄이고, stdout 에는 요약 줄만 — `# ` 머리 줄이 없다."""
    # given — 기준선이 달린 창 · 요약 줄 둘 · spill 은 tmp
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    w = Window(AT - HOUR, AT).with_baseline("1d")
    ev = Evidence(value=Lines(["a 1", "b 2"]))
    # when
    cli._finish(ev, [], "k", w, 40, False)  # pyright: ignore[reportPrivateUsage]
    # then
    assert _summary_text(tmp_path).splitlines() == [*cli.window_preamble(w), "a 1", "b 2"]
    out = capsys.readouterr().out.splitlines()
    assert out[:2] == ["a 1", "b 2"]
    assert not [line for line in out if line.startswith("# ")]


def test_promql_main_summarize_writes_no_assembled_query(monkeypatch: pytest.MonkeyPatch, make_client: ClientFactory, tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """promql.py summarize 의 stdout · summary.txt 어디에도 실제로 보낸 조립 질의가 없고, summary.txt 에는 `# 창` 줄이 있다."""
    # given — 빈 응답 · spill 은 tmp
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    # when
    cli.promql_main(["summarize", "up", "--since", "1h"])
    # then
    sent = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(t.calls[0].url).query))["query"]
    assert sent != "up"
    summary = _summary_text(tmp_path)
    assert [line for line in summary.splitlines() if line.startswith("# 창")]
    assert sent not in summary
    assert sent not in capsys.readouterr().out


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
    _, kind, _ = cli.promql_run(client, cli.PromqlArgs(query="sum(up)", range_="30m"), at=AT)
    assert kind == "promql-range"
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


EMPTY_STREAMS: dict[str, JSON] = {"status": "success", "data": {"resultType": "streams", "result": []}}
EMPTY_PATTERNS: dict[str, JSON] = {"status": "success", "data": []}

WINDOW_RUNS: list[tuple[str, Callable[[Client], cli.RunResult], dict[RouteKey, tuple[int, object]]]] = [
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
def test_run_window_is_cli_window(make_client: ClientFactory, run: Callable[[Client], cli.RunResult],
                                  routes: dict[RouteKey, tuple[int, object]]) -> None:
    """창 인자(--since · --range · --end)를 받는 명령의 RunResult 셋째 자리는 그 인자로 계산한 창이다 — 기준선이 없다."""
    # given — 빈 응답
    client, _ = make_client(routes)
    # when
    _, _, window = run(client)
    # then
    assert window == Window.last("30m", "-10m", AT)


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
