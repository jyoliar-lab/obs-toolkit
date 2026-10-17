"""메트릭 환원 연산의 출력 — 녹화 fixture 의 replay 로 summarize · compare · top 의 렌더링과 CLI 의 stdout · summary.txt 를 본다.
compare_lines 의 예외 행(기준선 없음 · 기준선 0 · 지금 없음)은 실환경이 한 번에 만들 수 없어 손 입력으로 스냅샷을 뜬다."""
from __future__ import annotations

from pathlib import Path

import pytest

from obs import metrics
from obs.adapters import cli
from obs.core import Window, fmt_rfc3339, json_list, json_obj
from tests.conftest import (
    AT, RED_BASE_ONLY, RED_FULL, RED_NOW_ONLY, UNCHECKED_SHARED_EMPTY, ClientFactory, ReplayFactory, _red_body, _stat, _vector, assert_snapshot,
    load_response,
)
from tests.record import CPU_QUERY


def _pods_in(name: str) -> set[str]:
    """fixture 응답의 pod 레이블 집합 — 시계열 수의 기대값을 fixture 에서 유도한다."""
    body = cli.check_status(load_response(name), "metrics")
    return {str(json_obj(json_obj(item).get("metric")).get("pod")) for item in json_list(json_obj(body.get("data")).get("result"))}


# ── summarize ────────────────────────────────────────────────────────────────────

def test_summary_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """summarize 의 렌더링은 창 헤더 한 줄 뒤에 시계열마다 일곱 통계 한 줄이다 — 시계열 수는 fixture 의 pod 수이고 min ≤ p50 ≤ p95 ≤ max 다."""
    client, t = replay("promql-instant-summarize-cpu-by-pod.json")
    w = Window.last("1h", at=t.at)
    ev = metrics.summarize(client, CPU_QUERY, w)
    assert len(ev.value.series) == len(_pods_in("promql-instant-summarize-cpu-by-pod.json"))
    for s in ev.value.series:
        assert s.min <= s.p50 <= s.p95 <= s.max
    assert_snapshot(request, cli.summary_lines(ev.value, w))


