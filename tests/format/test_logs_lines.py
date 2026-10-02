"""로그 환원 연산의 출력 — 녹화 fixture 의 replay 로 histogram · templates · around · first_seen 의 줄 모양을 본다.
단언은 스냅샷과 fixture 를 파서로 다시 읽어 유도한 불변식뿐이다."""
from __future__ import annotations

import pytest

from obs import logs
from obs.adapters import cli
from obs.core import Window, parse_duration
from obs.metrics import auto_step
from tests.conftest import ReplayFactory, assert_snapshot, load_response, window_of
from tests.record import EMPTY_LOG_SELECTOR, FIRST_SEEN_TEMPLATE, LOG_SELECTOR

TEMPLATES_FIXTURE: str = "logql-templates-careerhub-prod.json"


def _hour(at: float) -> Window:
    return Window.last("1h", at=at)


def _recorded_patterns() -> list[logs.Pattern]:
    """templates fixture 의 patterns 응답을 파서로 다시 읽는다 — 군집 수 · 첫 군집의 기대값을 fixture 에서 유도한다."""
    return logs.parse_patterns(cli.check_status(load_response(TEMPLATES_FIXTURE, "patterns"), "logs"))


# ── histogram ────────────────────────────────────────────────────────────────────

def test_histogram_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """histogram 의 렌더링은 헤더 한 줄 뒤에 행(group)마다 한 줄이고, 접힌 행이 있으면 꼬리 한 줄이다."""
    # given
    client, t = replay("logql-histogram-by-level.json")
    # when
    ev, _ = logs.histogram(client, LOG_SELECTOR, _hour(t.at))
    lines = cli.histogram_lines(ev.value, window_of(ev))
    # then
    assert len(lines) == 1 + len(ev.value.groups) + (1 if ev.value.folded else 0)
    assert_snapshot(request, lines)
    t.assert_exhausted()


def test_histogram_lines_empty(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """빈 창의 histogram 렌더링은 "데이터 없음" 한 줄이다."""
    # given
    client, t = replay("logql-histogram-empty.json")
    # when
    ev, _ = logs.histogram(client, EMPTY_LOG_SELECTOR, _hour(t.at))
    lines = cli.histogram_lines(ev.value, window_of(ev))
    # then
    assert len(lines) == 1
    assert lines[0].startswith("(데이터 없음")
    assert_snapshot(request, lines)


# ── templates ────────────────────────────────────────────────────────────────────

def test_templates_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """templates 의 렌더링은 헤더 한 줄 뒤에 군집마다 두 줄(요약 · 예시)이고 other 가 있으면 꼬리 한 줄이다 — 군집 수는 fixture 의 건수 있는 패턴 수와 k 중 작은 것."""
    # given
    client, t = replay(TEMPLATES_FIXTURE)
    # when
    ev, _ = logs.templates(client, LOG_SELECTOR, _hour(t.at), k=5)
    lines = cli.templates_lines(ev.value, window_of(ev))
    # then
    counted = [p for p in _recorded_patterns() if any(s.count > 0 for s in p.samples)]
    assert len(ev.value.clusters) == min(5, len(counted))
    assert len(lines) == 1 + 2 * len(ev.value.clusters) + (1 if ev.value.other else 0)
    assert_snapshot(request, lines)
    t.assert_exhausted()


def test_templates_lines_empty(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """빈 창의 templates 렌더링은 "데이터 없음" 한 줄이다."""
    # given
    client, t = replay("logql-templates-empty.json")
    # when
    ev, _ = logs.templates(client, EMPTY_LOG_SELECTOR, _hour(t.at))
    lines = cli.templates_lines(ev.value, window_of(ev))
    # then
    assert len(lines) == 1
    assert lines[0].startswith("(데이터 없음")
    assert_snapshot(request, lines)


# ── around ───────────────────────────────────────────────────────────────────────

def test_around_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """around 의 렌더링은 헤더(총 · 보임 · 앞/뒤와 잘림) 한 줄 뒤에 보인 줄마다 한 줄이고, 보인 줄은 ts 앞뒤에 걸쳐 있다."""
    # given — 녹화는 ts = at − 30m · span 1m · limit 20
    client, t = replay("logql-around-careerhub-prod.json")
    ts = t.at - 30 * 60
    # when
    ev, _ = logs.around(client, LOG_SELECTOR, ts, 60.0, limit=20)
    lines = cli.around_lines(ev.value, window_of(ev))
    # then
    v = ev.value
    assert len(lines) == 1 + v.shown
    assert "총 %d" % v.total in lines[0]
    assert "보임 %d" % v.shown in lines[0]
    assert any(e.ts_ns < int(ts * 1e9) for e in v.entries)
    assert any(e.ts_ns >= int(ts * 1e9) for e in v.entries)
    assert_snapshot(request, lines)
    t.assert_exhausted()


def test_around_lines_empty(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """빈 창의 around 렌더링은 "데이터 없음" 한 줄이다."""
    # given
    client, t = replay("logql-around-empty.json")
    # when
    ev, _ = logs.around(client, EMPTY_LOG_SELECTOR, t.at - 30 * 60, 60.0, limit=20)
    lines = cli.around_lines(ev.value, window_of(ev))
    # then
    assert len(lines) == 1
    assert lines[0].startswith("(데이터 없음")
    assert_snapshot(request, lines)


# ── first_seen ───────────────────────────────────────────────────────────────────

def test_first_seen_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """templates 의 첫 군집 템플릿을 그대로 넣은 first_seen 의 렌더링은 "첫 발생" 으로 시작하고, 그 시각은 그 줄을 센 sample 구간(first_ts 부터 step 폭) 안에 있다."""
    # given — 첫 군집은 templates fixture 의 patterns 응답에서 유도한다(first_seen fixture 는 그 실행의 예시 호출)
    step = auto_step(3600)
    first = logs.clusters_of(_recorded_patterns(), 5, step).clusters[0]
    client, t = replay("logql-first-seen-careerhub-prod.json")
    # when
    ev, _ = logs.first_seen(client, LOG_SELECTOR, first.template, _hour(t.at))
    lines = cli.first_seen_lines(ev.value, window_of(ev))
    # then
    assert lines[0].startswith("첫 발생")
    assert ev.value.ts is not None
    assert first.first_ts <= ev.value.ts
    assert ev.value.ts < first.first_ts + parse_duration(step)
    assert_snapshot(request, lines)
    t.assert_exhausted()


def test_first_seen_lines_absent(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """창 안에 없는 템플릿의 first_seen 렌더링은 "창 안에 없음" 으로 시작한다."""
    # given
    client, t = replay("logql-first-seen-empty.json")
    # when
    ev, _ = logs.first_seen(client, EMPTY_LOG_SELECTOR, FIRST_SEEN_TEMPLATE, _hour(t.at))
    lines = cli.first_seen_lines(ev.value, window_of(ev))
    # then
    assert lines[0].startswith("창 안에 없음")
    assert_snapshot(request, lines)
