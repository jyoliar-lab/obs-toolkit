"""트레이스 환원 연산의 출력 — 녹화 fixture 의 replay 로 exemplars · critical_path · service_graph · diff 의 줄 모양을 본다.
단언은 스냅샷과 값에서 유도한 불변식(줄 수 = 머리 + 항목 수 등)뿐이다."""
from __future__ import annotations

import pytest

from obs import traces
from obs.adapters import cli
from obs.core import Window
from tests.conftest import ReplayFactory, assert_snapshot, load_call, load_fixture_calls, window_of
from tests.record import NO_SUCH_SERVICE


def _day(at: float) -> Window:
    return Window.last("24h", at=at)


# ── exemplars ────────────────────────────────────────────────────────────────────

def test_exemplars_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """exemplars 의 렌더링은 헤더 · 요약 두 줄 뒤에 묶음마다 머리줄 하나와 건 줄이고 요약 줄에 표본 크기가 있다 — 줄 수 = 2 + 3 + 건 수 합."""
    # given
    client, t = replay("traceql-exemplars.json")
    # when
    ev, _ = traces.exemplars(client, "", _day(t.at), k=3)
    lines = cli.exemplars_lines(ev.value, window_of(ev))
    v = ev.value
    # then
    assert len(lines) == 2 + 3 + len(v.slow.items) + len(v.error.items) + len(v.normal.items)
    assert str(v.sample) in lines[1]
    assert_snapshot(request, lines)
    t.assert_exhausted()


def test_exemplars_lines_empty(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """없는 서비스의 exemplars 렌더링은 "데이터 없음" 한 줄이고 창을 넓히라는 안내가 있다."""
    # given
    client, t = replay("traceql-exemplars-empty.json")
    # when
    ev, _ = traces.exemplars(client, NO_SUCH_SERVICE, _day(t.at), k=3)
    lines = cli.exemplars_lines(ev.value, window_of(ev))
    # then
    assert len(lines) == 1
    assert lines[0].startswith("(데이터 없음")
    assert "--since" in lines[0]
    assert_snapshot(request, lines)


# ── critical_path ────────────────────────────────────────────────────────────────

def test_critical_path_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """critical_path 의 렌더링은 헤더 뒤에 경로 span 마다 한 줄, 그 뒤 병목 머리줄과 병목 상위 세 줄(self 내림차순)이고 첫 병목의 깊이는 2 이상이다."""
    # given
    client, t = replay(("traceql-critical-path.json", "api/traces/"))
    trace_id = load_call(("traceql-critical-path.json", "api/traces/")).call.request.path.rsplit("/", 1)[1]
    # when
    ev, _ = traces.critical_path(client, trace_id, Window.last("1h", at=t.at))
    lines = cli.critical_path_lines(ev.value, window_of(ev))
    v = ev.value
    # then
    assert len(lines) == 1 + len(v.path) + 1 + min(3, len(v.bottlenecks))
    assert v.bottlenecks[0].depth >= 2
    assert v.bottlenecks[0].name in lines[1 + len(v.path) + 1]
    assert_snapshot(request, lines)


# ── service_graph ────────────────────────────────────────────────────────────────

def test_service_graph_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """service_graph 의 렌더링은 헤더 뒤에 (caller → callee) 머리줄마다 그 쌍의 엔드포인트 줄이다 — 머리줄 수 = 쌍의 수 · 엔드포인트 줄 수 = 엣지 수."""
    # given
    client, t = replay("traceql-service-graph.json")
    # when
    ev, _ = traces.service_graph(client, _day(t.at))
    lines = cli.service_graph_lines(ev.value, window_of(ev))
    edges = ev.value.edges
    # then
    assert edges
    assert sum(1 for line in lines if "→" in line) == len({(e.caller, e.callee) for e in edges})
    assert sum(1 for line in lines if any(e.endpoint in line for e in edges)) == len(edges)
    assert_snapshot(request, lines)
    t.assert_exhausted()


def test_service_graph_lines_empty(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """보존 밖 창의 service_graph 렌더링은 "데이터 없음" 한 줄이고 창을 넓히라는 안내(--since 24h)가 있다."""
    # given
    client, t = replay("traceql-service-graph-empty.json")
    # when
    ev, _ = traces.service_graph(client, Window.last("1h", end="-60d", at=t.at))
    lines = cli.service_graph_lines(ev.value, window_of(ev))
    # then
    assert len(lines) == 1
    assert lines[0].startswith("(데이터 없음")
    assert "--since 24h" in lines[0]
    assert_snapshot(request, lines)


# ── diff ─────────────────────────────────────────────────────────────────────────

def test_diff_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """diff 의 렌더링은 헤더 한 줄 뒤에 (서비스, 이름) 행마다 한 줄이다 — 줄 수 = 1 + 행 수."""
    # given
    client, t = replay("traceql-diff.json")
    slow_id, normal_id = [c.call.request.path.rsplit("/", 1)[1] for c in load_fixture_calls("traceql-diff.json")]
    # when
    ev, _ = traces.diff(client, slow_id, normal_id, Window.last("1h", at=t.at))
    lines = cli.diff_lines(ev.value, window_of(ev))
    # then
    assert len(lines) == 1 + len(ev.value.deltas)
    assert_snapshot(request, lines)
    t.assert_exhausted()
