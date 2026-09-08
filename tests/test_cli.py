"""CLI 어댑터 — 녹화 응답으로 요약 포맷 · 출력 절제 · 물리 비노출 · 플러그인 골격."""
from __future__ import annotations

import json
import re
import stat
from pathlib import Path
from typing import Callable

import pytest

from obs.adapters import cli
from obs.core import DoctorReport, Evidence, Probe, Resolved
from tests.conftest import ROOT, load_fixture

PHYSICAL: tuple[str, ...] = ("obs.mg", "jyo-liar.com/api", "datasources/proxy", "uid/prometheus", "loki-careerhub", "tempo-careerhub")


def _no_physical(text: str) -> None:
    for word in PHYSICAL:
        assert word not in text, "agent 출력에 물리 정보: %s" % word


# ── promql ───────────────────────────────────────────────────────────────────────

def test_promql_instant_up(make_client: Callable) -> None:
    client, t = make_client({"api/v1/query?": (200, load_fixture("promql-instant-up.json"))})
    ev, body, kind = cli.promql_run(client, cli.PromqlArgs(query="up"), at=1000.0)
    assert isinstance(ev, Evidence) and ev.backend == "metrics" and ev.query == "up" and kind == "promql"
    lines = ev.value["lines"]
    assert len(lines) == 5 and all(l.startswith("1  {") for l in lines) and "job=kubelet" in lines[0]
    assert "query=up" in t.calls[0].url and "time=1000.0" in t.calls[0].url
    _no_physical("\n".join(lines))


def test_promql_range_summary(make_client: Callable) -> None:
    client, t = make_client({"api/v1/query_range": (200, load_fixture("promql-range-sum-up.json"))})
    ev, _, kind = cli.promql_run(client, cli.PromqlArgs(query="sum(up)", range_="30m"), at=10000.0)
    assert kind == "promql-range" and ev.window is not None and ev.window.seconds == 1800.0
    assert ev.value["lines"] == ["n=6  min=65  max=65  first=65  last=65  {}"]
    assert "step=30s" in t.calls[0].url and "start=8200.0" in t.calls[0].url


def test_promql_points(make_client: Callable) -> None:
    client, _ = make_client({"api/v1/query_range": (200, load_fixture("promql-range-sum-up.json"))})
    ev, _, _ = cli.promql_run(client, cli.PromqlArgs(query="sum(up)", range_="30m", points=True), at=10000.0)
    assert ev.value["lines"][0] == "{}" and len(ev.value["lines"]) == 7 and ev.value["lines"][1].strip().endswith("65")


def test_promql_requires_query(make_client: Callable) -> None:
    client, _ = make_client()
    with pytest.raises(cli.ObsError):
        cli.promql_run(client, cli.PromqlArgs())


def test_promql_backend_error_status(make_client: Callable) -> None:
    client, _ = make_client({"api/v1/query": (200, {"status": "error", "errorType": "bad_data", "error": "boom"})})
    with pytest.raises(cli.ObsError) as ei:
        cli.promql_run(client, cli.PromqlArgs(query="up"))
    assert str(ei.value).startswith("metrics 백엔드 질의 오류(bad_data): boom")


# ── logql ────────────────────────────────────────────────────────────────────────

def test_logql_streams(make_client: Callable) -> None:
    client, t = make_client({"loki/api/v1/query_range": (200, load_fixture("logql-streams-careerhub-prod.json"))})
    ev, _, kind = cli.logql_run(client, cli.LogqlArgs(query='{namespace="careerhub-prod"}', limit=5), at=10000.0)
    lines = ev.value["lines"]
    assert kind == "logql" and lines[0].startswith("공통 레이블 {") and "namespace=careerhub-prod" in lines[0]
    expected = sum(len(st["values"]) for st in load_fixture("logql-streams-careerhub-prod.json")["data"]["result"])
    assert len(lines) == 1 + expected and expected >= 3
    assert re.match(r"^\d\d-\d\d \d\d:\d\d:\d\d ", lines[1])
    # backward: 최신이 먼저
    assert lines[1][:14] >= lines[-1][:14]
    assert "limit=5" in t.calls[0].url and "direction=backward" in t.calls[0].url
    assert "start=%d" % int((10000.0 - 3600) * 1e9) in t.calls[0].url
    _no_physical("\n".join(lines))


