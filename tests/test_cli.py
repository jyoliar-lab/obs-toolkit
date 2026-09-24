"""CLI 어댑터 — 녹화 응답으로 요약 포맷 · 출력 절제 · 물리 비노출 · 플러그인 골격."""
from __future__ import annotations

import dataclasses
import json
import re
import stat
from pathlib import Path
from typing import Callable

import pytest

from obs.adapters import cli
from obs.core import DoctorReport, Evidence, Lines, Probe, Resolved
from tests.conftest import ROOT, ClientFactory, ReplayFactory, _no_physical, load_response


# ── promql ───────────────────────────────────────────────────────────────────────

def test_promql_instant_up(replay: ReplayFactory) -> None:
    """AC 3 의 첫 실측 `up` — 녹화 응답이 Evidence[Lines] 로 옮겨지고 시계열마다 `값  {레이블}` 한 줄이며, 나간 URL 에 query·time 이 있고 출력에 물리 정보가 없다."""
    client, t = replay("promql-instant-up.json")
    ev, _, kind = cli.promql_run(client, cli.PromqlArgs(query="up"), at=t.at)
    assert isinstance(ev, Evidence) and isinstance(ev.value, Lines) and ev.backend == "metrics" and ev.query == "up" and kind == "promql"
    lines = ev.value.lines
    assert len(lines) == 65 and all(l.startswith("1  {") for l in lines) and "job=kubelet" in lines[0]   # 2026-09-22 재녹화: 65 타깃 전부 up
    assert "query=up" in t.calls[0].url and "time=%s" % t.at in t.calls[0].url
    _no_physical("\n".join(lines))


def test_promql_range_summary(replay: ReplayFactory) -> None:
    """--range 구간 조회 — 창 길이가 계산되고 step 이 자동(range/60, 최소 15s)으로 붙으며, 시계열이 `n min max first last` 한 줄로 요약된다."""
    client, t = replay("promql-range-sum-up.json")
    ev, _, kind = cli.promql_run(client, cli.PromqlArgs(query="sum(up)", range_="30m"), at=t.at)
    assert kind == "promql-range" and ev.window is not None and ev.window.seconds == 1800.0
    assert ev.value.lines == ["n=61  min=65  max=65  first=65  last=65  {}"]   # 30m/30s → 61 점 (재녹화)
    assert "step=30s" in t.calls[0].url and "start=%s" % (t.at - 1800.0) in t.calls[0].url


def test_promql_points(replay: ReplayFactory) -> None:
    """--points — 요약 대신 레이블 줄 아래 시각별 값이 전부 나온다(많으니 보통 spill 로 떨어지는 형태)."""
    client, t = replay("promql-range-sum-up.json")
    ev, _, _ = cli.promql_run(client, cli.PromqlArgs(query="sum(up)", range_="30m", points=True), at=t.at)
    assert ev.value.lines[0] == "{}" and len(ev.value.lines) == 62 and ev.value.lines[1].strip().endswith("65")   # 레이블 줄 + 61 점


def test_promql_requires_query(make_client: ClientFactory) -> None:
    """질의 없이 즉시 조회를 부르면 ObsError — argparse 가 아니라 *_run 이 거부하므로 MCP 경로에서도 같은 검사가 산다."""
    client, _ = make_client()
    with pytest.raises(cli.ObsError):
        cli.promql_run(client, cli.PromqlArgs())


def test_parse_metric_shapes() -> None:
    """파서 단독 — vector 응답이 MetricResult(시계열·값)로, scalar 응답은 scalar 필드로 옮겨지고, 형태가 깨진 result 는 빈 목록으로 조용히 처리된다."""
    res = cli.parse_metric(cli.check_status(load_response("promql-instant-up.json"), "metrics"))
    assert res.result_type == "vector" and res.scalar is None and len(res.series) == 65
    assert all(len(s.values) == 1 and s.values[0][1] == "1" for s in res.series)
    scalar = cli.parse_metric({"data": {"resultType": "scalar", "result": [1.0, "42"]}})
    assert scalar.scalar == "42" and cli.instant_lines(scalar) == ["42"]
    assert cli.parse_metric({"data": {"resultType": "vector", "result": "garbage"}}).series == []


def test_promql_backend_error_status(make_client: ClientFactory) -> None:
    """HTTP 200 이지만 본문이 `status: error` 인 경우 — 논리 이름·errorType·메시지로 ObsError. 백엔드 이름(Prometheus)이 아니라 논리 이름(metrics)으로 말한다."""
    client, _ = make_client({("metrics", "api/v1/query"): (200, {"status": "error", "errorType": "bad_data", "error": "boom"})})
    with pytest.raises(cli.ObsError) as ei:
        cli.promql_run(client, cli.PromqlArgs(query="up"))
    assert str(ei.value).startswith("metrics 백엔드 질의 오류(bad_data): boom")


# ── logql ────────────────────────────────────────────────────────────────────────

