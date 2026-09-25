"""metadata 조회의 출력 — 녹화 응답의 replay 로 promql · logql 의 --labels · --label-values · --series 와 traceql tag-values 의 줄 모양을 본다.
replay 가 나간 요청(경로 · match[] · 창)을 녹화 요청과 대조하고, 스냅샷이 모양을, fixture 에서 유도한 불변식(정렬 · 개수)이 성질을 지킨다."""
from __future__ import annotations

import pytest

from obs.adapters import cli
from tests.conftest import ReplayFactory, assert_snapshot, load_response
from tests.record import LOG_SELECTOR, UP_APISERVER


def test_promql_labels_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """--labels 는 selector 에 맞는 레이블 이름을 정렬해 한 줄씩 낸다 — 줄 수는 fixture 의 이름 수다."""
    client, t = replay("promql-labels-up-apiserver.json")
    ev, _, kind = cli.promql_run(client, cli.PromqlArgs(query=UP_APISERVER, labels=True), at=t.at)
    names = cli.parse_strings(cli.check_status(load_response("promql-labels-up-apiserver.json"), "metrics"))
    assert kind == "promql-labels"
    assert ev.value.lines == sorted(names)
    assert_snapshot(request, ev.value.lines)


def test_promql_label_values_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """--label-values NAME 은 그 레이블의 값을 정렬해 한 줄씩 낸다 — 줄 수는 fixture 의 값 수다."""
    client, t = replay("promql-label-values-job.json")
    ev, _, kind = cli.promql_run(client, cli.PromqlArgs(query="up", label_values="job"), at=t.at)
    values = cli.parse_strings(cli.check_status(load_response("promql-label-values-job.json"), "metrics"))
    assert kind == "promql-label-values"
    assert ev.value.lines == sorted(values)
    assert_snapshot(request, ev.value.lines)


def test_promql_series_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """--series 는 selector 에 맞는 시계열의 레이블 집합을 `{k=v, …}` 한 줄씩 낸다 — 줄 수는 fixture 의 시계열 수다."""
    client, t = replay("promql-series-up-apiserver.json")
    ev, _, kind = cli.promql_run(client, cli.PromqlArgs(series=UP_APISERVER), at=t.at)
    sets = cli.parse_label_sets(cli.check_status(load_response("promql-series-up-apiserver.json"), "metrics"))
    assert kind == "promql-series"
    assert len(ev.value.lines) == len(sets)
    assert_snapshot(request, ev.value.lines)


def test_logql_labels_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """logql --labels 는 창 안의 레이블 이름을 정렬해 한 줄씩 낸다."""
    client, t = replay("logql-labels.json")
    ev, _, kind = cli.logql_run(client, cli.LogqlArgs(labels=True), at=t.at)
    names = cli.parse_strings(cli.check_status(load_response("logql-labels.json"), "logs"))
    assert kind == "logql-labels"
    assert ev.value.lines == sorted(names)
    assert_snapshot(request, ev.value.lines)


def test_logql_label_values_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """logql --label-values NAME 은 창 안의 그 레이블 값을 정렬해 한 줄씩 낸다."""
    client, t = replay("logql-label-values-namespace.json")
    ev, _, kind = cli.logql_run(client, cli.LogqlArgs(label_values="namespace"), at=t.at)
    values = cli.parse_strings(cli.check_status(load_response("logql-label-values-namespace.json"), "logs"))
    assert kind == "logql-label-values"
    assert ev.value.lines == sorted(values)
    assert_snapshot(request, ev.value.lines)


def test_logql_series_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """logql --series 는 selector 에 맞는 스트림의 레이블 집합을 `{k=v, …}` 한 줄씩 낸다 — 줄 수는 fixture 의 스트림 수다."""
    client, t = replay("logql-series-careerhub-prod.json")
    ev, _, kind = cli.logql_run(client, cli.LogqlArgs(series=LOG_SELECTOR), at=t.at)
    sets = cli.parse_label_sets(cli.check_status(load_response("logql-series-careerhub-prod.json"), "logs"))
    assert kind == "logql-series"
    assert len(ev.value.lines) == len(sets)
    assert_snapshot(request, ev.value.lines)


def test_traceql_tag_values_lines(replay: ReplayFactory, request: pytest.FixtureRequest) -> None:
    """tag-values TAG 는 그 태그의 값을 정렬해 한 줄씩 낸다 — 줄 수는 fixture 의 값 수다."""
    client, t = replay("traceql-tag-values-service-name.json")
    ev, _, kind = cli.traceql_run(client, cli.TraceqlArgs(cmd="tag-values", arg="resource.service.name", since="24h"), at=t.at)
    values = cli.parse_tag_values(cli.check_json(load_response("traceql-tag-values-service-name.json"), "traces"))
    assert kind == "traceql-tag-values"
    assert ev.value.lines == sorted(values)
    assert_snapshot(request, ev.value.lines)
