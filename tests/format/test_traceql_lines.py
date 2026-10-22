"""traceql 의 출력 — 녹화 응답의 replay 로 search · trace · tags · 오류 trace 의 줄 모양을 본다."""
from __future__ import annotations

import pytest

from obs.adapters import cli
from tests.conftest import ReplayFactory, assert_snapshot, load_call, load_response


def test_traceql_search_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """search 는 hit 마다 한 줄이고 첫 열이 trace ID 다 — 첫 줄의 ID 는 fixture 의 첫 hit 이다."""
    client, t = replay("traceql-search.json")
    r = cli.traceql_run(client, cli.TraceqlArgs(cmd="search", since="24h", limit=3), at=t.at)
    hits = cli.parse_search(cli.check_json(load_response("traceql-search.json"), "traces"))
    assert r.lines()[0].split()[0] == hits[0].trace_id
    assert_snapshot(request, r.lines())


def test_traceql_trace_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """trace 는 총시간 · 루트 · 서비스 · 오류 수 · 느린 span 순의 요약이다."""
    client, t = replay(("traceql-trace.json", "api/traces/"))
    trace_id = load_call(("traceql-trace.json", "api/traces/")).call.request.path.rsplit("/", 1)[1]
    r = cli.traceql_run(client, cli.TraceqlArgs(cmd="trace", arg=trace_id), at=t.at)
    assert_snapshot(request, r.lines())


def test_traceql_error_trace_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """오류 trace 는 `오류 span: N` 아래 ✗ 줄에 원인 한 줄을 싣는다."""
    client, _ = replay("traceql-trace-error.json", unchecked="recorded: manual — 요청이 추정이라 비교할 녹화 키가 없다")
    trace_id = load_call("traceql-trace-error.json").call.request.path.rsplit("/", 1)[1]
    r = cli.traceql_run(client, cli.TraceqlArgs(cmd="trace", arg=trace_id), at=0.0)
    assert_snapshot(request, r.lines())


def test_traceql_tags_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """tags 는 scope 별로 `[scope] a, b, c` 한 줄이다."""
    client, t = replay("traceql-tags.json")
    r = cli.traceql_run(client, cli.TraceqlArgs(cmd="tags"), at=t.at)
    assert_snapshot(request, r.lines())
