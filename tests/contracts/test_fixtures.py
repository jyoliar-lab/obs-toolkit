"""커밋되는 fixture 파일의 약속 — 물리 정보와 토큰 자국이 없다."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import FIXTURES, _no_physical


FIXTURE_FILES: list[Path] = sorted(FIXTURES.glob("*"))


NAMES: list[str] = [p.name for p in FIXTURE_FILES]


TOKEN_MARKS: tuple[str, ...] = ("Authorization", "Bearer ", "glsa_")


@pytest.mark.parametrize("name", NAMES)
def test_fixture_carries_no_physical_or_token(name: str) -> None:
    """커밋되는 fixture 파일 전체에 물리 문자열과 토큰 자국이 없다."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    _no_physical(text)
    for mark in TOKEN_MARKS:
        assert mark not in text, "%s: %s" % (name, mark)
