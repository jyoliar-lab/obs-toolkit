"""promql 의 출력 — 녹화 응답의 replay 로 즉시 조회 · 구간 요약 · --points 의 줄 모양과 파서 형상을 본다."""
from __future__ import annotations

import pytest

from obs.adapters import cli
from obs.core import json_list, json_obj
from tests.conftest import ReplayFactory, assert_snapshot, load_response


def test_promql_instant_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """즉시 조회는 시계열마다 `값  {레이블}` 한 줄이다 — 줄 수는 fixture 의 시계열 수다."""
    client, t = replay("promql-instant-up.json")
    r = cli.promql_run(client, cli.PromqlArgs(query="up"), at=t.at)
    parsed = cli.parse_metric(cli.check_status(load_response("promql-instant-up.json"), "metrics"))
    assert len(r.lines()) == len(parsed.series)
    assert_snapshot(request, r.lines())


def test_promql_range_summary_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """--range 는 시계열을 `n min max first last {레이블}` 한 줄로 요약한다."""
    client, t = replay("promql-range-sum-up.json")
    r = cli.promql_run(client, cli.PromqlArgs(query="sum(up)", range_="30m"), at=t.at)
    assert_snapshot(request, r.lines())


def test_promql_points_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """--points 는 레이블 줄 아래 시각별 값을 전부 낸다."""
    client, t = replay("promql-range-sum-up.json")
    r = cli.promql_run(client, cli.PromqlArgs(query="sum(up)", range_="30m", points=True), at=t.at)
    assert_snapshot(request, r.lines())


def test_parse_metric_vector_from_recording() -> None:
    """녹화한 vector 응답은 result 항목 수만큼의 시계열로 옮겨지고 시계열마다 값이 하나다."""
    body = cli.check_status(load_response("promql-instant-up.json"), "metrics")
    res = cli.parse_metric(body)
    assert res.result_type == "vector"
    assert res.scalar is None
    assert len(res.series) == len(json_list(json_obj(body.get("data")).get("result")))
    assert all(len(s.values) == 1 for s in res.series)
