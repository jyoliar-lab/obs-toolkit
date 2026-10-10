"""--help 텍스트 — 열 화면의 스냅샷. 폭은 COLUMNS 로 고정한다(argparse 가 터미널 폭으로 줄을 바꾼다)."""
from __future__ import annotations

from typing import Callable

import pytest

from obs.adapters import cli
from tests.conftest import assert_snapshot

HELP_SCREENS: dict[str, tuple[Callable[[list[str]], None], list[str]]] = {
    "promql": (cli.promql_main, ["--help"]), "promql-summarize": (cli.promql_main, ["summarize", "--help"]),
    "promql-compare": (cli.promql_main, ["compare", "--help"]), "promql-top": (cli.promql_main, ["top", "--help"]),
    "logql": (cli.logql_main, ["--help"]), "traceql": (cli.traceql_main, ["--help"]),
    "logql-histogram": (cli.logql_main, ["histogram", "--help"]), "logql-templates": (cli.logql_main, ["templates", "--help"]),
    "logql-around": (cli.logql_main, ["around", "--help"]), "logql-first-seen": (cli.logql_main, ["first-seen", "--help"]),
    "traceql-exemplars": (cli.traceql_main, ["exemplars", "--help"]), "traceql-critical-path": (cli.traceql_main, ["critical-path", "--help"]),
    "traceql-service-graph": (cli.traceql_main, ["service-graph", "--help"]), "traceql-diff": (cli.traceql_main, ["diff", "--help"]),
}


@pytest.mark.parametrize("screen", sorted(HELP_SCREENS))
def test_help_text(screen: str, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """--help 화면의 텍스트 — 서브커맨드 화면이면 첫 줄(usage)에 그 서브커맨드 이름이 있다."""
    monkeypatch.setenv("COLUMNS", "120")
    main, argv = HELP_SCREENS[screen]
    with pytest.raises(SystemExit):
        main(argv)
    out = capsys.readouterr().out.splitlines()
    for word in argv[:-1]:
        assert word in out[0]
    assert_snapshot(request, out)
