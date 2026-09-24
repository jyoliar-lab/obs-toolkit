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
    """bin/(Claude Code 가 플러그인이 켜진 동안 자동으로 PATH 에 올리는 자리)의 4개 파일은 실행 가능하고 20줄 미만의 shim 이며
    본체 함수(obs.adapters.cli.*_main)를 부른다 — 로직이 스크립트로 새지 않는다."""
    for name, fn in SHIMS:
        p = ROOT / "bin" / name
        assert p.stat().st_mode & stat.S_IXUSR, "%s 는 실행 가능해야 한다" % name
        src = p.read_text(encoding="utf-8")
        assert src.startswith("#!/usr/bin/env python3") and fn in src and len(src.splitlines()) < 20


def test_shim_finds_plugin_root_from_any_cwd(tmp_path: Path) -> None:
    """shim 은 cwd 와 무관하게 자기 위치에서 플러그인 루트를 찾는다 — 낯선 cwd 에서 --help 가 usage 를 낸다."""
    import subprocess
    import sys as _sys
    out = subprocess.run([_sys.executable, str(ROOT / "bin" / "promql.py"), "--help"], capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0 and out.stdout.startswith("usage: promql.py"), out.stderr[-300:]


def test_plugin_manifests() -> None:
    """플러그인 골격 — plugin.json 이름, marketplace.json 이 자기 자신(./)을 가리킴, hooks.json 이 ${CLAUDE_PLUGIN_ROOT} 로 session-env.sh 를 부르고 그 스크립트가 CLAUDE_ENV_FILE 에 PATH·PYTHONPATH 를 쓴다."""
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["name"] == "obs-toolkit"
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    assert market["name"] == "obs-toolkit" and market["plugins"][0]["name"] == "obs-toolkit" and market["plugins"][0]["source"] == "./"
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())
    cmd = hooks["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "${CLAUDE_PLUGIN_ROOT}" in cmd and "session-env.sh" in cmd
    sh = (ROOT / "hooks" / "session-env.sh").read_text()
    assert "CLAUDE_ENV_FILE" in sh and "PYTHONPATH" in sh and "PATH" in sh


def test_skill_frontmatter_spec_fields_only() -> None:
    """SKILL.md 프론트매터는 Agent Skills 스펙 6필드 안에서만(name·description 이 앞), name 이 디렉터리 이름과 같고, 본문에 물리 정보(호스트·경로)가 없다."""
    text = (ROOT / "skills" / "obs-query" / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m
    keys = [line.split(":", 1)[0] for line in m.group(1).splitlines() if line and not line.startswith(" ")]
    assert keys[0:2] == ["name", "description"]
    assert set(keys) <= {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    assert re.search(r"^name: obs-query$", m.group(1), re.M)
    _no_physical(text)


def test_gitignore_and_rules() -> None:
    """저장소 기본 — .gitignore 에 tmp/·settings.local.json, 규칙 파일(.claude/rules/python-typehints.md)과 CI 워크플로가 있다."""
    gi = (ROOT / ".gitignore").read_text().splitlines()
    assert "tmp/" in gi and "settings.local.json" in gi
    assert (ROOT / ".claude" / "rules" / "python-typehints.md").is_file()
    assert (ROOT / ".github" / "workflows" / "pytest.yml").is_file()
