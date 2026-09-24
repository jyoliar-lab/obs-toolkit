"""CLI 어댑터의 약속 — 질의 없음 · ID 없음 거부, 백엔드 오류 상태, 빈 검색의 힌트, 출력 절제(emit), --help 의 물리 비노출."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

import pytest

from obs.adapters import cli
from tests.conftest import ClientFactory, _no_physical


# ── 거부 · 오류 ──────────────────────────────────────────────────────────────────

def test_promql_requires_query(make_client: ClientFactory) -> None:
    """질의 없이 즉시 조회를 부르면 ObsError — argparse 가 아니라 *_run 이 거부하므로 MCP 경로에서도 같은 검사가 산다."""
    client, _ = make_client()
    with pytest.raises(cli.ObsError):
        cli.promql_run(client, cli.PromqlArgs())


def test_promql_backend_error_status(make_client: ClientFactory) -> None:
    """HTTP 200 이지만 본문이 `status: error` 인 경우 — 논리 이름·errorType·메시지로 ObsError. 백엔드 이름(Prometheus)이 아니라 논리 이름(metrics)으로 말한다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, {"status": "error", "errorType": "bad_data", "error": "boom"})})
    with pytest.raises(cli.ObsError) as ei:
        cli.promql_run(client, cli.PromqlArgs(query="up"))
    assert str(ei.value).startswith("metrics 백엔드 질의 오류(bad_data): boom")


def test_traceql_search_empty_hints_wider_window(make_client: ClientFactory) -> None:
    """search 가 비면 창을 넓히라는 힌트를 낸다 — 시험 보고의 관찰: 기본 1시간 창에는 트레이스가 없는 것이 보통이다."""
    client, _ = make_client({("traces", "api/search"): (200, {"traces": []})})
    ev, _, _ = cli.traceql_run(client, cli.TraceqlArgs(cmd="search"))
    assert ev.value.lines == ["(결과 없음 — 트레이스는 드물다. 창을 넓혀 다시: --since 24h · 7d)"]


def test_traceql_trace_requires_id(make_client: ClientFactory) -> None:
    """trace 명령에 ID 가 없으면 ObsError."""
    client, _ = make_client()
    with pytest.raises(cli.ObsError):
        cli.traceql_run(client, cli.TraceqlArgs(cmd="trace"))


# ── 출력 절제 ────────────────────────────────────────────────────────────────────

def test_emit_truncates_and_spills(tmp_path: Path) -> None:
    """출력 절제 — max_lines 를 넘으면 앞부분만 찍고 "N줄 더 있음" 과 호출 디렉터리를 안내한다. 디렉터리 안 raw.json 이 원본 전체,
    summary.txt 가 잘리지 않은 요약 전체다."""
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    out: list[str] = []
    lines = ["l%d" % i for i in range(10)]
    res = cli.emit(lines, {"full": True}, "k", max_lines=3, env=env, out=out.append)
    assert res.lines == ["l0", "l1", "l2"] and res.cut == 7 and res.ref.startswith("tmp/obs/") and res.ref.endswith("-k")
    assert out[-1] == "… 7줄 더 있음 (총 10줄). 원본·요약: %s/ (raw.json · summary.txt)" % res.ref
    assert json.loads((tmp_path / res.ref / "raw.json").read_text()) == {"full": True}
    assert (tmp_path / res.ref / "summary.txt").read_text().splitlines() == lines


def test_emit_quiet_writes_files_prints_only_path(tmp_path: Path) -> None:
    """--json(quiet) — stdout 에 요약을 찍지 않고 경로만. 파일은 보통 때와 같다(요약 전체가 summary.txt 에)."""
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    out: list[str] = []
    res = cli.emit(["a", "b"], {"x": 1}, "k", max_lines=3, quiet=True, env=env, out=out.append)
    assert res.lines == [] and out == ["원본·요약: %s/ (raw.json · summary.txt)" % res.ref]
    assert (tmp_path / res.ref / "summary.txt").read_text() == "a\nb\n"


def test_emit_always_writes_even_when_fits(tmp_path: Path) -> None:
    """상한 안에 들어가도 호출 디렉터리는 만든다 — 원본·요약은 기본으로 파일에 남고, stdout 은 요약 뒤에 경로 한 줄."""
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    out: list[str] = []
    res = cli.emit(["a", "b"], {"x": 1}, "k", max_lines=3, env=env, out=out.append)
    assert res.cut == 0 and out == ["a", "b", "원본·요약: %s/ (raw.json · summary.txt)" % res.ref]
    assert sorted(q.name for q in (tmp_path / res.ref).iterdir()) == ["raw.json", "summary.txt"]


# ── 인터페이스 — 물리 인자가 없다 ────────────────────────────────────────────────

@pytest.mark.parametrize("main", [cli.promql_main, cli.logql_main, cli.traceql_main])
def test_no_physical_flags(main: Callable[[list[str]], None], capsys: pytest.CaptureFixture[str]) -> None:
    """AC 2 의 반대편 — 세 명령의 --help 에 --uid·--url·--token·--host 가 없고 물리 문자열도 없다. agent 인터페이스에 물리 인자를 두지 않는다는 요구."""
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    for flag in ("--uid", "--url", "--token", "--host"):
        assert flag not in text
    _no_physical(text)


@pytest.mark.parametrize("argv", [["--help"], ["compare", "--help"], ["top", "--help"], ["summarize", "--help"]])
def test_help_mentions_operations_without_physical(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """--help — 최상위에 세 연산이 보이고, 서브커맨드 도움말에 --baseline(compare) · --by/--k(top) 이 있으며, 물리 인자·문자열이 없다."""
    with pytest.raises(SystemExit) as ei:
        cli.promql_main(argv)
    assert ei.value.code == 0
    text = capsys.readouterr().out
    if argv == ["--help"]:
        assert "summarize" in text and "compare" in text and "top" in text
    if argv[0] == "summarize":
        assert "quantile(0.5·0.95)" in text and "deriv" in text and "--since" in text and "--step" in text
    if argv[0] == "compare":
        assert "--baseline" in text and "prev" in text and "offset" in text and "기준선 없음" in text
    if argv[0] == "top":
        assert "--by" in text and "--k" in text and "--agg" in text and "namespace,pod" in text and "--since SINCE" not in text   # 옵션으로는 없다(설명 문장에만)
    for flag in ("--uid", "--url", "--token", "--host"):
        assert flag not in text
    _no_physical(text)