def test_logql_matrix(make_client: Callable) -> None:
    client, _ = make_client({"loki/api/v1/query_range": (200, load_fixture("logql-matrix-count-over-time.json"))})
    ev, _, _ = cli.logql_run(client, cli.LogqlArgs(query="sum by (detected_level) (count_over_time({namespace=\"careerhub-prod\"}[5m]))"))
    assert all(l.startswith("n=") and "detected_level=" in l for l in ev.value["lines"])


def test_logql_width_clip(make_client: Callable) -> None:
    client, _ = make_client({"loki/api/v1/query_range": (200, load_fixture("logql-streams-careerhub-prod.json"))})
    ev, _, _ = cli.logql_run(client, cli.LogqlArgs(query="{a=\"b\"}", width=60))
    assert all(len(l.split("  ", 2)[-1]) <= 60 for l in ev.value["lines"][1:])


# ── traceql ──────────────────────────────────────────────────────────────────────

def test_traceql_search_then_trace(make_client: Callable) -> None:
    client, t = make_client({"api/search": (200, load_fixture("traceql-search.json")),
                             "api/traces/": (200, load_fixture("traceql-trace.json"))})
    ev, _, kind = cli.traceql_run(client, cli.TraceqlArgs(cmd="search", since="24h", limit=3), at=10000.0)
    assert kind == "traceql-search" and len(ev.value["lines"]) == 3
    trace_id = ev.value["lines"][0].split()[0]
    assert re.fullmatch(r"[0-9a-f]{31,32}", trace_id)
    assert "limit=3" in t.calls[0].url and "start=%d" % (10000 - 86400) in t.calls[0].url

    ev2, _, kind2 = cli.traceql_run(client, cli.TraceqlArgs(cmd="trace", arg=trace_id))
    lines = ev2.value["lines"]
    assert kind2 == "traceql-trace" and lines[0].startswith("트레이스 %s  총 " % trace_id)
    assert lines[1].startswith("루트: posting-service /") and lines[3] == "오류 span: 0"
    assert t.calls[1].url.endswith("api/traces/" + trace_id)
    _no_physical("\n".join(lines))


def test_traceql_tags(make_client: Callable) -> None:
    client, _ = make_client({"api/v2/search/tags": (200, load_fixture("traceql-tags.json"))})
    ev, _, _ = cli.traceql_run(client, cli.TraceqlArgs(cmd="tags"))
    assert any(l.startswith("[resource] ") and "service.name" in l for l in ev.value["lines"])


def test_traceql_trace_requires_id(make_client: Callable) -> None:
    client, _ = make_client()
    with pytest.raises(cli.ObsError):
        cli.traceql_run(client, cli.TraceqlArgs(cmd="trace"))


def test_parse_spans_error_detection() -> None:
    body = load_fixture("traceql-trace.json")
    spans = cli.parse_spans(body)
    assert len(spans) == 1 and spans[0].service == "posting-service" and not spans[0].is_error
    body["batches"][0]["scopeSpans"][0]["spans"][0]["status"] = {"code": "STATUS_CODE_ERROR"}
    lines = cli.trace_lines("abc", cli.parse_spans(body), top=5)
    assert lines[3] == "오류 span: 1" and lines[4].startswith("  ✗ posting-service /")


# ── 출력 절제 ────────────────────────────────────────────────────────────────────

