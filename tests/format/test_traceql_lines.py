"""traceql 의 출력 — 녹화 응답의 replay 로 search → trace 흐름 · tags · 오류 span 의 줄 모양을 본다."""
from __future__ import annotations

import dataclasses
import re

from obs.adapters import cli
from tests.conftest import ReplayFactory, _no_physical, load_response


def test_traceql_search_then_trace(replay: ReplayFactory) -> None:
    """search 첫 줄의 trace ID 로 trace 를 부르면 요약(총시간 · 루트 · 오류 수)이 나오고 물리 정보가 없다."""
    client, t = replay("traceql-search.json", ("traceql-trace.json", "api/traces/"))   # 순서대로: search → traces/<id>
    ev, _, kind = cli.traceql_run(client, cli.TraceqlArgs(cmd="search", since="24h", limit=3), at=t.at)
    assert kind == "traceql-search"
    assert len(ev.value.lines) == 3
    trace_id = ev.value.lines[0].split()[0]
    assert re.fullmatch(r"[0-9a-f]{31,32}", trace_id)
    assert "limit=3" in t.calls[0].url
    assert "start=%d" % (int(t.at) - 86400) in t.calls[0].url

    ev2, _, kind2 = cli.traceql_run(client, cli.TraceqlArgs(cmd="trace", arg=trace_id), at=t.at)
    lines = ev2.value.lines
    assert kind2 == "traceql-trace"
    assert lines[0].startswith("트레이스 %s  총 " % trace_id)
    assert lines[1].startswith("루트: ngf:careerhub-prod:careerhub-nginx-gateway /")
    assert lines[3] == "오류 span: 0"
    assert t.calls[1].url.endswith("api/traces/" + trace_id)
    t.assert_exhausted()
    _no_physical("\n".join(lines))


def test_traceql_tags(replay: ReplayFactory) -> None:
    """tags 는 scope 별로 `[scope] a, b, c` 한 줄이다."""
    client, t = replay("traceql-tags.json")
    ev, _, _ = cli.traceql_run(client, cli.TraceqlArgs(cmd="tags"), at=t.at)
    assert any(l.startswith("[resource] ") and "service.name" in l for l in ev.value.lines)


def test_parse_spans_error_detection() -> None:
    """span 의 상태를 STATUS_CODE_ERROR 로 바꾸면 `오류 span: 1` 과 ✗ 줄이 나온다."""
    spans = cli.parse_spans(cli.check_json(load_response("traceql-trace.json", "api/traces/"), "traces"))
    assert len(spans) == 8
    assert spans[0].service == "posting-service"
    assert not spans[0].is_error
    errored = [dataclasses.replace(spans[0], status_code="STATUS_CODE_ERROR")]
    lines = cli.trace_lines("abc", errored, top=5)
    assert lines[3] == "오류 span: 1"
    assert lines[4].startswith("  ✗ posting-service /")


def test_trace_error_span_shows_status_message() -> None:
    """오류 span 의 ✗ 줄에는 status.message 의 첫 줄(스택 제외)이 잘려 실린다."""
    spans = cli.parse_spans(cli.check_json(load_response("traceql-trace-error.json", "api/traces/"), "traces"))
    assert len(spans) == 1
    assert spans[0].is_error
    assert spans[0].status_message.lstrip().startswith("connection(")
    lines = cli.trace_lines("d47871cc20408ed84a7506ccf306200d", spans, top=5)
    assert lines[3] == "오류 span: 1"
    err = lines[4]
    assert err.startswith("  ✗ posting-service / ")
    assert "incomplete read of message header" in err
    assert "StackTrace" not in err
    assert "\t" not in err
    assert "\n" not in err


def test_trace_error_reason_falls_back_to_attrs() -> None:
    """status.message 가 비면 exception.message → error.message → http.status_code 순으로 속성에서 원인을 찾는다."""
    base = cli.parse_spans(cli.check_json(load_response("traceql-trace.json", "api/traces/"), "traces"))[0]
    s = dataclasses.replace(base, status_code="STATUS_CODE_ERROR", status_message="", attrs={**base.attrs, "exception.message": "boom"})
    assert cli.error_reason(s) == "boom"
    s = dataclasses.replace(s, status_message="\n\tStackTrace: a.go:1")
    assert cli.error_reason(s) == "boom"
    s = dataclasses.replace(s, status_message="  EOF\tStackTrace: a.go:1")
    assert cli.error_reason(s) == "EOF"
