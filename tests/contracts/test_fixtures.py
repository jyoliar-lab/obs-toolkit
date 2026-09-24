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
    """AC 7 — 커밋되는 파일이다: `_no_physical` 의 단어 목록(호스트 · 프록시 경로 · uid)과 토큰 자국이 파일 전체에 없다."""
    text = (FIXTURES / name).read_text(encoding="utf-8")
    _no_physical(text)
    for mark in TOKEN_MARKS:
        assert mark not in text, "%s: %s" % (name, mark)
