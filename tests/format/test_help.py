"""--help 텍스트 — 최상위와 서브커맨드 도움말이 말하는 것."""
from __future__ import annotations

import pytest

from obs.adapters import cli


@pytest.mark.parametrize("argv", [["--help"], ["compare", "--help"], ["top", "--help"], ["summarize", "--help"]])
def test_help_mentions_operations(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """promql --help 는 세 연산을, 서브커맨드 도움말은 자기 옵션과 설명을 보인다."""
    with pytest.raises(SystemExit):
        cli.promql_main(argv)
    text = capsys.readouterr().out
    if argv == ["--help"]:
        assert "summarize" in text
        assert "compare" in text
        assert "top" in text
    if argv[0] == "summarize":
        assert "quantile(0.5·0.95)" in text
        assert "deriv" in text
        assert "--since" in text
        assert "--step" in text
    if argv[0] == "compare":
        assert "--baseline" in text
        assert "prev" in text
        assert "offset" in text
        assert "기준선 없음" in text
    if argv[0] == "top":
        assert "--by" in text
        assert "--k" in text
        assert "--agg" in text
        assert "namespace,pod" in text
        assert "--since SINCE" not in text   # 옵션으로는 없다(설명 문장에만)