def test_emit_truncates_and_spills(tmp_path: Path) -> None:
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    out: list[str] = []
    res = cli.emit(["l%d" % i for i in range(10)], {"full": True}, "k", max_lines=3, env=env, out=out.append)
    assert res.lines == ["l0", "l1", "l2"] and res.cut == 7 and res.raw_ref and res.raw_ref.startswith("tmp/obs/")
    assert out[-1] == "… 7줄 더 있음 (총 10줄). 원본 전체: %s" % res.raw_ref
    assert json.loads((tmp_path / res.raw_ref).read_text()) == {"full": True}


def test_emit_force_spill(tmp_path: Path) -> None:
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    out: list[str] = []
    res = cli.emit(["a"], {"x": 1}, "k", max_lines=3, force_spill=True, env=env, out=out.append)
    assert res.cut == 0 and res.raw_ref and out == ["a", "원본 전체: %s" % res.raw_ref]


def test_emit_no_spill_when_fits(tmp_path: Path) -> None:
    env = {"CLAUDE_PROJECT_DIR": str(tmp_path)}
    res = cli.emit(["a", "b"], {}, "k", max_lines=3, env=env, out=lambda s: None)
    assert res.raw_ref is None and not (tmp_path / "tmp" / "obs").exists()


# ── doctor 렌더링 — 물리가 보이는 유일한 출력 ─────────────────────────────────────

def test_doctor_lines_show_url_and_hide_token() -> None:
    rep = DoctorReport(
        grafana_url=Resolved("https://gw.example", "default"), proxy_prefix=Resolved("/p", "default"),
        uids={"metrics": Resolved("prom", "env:OBS_UID_METRICS")}, config_path=None, config_candidates=["/x/config.json"],
        token_present=True, spill_dir="/proj/tmp/obs",
        probes=[Probe("metrics", "prom", "https://gw.example/p/prom/api", False, 401, "metrics 백엔드 인증 실패(401)")],
    )
    text = "\n".join(cli.doctor_lines(rep))
    assert "https://gw.example" in text and "uid metrics   prom  (env:OBS_UID_METRICS)" in text
    assert "token         있음 (env)" in text and "✗ metrics  HTTP 401" in text and "결론: 문제 있음" in text


# ── 인터페이스 — 물리 인자가 없다 ────────────────────────────────────────────────

@pytest.mark.parametrize("main", [cli.promql_main, cli.logql_main, cli.traceql_main])
def test_no_physical_flags(main: Callable, capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    for flag in ("--uid", "--url", "--token", "--host"):
        assert flag not in text
    _no_physical(text)


# ── 플러그인 골격 ────────────────────────────────────────────────────────────────

def test_scripts_are_thin_executable_shims() -> None:
    d = ROOT / "skills" / "obs-query" / "scripts"
    for name, fn in (("promql.py", "promql_main"), ("logql.py", "logql_main"), ("traceql.py", "traceql_main"), ("obs-doctor", "doctor_main")):
        p = d / name
        assert p.stat().st_mode & stat.S_IXUSR, "%s 는 실행 가능해야 한다" % name
        src = p.read_text(encoding="utf-8")
        assert src.startswith("#!/usr/bin/env python3") and fn in src and len(src.splitlines()) < 20


def test_plugin_manifests() -> None:
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
    text = (ROOT / "skills" / "obs-query" / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m
    keys = [line.split(":", 1)[0] for line in m.group(1).splitlines() if line and not line.startswith(" ")]
    assert keys[0:2] == ["name", "description"]
    assert set(keys) <= {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    assert re.search(r"^name: obs-query$", m.group(1), re.M)
    _no_physical(text)


def test_gitignore_and_rules() -> None:
    gi = (ROOT / ".gitignore").read_text().splitlines()
    assert "tmp/" in gi and "settings.local.json" in gi
    assert (ROOT / ".claude" / "rules" / "python-typehints.md").is_file()
    assert (ROOT / ".github" / "workflows" / "pytest.yml").is_file()