def test_logql_streams(replay: ReplayFactory) -> None:
    """AC 3 의 둘째 실측 careerhub-prod 로그 — 공통 레이블은 머리에 한 번, 각 줄은 시각으로 시작하고 최신순이며, URL 에 limit·direction·나노초 창이 있고 출력에 물리 정보가 없다.
    줄 수 기대값은 fixture 를 파서로 다시 읽어 계산한다(숫자를 하드코딩하지 않는다)."""
    client, t = replay("logql-streams-careerhub-prod.json")
    ev, _, kind = cli.logql_run(client, cli.LogqlArgs(query='{namespace="careerhub-prod"}', limit=5), at=t.at)
    lines = ev.value.lines
    assert kind == "logql" and lines[0].startswith("공통 레이블 {") and "namespace=careerhub-prod" in lines[0]
    parsed = cli.parse_logs(cli.check_json(load_response("logql-streams-careerhub-prod.json"), "logs"))
    expected = sum(len(st.values) for st in parsed.streams)
    assert len(parsed.streams) == 1 and expected == 5 and len(lines) == 1 + expected   # 재녹화: limit=5 가 한 스트림(mongodb)에서 찼다
    assert re.match(r"^\d\d-\d\d \d\d:\d\d:\d\dZ ", lines[1])   # 시각은 UTC 이고 Z 로 표기한다(마찰 2)
    # backward: 최신이 먼저
    assert lines[1][:14] >= lines[-1][:14]
    assert "limit=5" in t.calls[0].url and "direction=backward" in t.calls[0].url
    assert "start=%d" % int((t.at - 3600) * 1e9) in t.calls[0].url
    _no_physical("\n".join(lines))


def test_logql_matrix(replay: ReplayFactory) -> None:
    """메트릭형 LogQL(count_over_time) — resultType=matrix 를 알아보고 시계열 요약(`n=… detected_level=…`)으로 낸다."""
    client, t = replay("logql-matrix-count-over-time.json")
    ev, _, _ = cli.logql_run(client, cli.LogqlArgs(query="sum by (detected_level) (count_over_time({namespace=\"careerhub-prod\"}[5m]))"), at=t.at)
    assert all(l.startswith("n=") and "detected_level=" in l for l in ev.value.lines)


def test_logql_width_clip(replay: ReplayFactory) -> None:
    """--width — 로그 본문이 지정 폭에서 잘린다. 긴 줄이 컨텍스트를 먹지 않게 하는 절제다."""
    client, t = replay("logql-streams-careerhub-prod.json")
    ev, _, _ = cli.logql_run(client, cli.LogqlArgs(query='{namespace="careerhub-prod"}', limit=5, width=60), at=t.at)   # 녹화와 같은 질의 — width 는 URL 에 없다
    assert all(len(l.split("  ", 2)[-1]) <= 60 for l in ev.value.lines[1:])


# ── traceql ──────────────────────────────────────────────────────────────────────

def test_traceql_search_then_trace(replay: ReplayFactory) -> None:
    """AC 3 의 셋째 실측 — search 결과 첫 줄에서 trace ID 를 뽑아 trace 명령에 넣는 흐름. search URL 에 limit·창이 있고, trace URL 이 api/traces/<id> 로 끝나며, 요약(총시간·루트·오류 수)에 물리 정보가 없다."""
    client, t = replay("traceql-search.json", ("traceql-trace.json", "api/traces/"))   # 순서대로: search → traces/<id>
    ev, _, kind = cli.traceql_run(client, cli.TraceqlArgs(cmd="search", since="24h", limit=3), at=t.at)
    assert kind == "traceql-search" and len(ev.value.lines) == 3
    trace_id = ev.value.lines[0].split()[0]
    assert re.fullmatch(r"[0-9a-f]{31,32}", trace_id)
    assert "limit=3" in t.calls[0].url and "start=%d" % (int(t.at) - 86400) in t.calls[0].url

    ev2, _, kind2 = cli.traceql_run(client, cli.TraceqlArgs(cmd="trace", arg=trace_id), at=t.at)
    lines = ev2.value.lines
    assert kind2 == "traceql-trace" and lines[0].startswith("트레이스 %s  총 " % trace_id)
    assert lines[1].startswith("루트: ngf:careerhub-prod:careerhub-nginx-gateway /") and lines[3] == "오류 span: 0"   # 재녹화(2026-09-22 10:44Z): 게이트웨이 → api-composer → 세 서비스, span 8
    assert t.calls[1].url.endswith("api/traces/" + trace_id)
    t.assert_exhausted()
    _no_physical("\n".join(lines))


def test_traceql_search_empty_hints_wider_window(make_client: ClientFactory) -> None:
    """search 가 비면 창을 넓히라는 힌트를 낸다 — 시험 보고의 관찰: 기본 1시간 창에는 트레이스가 없는 것이 보통이다."""
    client, _ = make_client({("traces", "api/search"): (200, {"traces": []})})
    ev, _, _ = cli.traceql_run(client, cli.TraceqlArgs(cmd="search"))
    assert ev.value.lines == ["(결과 없음 — 트레이스는 드물다. 창을 넓혀 다시: --since 24h · 7d)"]


