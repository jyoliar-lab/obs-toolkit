"""플러그인 골격 — bin/ shim · manifest · hooks · SKILL.md 프론트매터 · 저장소 기본 파일."""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path

from tests.conftest import ROOT, _no_physical


SHIMS: tuple[tuple[str, str], ...] = (("promql.py", "promql_main"), ("logql.py", "logql_main"),
                                       ("traceql.py", "traceql_main"), ("obs-doctor.py", "doctor_main"))


def test_scripts_are_thin_executable_shims() -> None:
    """bin/ 의 네 파일은 실행 가능한 20줄 미만의 shim 이고 본체 함수(obs.adapters.cli.*_main)를 부른다."""
    for name, fn in SHIMS:
        p = ROOT / "bin" / name
        assert p.stat().st_mode & stat.S_IXUSR, "%s 는 실행 가능해야 한다" % name
        src = p.read_text(encoding="utf-8")
        assert src.startswith("#!/usr/bin/env python3")
        assert fn in src
        assert len(src.splitlines()) < 20


def test_shim_finds_plugin_root_from_any_cwd(tmp_path: Path) -> None:
    """shim 은 cwd 와 무관하게 자기 위치에서 플러그인 루트를 찾는다."""
    import subprocess
    import sys as _sys
    out = subprocess.run([_sys.executable, str(ROOT / "bin" / "promql.py"), "--help"], capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr[-300:]
    assert out.stdout.startswith("usage: promql.py"), out.stderr[-300:]


def test_plugin_manifests() -> None:
    """plugin.json · marketplace.json · hooks.json · session-env.sh 가 서로를 가리키고 PATH · PYTHONPATH 를 쓴다."""
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "obs-toolkit"
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "obs-toolkit"
    assert market["plugins"][0]["name"] == "obs-toolkit"
    assert market["plugins"][0]["source"] == "./"
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    cmd = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "${CLAUDE_PLUGIN_ROOT}" in cmd
    assert "session-env.sh" in cmd
    sh = (ROOT / "hooks" / "session-env.sh").read_text()
    assert "CLAUDE_ENV_FILE" in sh
    assert "PYTHONPATH" in sh
    assert "PATH" in sh


def test_skill_frontmatter_spec_fields_only() -> None:
    """SKILL.md 프론트매터는 스펙 6필드 안이고 name 이 디렉터리 이름과 같으며 본문에 물리 정보가 없다."""
    text = (ROOT / "skills" / "obs-query" / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m
    keys = [line.split(":", 1)[0] for line in m.group(1).splitlines() if line and not line.startswith(" ")]
    assert keys[0:2] == ["name", "description"]
    assert set(keys) <= {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    assert re.search(r"^name: obs-query$", m.group(1), re.M)
    _no_physical(text)


def test_gitignore_and_rules() -> None:
    """.gitignore · 규칙 파일 · CI 워크플로가 있다."""
    gi = (ROOT / ".gitignore").read_text().splitlines()
    assert "tmp/" in gi
    assert "settings.local.json" in gi
    assert (ROOT / ".claude" / "rules" / "python-typehints.md").is_file()
    assert (ROOT / ".github" / "workflows" / "pytest.yml").is_file()
