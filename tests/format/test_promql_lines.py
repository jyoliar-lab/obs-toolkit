"""promql 의 출력 — 녹화 응답의 replay 로 즉시 조회 · 구간 요약 · --points 의 줄 모양과 파서 형상을 본다."""
from __future__ import annotations

from obs.adapters import cli
from obs.core import Evidence, Lines
from tests.conftest import ReplayFactory, _no_physical, load_response


def test_promql_instant_up(replay: ReplayFactory) -> None:
    """AC 3 의 첫 실측 `up` — 녹화 응답이 Evidence[Lines] 로 옮겨지고 시계열마다 `값  {레이블}` 한 줄이며, 나간 URL 에 query·time 이 있고 출력에 물리 정보가 없다."""
    client, t = replay("promql-instant-up.json")
    ev, _, kind = cli.promql_run(client, cli.PromqlArgs(query="up"), at=t.at)
    assert isinstance(ev, Evidence)
    assert isinstance(ev.value, Lines)
    assert ev.backend == "metrics"
    assert ev.query == "up"
    assert kind == "promql"
    lines = ev.value.lines
    assert len(lines) == 65
    assert all(l.startswith("1  {") for l in lines)
    assert "job=kubelet" in lines[0]   # 2026-09-22 재녹화: 65 타깃 전부 up
    assert "query=up" in t.calls[0].url
    assert "time=%s" % t.at in t.calls[0].url
    _no_physical("\n".join(lines))


def test_promql_range_summary(replay: ReplayFactory) -> None:
    """--range 구간 조회 — 창 길이가 계산되고 step 이 자동(range/60, 최소 15s)으로 붙으며, 시계열이 `n min max first last` 한 줄로 요약된다."""
    client, t = replay("promql-range-sum-up.json")
    ev, _, kind = cli.promql_run(client, cli.PromqlArgs(query="sum(up)", range_="30m"), at=t.at)
    assert kind == "promql-range"
    assert ev.window is not None
    assert ev.window.seconds == 1800.0
    assert ev.value.lines == ["n=61  min=65  max=65  first=65  last=65  {}"]   # 30m/30s → 61 점 (재녹화)
    assert "step=30s" in t.calls[0].url
    assert "start=%s" % (t.at - 1800.0) in t.calls[0].url


def test_promql_points(replay: ReplayFactory) -> None:
    """--points — 요약 대신 레이블 줄 아래 시각별 값이 전부 나온다(많으니 보통 spill 로 떨어지는 형태)."""
    client, t = replay("promql-range-sum-up.json")
    ev, _, _ = cli.promql_run(client, cli.PromqlArgs(query="sum(up)", range_="30m", points=True), at=t.at)
    assert ev.value.lines[0] == "{}"
    assert len(ev.value.lines) == 62
    assert ev.value.lines[1].strip().endswith("65")   # 레이블 줄 + 61 점


def test_parse_metric_vector_from_recording() -> None:
    """녹화한 vector 응답이 MetricResult 로 옮겨진다 — 시계열마다 값 하나."""
    res = cli.parse_metric(cli.check_status(load_response("promql-instant-up.json"), "metrics"))
    assert res.result_type == "vector"
    assert res.scalar is None
    assert len(res.series) == 65
    assert all(len(s.values) == 1 and s.values[0][1] == "1" for s in res.series)