def test_traceql_tags(replay: ReplayFactory) -> None:
    """tags — scope 별로 `[resource] a, b, c` 한 줄. 탐색용 명령의 형식."""
    client, t = replay("traceql-tags.json")
    ev, _, _ = cli.traceql_run(client, cli.TraceqlArgs(cmd="tags"), at=t.at)
    assert any(l.startswith("[resource] ") and "service.name" in l for l in ev.value.lines)


def test_traceql_trace_requires_id(make_client: ClientFactory) -> None:
    """trace 명령에 ID 가 없으면 ObsError."""
    client, _ = make_client()
    with pytest.raises(cli.ObsError):
        cli.traceql_run(client, cli.TraceqlArgs(cmd="trace"))


def test_parse_spans_error_detection() -> None:
    """span 파서와 오류 표시 — 녹화 트레이스가 Span 으로 옮겨지고(service·오류 아님), 상태를 STATUS_CODE_ERROR 로 바꾸면 `오류 span: 1` 과 ✗ 줄이 나온다.
    fixture dict 를 고치지 않고 dataclasses.replace 로 바꾼다 — Any 없이 쓰는 방식."""
    spans = cli.parse_spans(cli.check_json(load_response("traceql-trace.json", "api/traces/"), "traces"))
    assert len(spans) == 8 and spans[0].service == "posting-service" and not spans[0].is_error   # 재녹화: 8 span — 첫 span 은 posting-service
    errored = [dataclasses.replace(spans[0], status_code="STATUS_CODE_ERROR")]
    lines = cli.trace_lines("abc", errored, top=5)
    assert lines[3] == "오류 span: 1" and lines[4].startswith("  ✗ posting-service /")


def test_trace_error_span_shows_status_message() -> None:
    """재시험 보고(#93 코멘트)의 새 마찰 2 — ✗ 줄 뒤가 비어 원인을 보려면 jq 왕복이 필요했다. 녹화한 실제 오류 트레이스(posting-service
    GetAllHiring, mongodb EOF)에서 status.message 의 첫 줄(탭 뒤 StackTrace 제외)이 ✗ 줄에 실린다. 원인이 여러 번 반복된 긴 문구라 잘리되 EOF 원인은 남는다."""
    spans = cli.parse_spans(cli.check_json(load_response("traceql-trace-error.json", "api/traces/"), "traces"))
    assert len(spans) == 1 and spans[0].is_error and spans[0].status_message.lstrip().startswith("connection(")
    lines = cli.trace_lines("d47871cc20408ed84a7506ccf306200d", spans, top=5)
    assert lines[3] == "오류 span: 1"
    err = lines[4]
    assert err.startswith("  ✗ posting-service / ") and "incomplete read of message header" in err
    assert "StackTrace" not in err and "\t" not in err and "\n" not in err


def test_trace_error_reason_falls_back_to_attrs() -> None:
    """status.message 가 비면 예전처럼 exception.message → error.message → http.status_code 순으로 속성에서 찾는다.
    스택만 있고 첫 줄이 비는 message 도 빈 것으로 본다."""
    base = cli.parse_spans(cli.check_json(load_response("traceql-trace.json", "api/traces/"), "traces"))[0]
    s = dataclasses.replace(base, status_code="STATUS_CODE_ERROR", status_message="", attrs={**base.attrs, "exception.message": "boom"})
    assert cli.error_reason(s) == "boom"
    s = dataclasses.replace(s, status_message="\n\tStackTrace: a.go:1")
    assert cli.error_reason(s) == "boom"
    s = dataclasses.replace(s, status_message="  EOF\tStackTrace: a.go:1")
    assert cli.error_reason(s) == "EOF"


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


# ── doctor 렌더링 — 물리가 보이는 유일한 출력 ─────────────────────────────────────

def test_doctor_lines_show_url_and_hide_token() -> None:
    """AC 2 — obs-doctor.py 렌더링에는 URL·uid·출처가 보이고(물리가 보이는 유일한 출력), 토큰은 있음/없음만이며, 실패 probe 는 ✗·HTTP 상태·문구와 "문제 있음" 결론으로 이어진다."""
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
def test_no_physical_flags(main: Callable[[list[str]], None], capsys: pytest.CaptureFixture[str]) -> None:
    """AC 2 의 반대편 — 세 명령의 --help 에 --uid·--url·--token·--host 가 없고 물리 문자열도 없다. agent 인터페이스에 물리 인자를 두지 않는다는 요구."""
    with pytest.raises(SystemExit):
        main(["--help"])
    text = capsys.readouterr().out
    for flag in ("--uid", "--url", "--token", "--host"):
        assert flag not in text
    _no_physical(text)


# ── 플러그인 골격 ────────────────────────────────────────────────────────────────

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
