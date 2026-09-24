"""logql 의 출력 — 녹화 응답의 replay 로 스트림 · matrix 의 줄 모양을 본다."""
from __future__ import annotations

import pytest

from obs.adapters import cli
from tests.conftest import ReplayFactory, assert_snapshot, load_response


def test_logql_stream_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """스트림 출력은 공통 레이블 한 줄 뒤에 로그 줄이 최신순이다 — 줄 수는 fixture 의 값 수 합 + 1 이다."""
    client, t = replay("logql-streams-careerhub-prod.json")
    ev, _, _ = cli.logql_run(client, cli.LogqlArgs(query='{namespace="careerhub-prod"}', limit=5), at=t.at)
    lines = ev.value.lines
    parsed = cli.parse_logs(cli.check_json(load_response("logql-streams-careerhub-prod.json"), "logs"))
    assert len(lines) == 1 + sum(len(st.values) for st in parsed.streams)
    assert lines[1][:14] >= lines[-1][:14]   # backward — 최신이 먼저
    assert_snapshot(request, lines)


def test_logql_matrix_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """메트릭형 LogQL 은 matrix 를 알아보고 시계열 요약 줄로 낸다."""
    client, t = replay("logql-matrix-count-over-time.json")
    ev, _, _ = cli.logql_run(client, cli.LogqlArgs(query="sum by (detected_level) (count_over_time({namespace=\"careerhub-prod\"}[5m]))"), at=t.at)
    assert_snapshot(request, ev.value.lines)