def test_summary_lines_empty(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """빈 응답의 summarize 렌더링은 "데이터 없음" 한 줄이다."""
    client, t = replay("promql-instant-empty.json", unchecked=UNCHECKED_SHARED_EMPTY)
    w = Window.last("1h", at=t.at)
    ev = metrics.summarize(client, CPU_QUERY, w)
    assert_snapshot(request, cli.summary_lines(ev.value, w))


# ── compare ──────────────────────────────────────────────────────────────────────

def test_compare_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """compare 의 렌더링은 헤더 한 줄 뒤에 시계열마다 비율 · Δ · now · base 한 줄이 비율 내림차순이다 — Δ 는 now − base 다."""
    client, t = replay("promql-instant-compare-cpu-by-pod-1d.json")
    w = Window.last("1h", at=t.at).with_baseline("1d")
    ev = metrics.compare(client, CPU_QUERY, w)
    assert len(ev.value.deltas) == len(_pods_in("promql-instant-compare-cpu-by-pod-1d.json"))
    ratios = [d.ratio for d in ev.value.deltas]
    assert ratios == sorted((r for r in ratios if r is not None), reverse=True)
    for d in ev.value.deltas:
        assert d.now is not None
        assert d.base is not None
        assert d.delta == pytest.approx(d.now - d.base)
    assert_snapshot(request, cli.compare_lines(ev.value, w))


def test_compare_lines_without_baseline(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """보존 밖 offset 으로 base 가 부재면 렌더링이 "기준선 없음" 을 말하고 비율은 어디에도 없다."""
    client, t = replay("promql-instant-compare-cpu-by-pod-no-baseline.json")
    w = Window.last("1h", at=t.at).with_baseline("60d")
    ev = metrics.compare(client, CPU_QUERY, w)
    assert ev.value.baseline_empty
    assert all(d.ratio is None for d in ev.value.deltas)
    assert_snapshot(request, cli.compare_lines(ev.value, w))


def test_compare_lines_mark_missing_baseline_zero_and_missing_now(make_client: ClientFactory, request: pytest.FixtureRequest) -> None:
    """시계열별 예외 행 — 기준선 0 은 `×—` 와 "(기준선 0)", 기준선 없음 · 지금 없음은 그 말로 시작한다."""
    body = _vector(_stat("a", "now", "0.4"), _stat("a", "base", "0"), _stat("b", "now", "1"), _stat("c", "base", "3"),
                   _stat("d", "now", "3.9"), _stat("d", "base", "1"))
    client, _ = make_client({("metrics", "api/v1/query"): (200, body)})
    w = Window.last("1h", at=AT).with_baseline("1d")
    ev = metrics.compare(client, "q", w)
    assert_snapshot(request, cli.compare_lines(ev.value, w))


# ── red — 손 입력 스냅샷(실환경 fixture 는 구현 뒤 녹화 — 머지 직전 커밋) ─────────────────

RED_W: Window = Window.last("1h", at=AT).with_baseline("1d")


def _red_lines(make_client: ClientFactory, **stats: str) -> list[str]:
    client, _ = make_client({("metrics", "api/v1/query"): (200, _red_body(**stats))})
    return cli.red_lines(metrics.red(client, "svc", RED_W).value, RED_W)


def test_red_lines(make_client: ClientFactory, request: pytest.FixtureRequest) -> None:
    """red 의 렌더링은 헤더 한 줄 뒤에 R · E · D 세 줄이다 — 각 줄이 그 이름으로 시작한다."""
    # given — 양쪽 창에 값이 있는 응답
    client, _ = make_client({("metrics", "api/v1/query"): (200, _red_body(**RED_FULL))})
    # when
    ev = metrics.red(client, "svc", RED_W)
    lines = cli.red_lines(ev.value, RED_W)
    # then
    assert len(lines) == 1 + len(ev.value.signals)
    assert len(lines) == 1 + len(metrics.RED_SIGNALS)
    for line, name in zip(lines[1:], ["R", "E", "D"]):
        assert line.startswith(name)
    assert_snapshot(request, lines)


def test_red_lines_missing_series(make_client: ClientFactory, request: pytest.FixtureRequest) -> None:
    """지금 창에 호출 카운터 시계열이 없는 서비스의 렌더링."""
    # given — 기준선만 있는 응답
    # when
    lines = _red_lines(make_client, **RED_BASE_ONLY)
    # then
    assert_snapshot(request, lines)


def test_red_lines_idle(make_client: ClientFactory, request: pytest.FixtureRequest) -> None:
    """시계열은 있되 창 안 호출이 0 인 서비스의 렌더링."""
    # given — calls_now 0 · p95_now NaN · 기준선 있음
    # when
    lines = _red_lines(make_client, calls_now="0", p95_now="NaN", **RED_BASE_ONLY)
    # then
    assert_snapshot(request, lines)


@pytest.mark.parametrize("stats", [
    RED_NOW_ONLY,
    dict(calls_base="0", p95_base="NaN", **RED_NOW_ONLY),
    dict(calls_now="200", errors_now="10", p95_now="0.4", calls_base="100", p95_base="0.2"),
], ids=["missing", "idle", "e-zero"])
def test_red_lines_baseline_states(make_client: ClientFactory, request: pytest.FixtureRequest, stats: dict[str, str]) -> None:
    """기준선 없음 · 기준선 호출 0 · 오류 기준선 0 의 렌더링."""
    # given — 9 · 10 · 11 의 응답
    # when
    lines = _red_lines(make_client, **stats)
    # then
    assert_snapshot(request, lines)


# ── top ──────────────────────────────────────────────────────────────────────────

def test_top_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """top 의 렌더링은 값 내림차순으로 항목마다 `값  {레이블}` 한 줄이다."""
    client, t = replay("promql-instant-topk-memory-by-pod.json")
    w = Window.last("1h", at=t.at)
    ev = metrics.top(client, "container_memory_working_set_bytes", w, k=10, by=["pod"])
    assert [e.value for e in ev.value.entries] == sorted((e.value for e in ev.value.entries), reverse=True)
    assert_snapshot(request, cli.top_lines(ev.value, w))


def test_top_lines_empty(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """빈 응답의 top 렌더링은 "데이터 없음" 한 줄이다."""
    client, t = replay("promql-instant-empty.json", unchecked=UNCHECKED_SHARED_EMPTY)
    w = Window.last("1h", at=t.at)
    ev = metrics.top(client, "nothing", w)
    assert_snapshot(request, cli.top_lines(ev.value, w))


# ── CLI — stdout 절제 · summary.txt 머리 ───────────────────────────────────────────

def test_promql_main_top_prints_table_and_call_dir_only(replay: ReplayFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    """`promql.py top …` 은 stdout 에 표와 호출 디렉터리 한 줄만 찍고(조립 질의 없음), summary.txt 머리에 창만 남고 그 뒤가 stdout 의 표다."""
    client, t = replay("promql-instant-topk-memory-by-pod.json")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    cli.promql_main(["top", "container_memory_working_set_bytes", "--by", "pod", "--k", "10", "--end", str(t.at)])
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("원본·요약: tmp/obs/")
    assert out[-1].endswith("-promql-top/ (raw.json · summary.txt)")
    assert not any("topk(" in l for l in out)
    summary = (tmp_path / out[-1].split()[1].rstrip("/")).joinpath("summary.txt").read_text(encoding="utf-8").splitlines()
    assert summary[0].startswith("# 창  ")
    assert summary[1] == ""
    assert summary[2:] == out[:-1]
    assert not any("topk(" in l for l in summary)
    t.assert_exhausted()


def test_promql_main_plain_query_is_instant_lookup(replay: ReplayFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """서브커맨드 없는 `promql.py 'up'` 은 그대로 즉시 조회다 — 질의가 감싸이지 않는다."""
    client, t = replay("promql-instant-up.json")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    cli.promql_main(["up", "--end", str(t.at)])
    assert "query=up" in t.calls[0].url
    assert "topk" not in t.calls[0].url
    t.assert_exhausted()


def test_compare_summary_preamble_has_window_baseline(replay: ReplayFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    """summary.txt 머리에는 창 · 기준선이 RFC3339 와 unix 초로 남고 stdout · summary.txt 어디에도 조립 질의가 없다."""
    client, t = replay("promql-instant-compare-cpu-by-pod-1d.json")
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    cli.promql_main(["compare", CPU_QUERY, "--since", "1h", "--baseline", "1d", "--end", str(t.at)])
    out = capsys.readouterr().out.splitlines()
    assert not any("label_replace" in l for l in out)
    summary = (tmp_path / out[-1].split()[1].rstrip("/")).joinpath("summary.txt").read_text(encoding="utf-8").splitlines()
    assert summary[0] == "# 창  %s ~ %s  (start=%.3f end=%.3f)" % (fmt_rfc3339(t.at - 3600), fmt_rfc3339(t.at), t.at - 3600, t.at)
    assert summary[1] == "# 기준선  %s ~ %s  (start=%.3f end=%.3f)" % (fmt_rfc3339(t.at - 3600 - 86400), fmt_rfc3339(t.at - 86400), t.at - 3600 - 86400, t.at - 86400)
    assert summary[2] == ""
    assert summary[3:] == out[:-1]
    assert not any("label_replace" in l for l in summary)
