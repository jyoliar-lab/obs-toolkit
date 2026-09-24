"""assert_snapshot — 없으면 쓴다 · 같으면 통과 · 다르면 diff 로 실패 · OBS_TEST_UPDATE=1 이면 다시 쓴다. 파일 이름은 테스트 이름 그대로."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests import conftest
from tests.conftest import UPDATE_ENV, assert_snapshot


@pytest.fixture
def snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """스냅샷 디렉터리를 tmp 로 돌린다 — 저장소의 tests/format/snapshots/ 를 건드리지 않는다."""
    monkeypatch.setattr(conftest, "SNAPSHOTS", tmp_path)
    monkeypatch.delenv(UPDATE_ENV, raising=False)
    return tmp_path


def test_missing_snapshot_is_written_under_test_name(request: pytest.FixtureRequest, snapshots: Path) -> None:
    """파일이 없으면 이번 출력을 <테스트 이름>.txt 로 쓰고 통과한다."""
    assert_snapshot(request, ["a", "b"])
    assert (snapshots / "test_missing_snapshot_is_written_under_test_name.txt").read_text(encoding="utf-8") == "a\nb\n"


def test_matching_snapshot_passes(request: pytest.FixtureRequest, snapshots: Path) -> None:
    """파일과 같으면 통과하고 파일은 그대로다."""
    path = snapshots / "test_matching_snapshot_passes.txt"
    path.write_text("a\n", encoding="utf-8")
    assert_snapshot(request, ["a"])
    assert path.read_text(encoding="utf-8") == "a\n"


def test_differing_snapshot_fails_with_diff(request: pytest.FixtureRequest, snapshots: Path) -> None:
    """파일과 다르면 실패 문구에 unified diff(-기존 · +이번)가 있다."""
    (snapshots / "test_differing_snapshot_fails_with_diff.txt").write_text("a\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"(?s)-a\n\+b\n"):
        assert_snapshot(request, ["b"])


def test_update_env_rewrites_snapshot(request: pytest.FixtureRequest, snapshots: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """OBS_TEST_UPDATE=1 이면 다른 출력으로 파일을 다시 쓰고 통과한다."""
    path = snapshots / "test_update_env_rewrites_snapshot.txt"
    path.write_text("a\n", encoding="utf-8")
    monkeypatch.setenv(UPDATE_ENV, "1")
    assert_snapshot(request, ["b"])
    assert path.read_text(encoding="utf-8") == "b\n"
