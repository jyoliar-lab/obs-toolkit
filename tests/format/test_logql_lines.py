"""logql 의 출력 — 녹화 응답의 replay 로 스트림 · matrix · --width 의 줄 모양을 본다."""
from __future__ import annotations

import re

from obs.adapters import cli
from tests.conftest import ReplayFactory, _no_physical, load_response


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
