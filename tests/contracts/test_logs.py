"""로그 환원 연산의 약속 — 질의 조립 · 응답 → 계약 · 없음/0 판정 · 상한 · 거부 · 나간 요청 · 디스패치. 손 입력과 성질 단언만.
렌더링의 모양은 format/test_logs_lines.py. around 의 손 입력은 Loki 처럼 창 · 방향 · limit 을 적용하는 FakeLoki(conftest)다."""
from __future__ import annotations

import json
import urllib.parse
from typing import Mapping

import pytest

from obs import logs
from obs.adapters import cli
from obs.core import JSON, Client, Config, ObsError, Window
from obs.logs import LogEntry, Sample, Side
from tests.conftest import AT, MINUTE, ClientFactory, LokiFactory, _vector
from tests.record import strip_physical
from tests.replay import Call

SEL: str = '{namespace="n"}'
W: Window = Window.last("1h", at=AT)
TS: float = AT - 30 * MINUTE          # around 의 중심 시각
PATTERNS: str = "loki/api/v1/patterns"
RANGE: str = "loki/api/v1/query_range"
INSTANT: str = "loki/api/v1/query"


def _params(call: Call) -> dict[str, str]:
    """나간 URL 의 query 파라미터 — 값은 문자열 그대로."""
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call.url).query, keep_blank_values=True))


def _path(call: Call) -> str:
    return urllib.parse.urlsplit(call.url).path


def _ns(sec: float) -> int:
    return int(sec * 1e9)


def _matrix(*series: tuple[dict[str, str], list[tuple[float, str]]]) -> dict[str, JSON]:
    """Loki 메트릭 질의(matrix) 응답을 손으로 — (레이블, [(초, 값)…]) 목록."""
    result: list[JSON] = [{"metric": dict(labels), "values": [[ts, v] for ts, v in values]} for labels, values in series]
    return {"status": "success", "data": {"resultType": "matrix", "result": result}}


def _streams(*streams: tuple[dict[str, str], list[tuple[int, str]]]) -> dict[str, JSON]:
    """Loki 로그 질의(streams) 응답을 손으로 — (레이블, [(ns, 줄)…]) 목록."""
    result: list[JSON] = [{"stream": dict(labels), "values": [[str(ts), line] for ts, line in values]} for labels, values in streams]
    return {"status": "success", "data": {"resultType": "streams", "result": result}}


def _patterns(*items: tuple[str, list[tuple[int, int]]]) -> dict[str, JSON]:
    """Patterns API 응답을 손으로 — (패턴, [(초, 건수)…]) 목록."""
    data: list[JSON] = [{"pattern": p, "samples": [[ts, n] for ts, n in samples]} for p, samples in items]
    return {"status": "success", "data": data}


def _entry(offset_sec: float, line: str, labels: dict[str, str] | None = None) -> LogEntry:
    """around 의 세상 — 중심 시각 TS 에서 offset 초 떨어진 줄."""
    return LogEntry(ts_ns=_ns(TS + offset_sec), labels=labels or {"pod": "p"}, line=line)


# ── pattern_filter — templates(예시) · first_seen 이 쓰는 LogQL 패턴 필터 ─────────────

def test_pattern_filter_uses_backtick_literal_verbatim() -> None:
    """백틱이 없는 패턴은 백틱 리터럴로 감싸고 안은 그대로다 — 따옴표 · 역슬래시를 escape 하지 않는다."""
    # given — 따옴표 · 역슬래시가 든 패턴
    pattern = '{"msg":"<_>"} \\ <_>'
    # when
    q = logs.pattern_filter(SEL, pattern)
    # then
    assert q == SEL + " |> `" + pattern + "`"


def test_pattern_filter_quotes_and_escapes_when_pattern_has_backtick() -> None:
    """백틱이 든 패턴은 따옴표 리터럴로 — 역슬래시와 따옴표를 escape 하고 백틱은 그대로다."""
    # given
    pattern = 'x`y "z" \\'
    # when
    q = logs.pattern_filter(SEL, pattern)
    # then
    assert q == SEL + ' |> "x`y \\"z\\" \\\\"'


def test_pattern_filter_rejects_blank() -> None:
    """공백뿐인 패턴은 ObsError 다."""
    # given · when · then
    with pytest.raises(ObsError):
        logs.pattern_filter(SEL, " ")


# ── LogsArgs.validate — 순수 검사 ────────────────────────────────────────────────────

def test_logs_args_validate_rejects_unknown_subcommand() -> None:
    """모르는 cmd 는 validate 가 ObsError 로 거부한다."""
    # given
    a = cli.LogsArgs(cmd="red", selector=SEL)
    # when · then
    with pytest.raises(ObsError):
        a.validate()


@pytest.mark.parametrize("a", [cli.LogsArgs(cmd="around", selector=SEL, ts=None), cli.LogsArgs(cmd="first-seen", selector=SEL, template=None)],
                         ids=["around", "first-seen"])
def test_logs_args_validate_requires_ts_and_template(a: cli.LogsArgs) -> None:
    """around 에 ts 가 없거나 first-seen 에 template 이 없으면 validate 가 ObsError 다."""
    # given — 필수 필드가 None 인 Args · when · then
    with pytest.raises(ObsError):
        a.validate()


# ── histogram_query ─────────────────────────────────────────────────────────────────

def test_histogram_query_shape() -> None:
    """histogram_query 는 sum by (<by>) (count_over_time(<selector>[<step>])) 다."""
    # given · when
    q = logs.histogram_query('{a="b"}', ["detected_level"], "60s")
    # then
    assert q == 'sum by (detected_level) (count_over_time({a="b"}[60s]))'


def test_histogram_query_joins_axes_and_trims() -> None:
    """축이 여럿이면 쉼표로 잇고 공백은 다듬되 이름은 그대로다 — 별칭이 없다."""
    # given · when
    q = logs.histogram_query('{a="b"}', ["detected_level", " app "], "60s")
    # then
    assert q == 'sum by (detected_level, app) (count_over_time({a="b"}[60s]))'


def test_histogram_query_requires_axis() -> None:
    """공백뿐인 축은 ObsError 다."""
    # given · when · then
    with pytest.raises(ObsError):
        logs.histogram_query('{a="b"}', ["", " "], "60s")


# ── histogram — query_range GET 1회 → 표 ────────────────────────────────────────────

def test_histogram_groups_totals_and_times(make_client: ClientFactory) -> None:
    """행마다 total 은 값의 합, counts 는 times 와 같은 길이로 점 없던 구간이 0 이며, 행은 total 내림차순이다."""
    # given — 시계열 둘, 하나는 구간 하나(t1)에 점이 없다
    body = _matrix(({"detected_level": "info"}, [(100.0, "1"), (160.0, "2"), (220.0, "3")]),
                   ({"detected_level": "error"}, [(100.0, "5"), (220.0, "5")]))
    client, _ = make_client({("logs", RANGE): (200, body)})
    # when
    ev = logs.histogram(client, SEL, W)
    # then
    h = ev.value
    assert h.times == [100.0, 160.0, 220.0]
    assert [g.labels["detected_level"] for g in h.groups] == ["error", "info"]
    assert [g.total for g in h.groups] == [10, 6]
    assert h.groups[0].counts == [5, 0, 5]
    assert h.groups[1].counts == [1, 2, 3]
    for g in h.groups:
        assert len(g.counts) == len(h.times)


def test_histogram_sends_one_metric_range_query(make_client: ClientFactory) -> None:
    """histogram 은 query_range 에 메트릭 질의를 한 번 보낸다 — step 은 있고 limit 은 없다(줄을 받지 않는다)."""
    # given
    client, t = make_client({("logs", RANGE): (200, _matrix())})
    # when
    logs.histogram(client, SEL, W)
    # then
    assert len(t.calls) == 1
    assert _path(t.calls[0]).endswith(RANGE)
    params = _params(t.calls[0])
    assert params["step"] == "60s"
    assert "limit" not in params


def test_histogram_sends_assembled_query_with_auto_step(make_client: ClientFactory) -> None:
    """histogram 은 자동 step 으로 조립한 질의를 보내고 Histogram 의 step 은 그 자동값이다."""
    # given
    client, _ = make_client({("logs", RANGE): (200, _matrix())})
    # when
    ev = logs.histogram(client, SEL, W)
    # then
    assert ev.value.step == "60s"


def test_histogram_folds_groups_beyond_cap(make_client: ClientFactory) -> None:
    """행이 max_groups 를 넘으면 total 상위만 남기고 뺀 행 수를 folded 에 둔다."""
    # given — 시계열 다섯
    body = _matrix(*[({"pod": "p%d" % i}, [(100.0, str(i * 10))]) for i in range(1, 6)])
    client, _ = make_client({("logs", RANGE): (200, body)})
    # when
    ev = logs.histogram(client, SEL, W, by=["pod"], max_groups=3)
    # then
    assert len(ev.value.groups) == 3
    assert [g.total for g in ev.value.groups] == [50, 40, 30]
    assert ev.value.folded == 2


def test_histogram_empty_is_no_data(make_client: ClientFactory) -> None:
    """빈 matrix 는 groups 가 빈 목록이고 folded 는 0 이다 — 데이터 없음."""
    # given
    client, _ = make_client({("logs", RANGE): (200, _matrix())})
    # when
    ev = logs.histogram(client, SEL, W)
    # then
    assert ev.value.groups == []
    assert ev.value.folded == 0


def test_histogram_zero_is_not_no_data(make_client: ClientFactory) -> None:
    """값이 전부 0 인 시계열은 total 0 인 행으로 실린다 — 없음이 아니라 0 이다."""
    # given
    client, _ = make_client({("logs", RANGE): (200, _matrix(({"detected_level": "error"}, [(100.0, "0"), (160.0, "0")])))})
    # when
    ev = logs.histogram(client, SEL, W)
    # then
    assert len(ev.value.groups) == 1
    assert ev.value.groups[0].total == 0


def test_histogram_rejects_zero_groups_cap(make_client: ClientFactory) -> None:
    """max_groups 가 0 이면 질의 전에 ObsError 다."""
    # given
    client, t = make_client()
    # when · then
    with pytest.raises(ObsError):
        logs.histogram(client, SEL, W, max_groups=0)
    assert t.calls == []


# ── parse_patterns — Patterns API 응답 → Pattern ───────────────────────────────────

def test_parse_patterns_shape() -> None:
    """패턴마다 Pattern 하나 — samples 는 Sample(ts, count) 목록이고 응답 순서 그대로다."""
    # given
    body = _patterns(("a <_>", [(100, 2), (110, 0)]), ("b <_>", [(100, 7)]))
    # when
    pats = logs.parse_patterns(body)
    # then
    assert [p.pattern for p in pats] == ["a <_>", "b <_>"]
    assert pats[0].samples == [Sample(100.0, 2), Sample(110.0, 0)]
    assert pats[1].samples == [Sample(100.0, 7)]


def test_parse_patterns_empty() -> None:
    """data 가 비면 빈 목록이다."""
    # given · when · then
    assert logs.parse_patterns(_patterns()) == []


@pytest.mark.parametrize("sample", [["x", 1], [1]], ids=["not_number", "not_pair"])
def test_parse_patterns_rejects_malformed_samples(sample: list[JSON]) -> None:
    """[ts, count] 꼴이 아닌 sample 이 섞이면 조용히 빼지 않고 ObsError 다 — 문구가 Patterns API 와 형식을 말하고 사람에게로 끝난다."""
    # given
    body: dict[str, JSON] = {"status": "success", "data": [{"pattern": "a <_>", "samples": [[100, 1], sample]}]}
    # when · then
    with pytest.raises(ObsError) as ei:
        logs.parse_patterns(body)
    assert "Patterns API" in str(ei.value)
    assert "형식" in str(ei.value)
    assert "사람에게" in str(ei.value)


# ── clusters_of — Pattern → Templates (계산 없이 판정만) ─────────────────────────────

P1: logs.Pattern = logs.Pattern("one <_>", [Sample(10.0, 0), Sample(20.0, 3), Sample(30.0, 2), Sample(40.0, 0)])
P2: logs.Pattern = logs.Pattern("two <_>", [Sample(10.0, 4), Sample(20.0, 0)])
P3: logs.Pattern = logs.Pattern("three <_>", [Sample(30.0, 1)])


def test_clusters_of_counts_ratio_first_last() -> None:
    """군집마다 count 는 samples 의 합, ratio 는 전체 합 대비, first/last 는 건수 > 0 인 첫/마지막 시각이고 example 은 None 이다."""
    # given — 패턴 셋, samples 에 0 인 구간이 섞여 있다
    # when
    t = logs.clusters_of([P1, P2, P3], 10, "60s")
    # then — 세 군집 모두: (count, ratio, first_ts, last_ts)
    expected = {"one <_>": (5, 0.5, 20.0, 30.0), "two <_>": (4, 0.4, 10.0, 10.0), "three <_>": (1, 0.1, 30.0, 30.0)}
    by_name = {c.template: c for c in t.clusters}
    assert t.total == 10
    assert set(by_name) == set(expected)
    for name, (count, ratio, first_ts, last_ts) in expected.items():
        c = by_name[name]
        assert c.count == count, name
        assert c.ratio == pytest.approx(ratio), name
        assert c.first_ts == first_ts, name
        assert c.last_ts == last_ts, name
        assert c.example is None, name


def test_clusters_of_is_order_independent() -> None:
    """같은 패턴 셋을 순서만 바꿔 넣어도 Templates 가 같다 — 출력 순서는 입력이 아니라 내용이 정한다."""
    # given — 같은 셋의 두 순열
    # when
    a = logs.clusters_of([P1, P2, P3], 10, "60s")
    b = logs.clusters_of([P3, P1, P2], 10, "60s")
    # then
    assert a == b


def test_clusters_of_sorts_by_count_then_template() -> None:
    """군집은 count 내림차순이고 count 가 같으면 template 사전순이다."""
    # given — count 3 이 둘(b · a), count 5 가 하나
    pats = [logs.Pattern("b <_>", [Sample(10.0, 3)]), logs.Pattern("c <_>", [Sample(10.0, 5)]), logs.Pattern("a <_>", [Sample(10.0, 3)])]
    # when
    t = logs.clusters_of(pats, 10, "60s")
    # then
    assert [c.template for c in t.clusters] == ["c <_>", "a <_>", "b <_>"]


def test_clusters_of_caps_at_k_and_counts_rest() -> None:
    """k 를 넘는 패턴은 군집에서 빠지고 그 건수 합이 other 다 — total 은 전체 합이다."""
    # given — 패턴 다섯(50 · 40 · 30 · 20 · 10)
    pats = [logs.Pattern("p%d <_>" % n, [Sample(10.0, n)]) for n in (50, 40, 30, 20, 10)]
    # when
    t = logs.clusters_of(pats, 2, "60s")
    # then
    assert [c.count for c in t.clusters] == [50, 40]
    assert t.other == 60
    assert t.total == 150


@pytest.mark.parametrize("pats", [[], [logs.Pattern("z <_>", [Sample(10.0, 0), Sample(20.0, 0)])]], ids=["none", "all_zero"])
def test_clusters_of_empty(pats: list[logs.Pattern]) -> None:
    """패턴이 없거나 samples 가 전부 0 이면 군집이 비고 total · other 는 0 이다."""
    # given · when
    t = logs.clusters_of(pats, 10, "60s")
    # then
    assert t.clusters == []
    assert t.total == 0
    assert t.other == 0


# ── templates — patterns GET 1회 + 군집마다 예시 1회 ──────────────────────────────────

THREE: dict[str, JSON] = _patterns(("a <_>", [(100, 6)]), ("b <_>", [(100, 3)]), ("c <_>", [(100, 1)]))
EXAMPLE: dict[str, JSON] = _streams(({"pod": "p"}, [(100_000_000_000, "example line")]))


def test_templates_clusters_with_examples(make_client: ClientFactory) -> None:
    """군집마다 count · ratio 와 예시 응답의 첫 줄이 실린다."""
    # given — 패턴 셋 · 예시 응답
    client, _ = make_client({("logs", PATTERNS): (200, THREE), ("logs", RANGE): (200, EXAMPLE)})
    # when
    ev = logs.templates(client, SEL, W)
    # then
    t = ev.value
    assert len(t.clusters) == 3
    assert t.total == 10
    for c in t.clusters:
        assert c.ratio == pytest.approx(c.count / 10)
        assert c.example == "example line"


def test_templates_requests_patterns_then_one_example_per_cluster(make_client: ClientFactory) -> None:
    """요청은 군집 목록 1회 + 군집마다 예시 1회다 — patterns 에 start · end · step 이 있고 limit 은 없으며, 예시 질의는 군집마다 그 패턴의
    pattern_filter 질의(중복 없이 셋 다) · forward · limit 1 이다."""
    # given
    client, t = make_client({("logs", PATTERNS): (200, THREE), ("logs", RANGE): (200, EXAMPLE)})
    # when
    logs.templates(client, SEL, W, k=10)
    # then
    assert len(t.calls) == 4
    assert _path(t.calls[0]).endswith(PATTERNS)
    first = _params(t.calls[0])
    assert first["query"] == SEL
    assert "start" in first
    assert "end" in first
    assert "step" in first
    assert "limit" not in first
    examples = [_params(c) for c in t.calls[1:]]
    for call in t.calls[1:]:
        assert _path(call).endswith(RANGE)
    assert sorted(e["query"] for e in examples) == sorted(logs.pattern_filter(SEL, p) for p in ("a <_>", "b <_>", "c <_>"))
    for e in examples:
        assert e["direction"] == "forward"
        assert e["limit"] == "1"


def test_templates_logs_patterns_then_one_example_per_cluster(make_client: ClientFactory) -> None:
    """templates 뒤 client.log 는 1 + k — patterns 응답 하나 뒤에 군집마다 그 패턴(pattern_filter)의 예시 요청과 응답이 순서대로."""
    # given — 군집 셋의 patterns 응답 · 예시 응답
    client, _ = make_client({("logs", PATTERNS): (200, THREE), ("logs", RANGE): (200, EXAMPLE)})
    # when
    ev = logs.templates(client, SEL, W)
    # then
    assert len(ev.value.clusters) == 3
    assert len(client.log) == 1 + len(ev.value.clusters)
    assert (client.log[0].path, client.log[0].body) == (PATTERNS, THREE)
    assert [r.path for r in client.log[1:]] == [RANGE] * len(ev.value.clusters)
    assert [r.body for r in client.log[1:]] == [EXAMPLE] * len(ev.value.clusters)
    assert [r.params["query"] for r in client.log[1:]] == [logs.pattern_filter(SEL, c.template) for c in ev.value.clusters]


def test_templates_logs_patterns_only_when_no_clusters(make_client: ClientFactory) -> None:
    """패턴이 없으면 templates 뒤 client.log 는 patterns 응답 하나뿐이다."""
    # given
    body = _patterns()
    client, _ = make_client({("logs", PATTERNS): (200, body)})
    # when
    logs.templates(client, SEL, W)
    # then
    assert len(client.log) == 1
    assert client.log[0].path == PATTERNS
    assert client.log[0].body == body


def test_templates_explicit_step_overrides_auto(make_client: ClientFactory) -> None:
    """step 을 주면 자동값 대신 그 step 이 요청과 Templates.step 에 쓰인다."""
    # given
    client, t = make_client({("logs", PATTERNS): (200, THREE), ("logs", RANGE): (200, EXAMPLE)})
    # when
    logs.templates(client, SEL, W, step="30s")
    # then
    assert _params(t.calls[0])["step"] == "30s"


def test_templates_asks_examples_only_for_top_k(make_client: ClientFactory) -> None:
    """예시 질의는 남긴 군집 수만큼이다 — k 를 넘어 버린 패턴에는 묻지 않는다."""
    # given — 패턴 다섯
    five = _patterns(*[("p%d <_>" % n, [(100, n)]) for n in (50, 40, 30, 20, 10)])
    client, t = make_client({("logs", PATTERNS): (200, five), ("logs", RANGE): (200, EXAMPLE)})
    # when
    logs.templates(client, SEL, W, k=2)
    # then — 예시 질의는 상위 둘(50 · 40)의 것뿐이다
    assert len(t.calls) == 3
    assert sorted(_params(c)["query"] for c in t.calls[1:]) == sorted(logs.pattern_filter(SEL, p) for p in ("p50 <_>", "p40 <_>"))


def test_templates_example_missing_is_none(config: Config) -> None:
    """한 군집의 예시 질의만 빈 스트림이면 그 군집의 example 만 None 이고 나머지는 차며 군집 수는 그대로다."""
    # given — 'b <_>' 의 예시 질의만 빈 스트림, 나머지 예시 질의는 한 줄 (질의별 응답 — 경로 단위 라우트로는 가를 수 없다)
    empty_query = logs.pattern_filter(SEL, "b <_>")

    def transport(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        rq = strip_physical(url, config, None)
        body: JSON = THREE if rq.path == PATTERNS else (_streams() if rq.params.get("query") == empty_query else EXAMPLE)
        return 200, json.dumps(body).encode("utf-8")

    client = Client(config=config, token="test-token-not-real", transport=transport)
    # when
    ev = logs.templates(client, SEL, W)
    # then
    assert len(ev.value.clusters) == 3
    assert {c.template: c.example is None for c in ev.value.clusters} == {"a <_>": False, "b <_>": True, "c <_>": False}


def test_templates_backtick_pattern_still_gets_example(make_client: ClientFactory) -> None:
    """백틱이 든 패턴도 거부하지 않는다 — 예시 질의가 따옴표 꼴로 나가고 그 군집의 example 도 찬다."""
    # given — 패턴 셋, 그중 하나에 백틱
    body = _patterns(("a <_>", [(100, 6)]), ("x`y <_>", [(100, 3)]), ("c <_>", [(100, 1)]))
    client, t = make_client({("logs", PATTERNS): (200, body), ("logs", RANGE): (200, EXAMPLE)})
    # when
    ev = logs.templates(client, SEL, W)
    # then
    assert len(t.calls) == 4
    quoted = [_params(c)["query"] for c in t.calls[1:] if "x`y" in _params(c)["query"]]
    assert quoted == [SEL + ' |> "x`y <_>"']
    assert {c.template: c.example for c in ev.value.clusters}["x`y <_>"] == "example line"


def test_templates_empty_window_asks_no_example(make_client: ClientFactory) -> None:
    """패턴이 없으면 요청은 patterns 1회뿐이다 — 예시를 묻지 않는다."""
    # given
    client, t = make_client({("logs", PATTERNS): (200, _patterns())})
    # when
    logs.templates(client, SEL, W)
    # then
    assert len(t.calls) == 1


def test_templates_rejects_zero_k(make_client: ClientFactory) -> None:
    """k 가 0 이면 질의 전에 ObsError 다."""
    # given
    client, t = make_client()
    # when · then
    with pytest.raises(ObsError):
        logs.templates(client, SEL, W, k=0)
    assert t.calls == []


def test_templates_patterns_404_points_to_pattern_ingester(make_client: ClientFactory) -> None:
    """patterns 가 404 면 ObsError 의 문구가 Patterns API · pattern_ingester · 사람에게 를 말한다 — 예시는 묻지 않는다."""
    # given
    client, t = make_client({("logs", PATTERNS): (404, {"message": "not found"})})
    # when · then
    with pytest.raises(ObsError) as ei:
        logs.templates(client, SEL, W)
    assert "Patterns API" in str(ei.value)
    assert "pattern_ingester" in str(ei.value)
    assert "사람에게" in str(ei.value)
    assert len(t.calls) == 1


# ── count_query · around ────────────────────────────────────────────────────────────

def test_count_query_shape() -> None:
    """count_query 는 sum(count_over_time(<selector>[<n>s])) 다."""
    # given · when · then
    assert logs.count_query('{a="b"}', 600) == 'sum(count_over_time({a="b"}[600s]))'


def test_around_reports_total_and_shown_per_side(make_loki: LokiFactory) -> None:
    """창 전체 건수와 쪽별 보인 수 · 잘림이 실린다 — 앞은 limit 에 잘리고 뒤는 전부다."""
    # given — 창 안에 앞 6줄 · 뒤 1줄
    before = [_entry(-50 + i * 8, "b%d" % i) for i in range(6)]
    client, _ = make_loki([*before, _entry(10, "a0")], count=7)
    # when
    ev = logs.around(client, SEL, TS, 60, limit=2)
    # then
    v = ev.value
    assert v.before == Side(shown=2, truncated=True)
    assert v.after == Side(shown=1, truncated=False)
    assert v.total == 7
    assert v.shown == 3
    assert v.truncated


def test_around_asks_count_then_backward_and_forward(make_client: ClientFactory) -> None:
    """요청 3회 — 창 전체 count(인스턴트 · time 은 창 끝 ns) · 앞은 backward [ts−span, ts) · 뒤는 forward [ts, ts+span), 둘 다 selector 그대로다."""
    # given
    client, t = make_client({("logs", INSTANT): (200, _vector(({}, "0"))), ("logs", RANGE): (200, _streams())})
    # when
    logs.around(client, SEL, TS, 60, limit=5)
    # then
    assert len(t.calls) == 3
    by_path = {(_path(c).split("/")[-1], _params(c).get("direction", "")): _params(c) for c in t.calls}
    count = by_path[("query", "")]
    assert count["time"] == str(_ns(TS + 60))
    back = by_path[("query_range", "backward")]
    assert back["query"] == SEL
    assert back["start"] == str(_ns(TS - 60))
    assert back["end"] == str(_ns(TS))
    fwd = by_path[("query_range", "forward")]
    assert fwd["query"] == SEL
    assert fwd["start"] == str(_ns(TS))
    assert fwd["end"] == str(_ns(TS + 60))


def test_around_keeps_only_lines_inside_window_in_order(make_loki: LokiFactory) -> None:
    """entries 는 창 안의 줄만 시각 오름차순으로 — 창 밖의 줄은 실리지 않는다."""
    # given — 창 안에 앞 2 · 뒤 2, 창 밖에도 줄이 있다
    world = [_entry(-70, "out-before"), _entry(-20, "b0"), _entry(-10, "b1"), _entry(5, "a0"), _entry(15, "a1"), _entry(70, "out-after")]
    client, _ = make_loki(world, count=4)
    # when
    ev = logs.around(client, SEL, TS, 60)
    # then
    assert [e.line for e in ev.value.entries] == ["b0", "b1", "a0", "a1"]
    assert [e.ts_ns for e in ev.value.entries] == sorted(e.ts_ns for e in ev.value.entries)


def test_around_drops_lines_outside_each_side_window(make_client: ClientFactory) -> None:
    """창 밖 시각의 줄이 응답에 섞여 와도 entries 는 창 안 줄만 — 각 한 번 · 시각 오름차순(클라이언트 거름)."""
    # given — 양쪽 query_range 에 같은 정적 본문: 창 안 앞 2 · 뒤 2 와 창 밖(±70s) 둘
    body = _streams(({"pod": "p"}, [(_ns(TS - 70), "out-before"), (_ns(TS - 20), "b0"), (_ns(TS - 10), "b1"),
                                   (_ns(TS + 5), "a0"), (_ns(TS + 15), "a1"), (_ns(TS + 70), "out-after")]))
    client, _ = make_client({("logs", INSTANT): (200, _vector(({}, "4"))), ("logs", RANGE): (200, body)})
    # when
    ev = logs.around(client, SEL, TS, 60)
    # then
    assert [e.line for e in ev.value.entries] == ["b0", "b1", "a0", "a1"]


def test_around_line_at_center_from_both_sides_counts_once(make_client: ClientFactory) -> None:
    """ts 와 정확히 같은 시각의 줄이 양쪽 응답에 와도 entries 에 한 번 — 뒤에 센다."""
    # given — 양쪽 query_range 에 같은 정적 본문: ts 시각의 줄 하나
    body = _streams(({"pod": "p"}, [(_ns(TS), "at-center")]))
    client, _ = make_client({("logs", INSTANT): (200, _vector(({}, "1"))), ("logs", RANGE): (200, body)})
    # when
    ev = logs.around(client, SEL, TS, 60)
    # then
    assert [e.line for e in ev.value.entries] == ["at-center"]
    assert ev.value.after.shown == 1
    assert ev.value.before.shown == 0


def test_around_logs_count_then_before_then_after(make_client: ClientFactory) -> None:
    """around 뒤 client.log 는 셋 — count(query) · backward(before) · forward(after) 순이고 body 가 각 응답이다."""
    # given
    count, lines = _vector(({}, "0")), _streams()
    client, _ = make_client({("logs", INSTANT): (200, count), ("logs", RANGE): (200, lines)})
    # when
    logs.around(client, SEL, TS, 60)
    # then
    assert [r.path for r in client.log] == [INSTANT, RANGE, RANGE]
    assert client.log[1].params["direction"] == "backward"
    assert client.log[2].params["direction"] == "forward"
    assert [r.body for r in client.log] == [count, lines, lines]


def test_around_logs_count_before_failing_range(make_client: ClientFactory) -> None:
    """around 의 둘째 호출(before)이 500 이면 ObsError 와 별개로 client.log 는 둘 — count 200 뒤 before 500 이고 after 는 없다."""
    # given — count 200 · query_range 500
    count = _vector(({}, "3"))
    client, _ = make_client({("logs", INSTANT): (200, count), ("logs", RANGE): (500, {"message": "boom"})})
    # when
    with pytest.raises(ObsError):
        logs.around(client, SEL, TS, 60)
    # then
    assert [r.path for r in client.log] == [INSTANT, RANGE]
    assert [r.status for r in client.log] == [200, 500]
    assert client.log[1].params["direction"] == "backward"


def test_around_center_is_ts(make_client: ClientFactory) -> None:
    """Context 의 center 는 받은 ts 이고 span 은 받은 span 이다."""
    # given
    client, _ = make_client({("logs", INSTANT): (200, _vector(({}, "0"))), ("logs", RANGE): (200, _streams())})
    # when
    ev = logs.around(client, SEL, TS, 60)
    # then
    assert ev.value.center == TS


def test_around_empty_window(make_loki: LokiFactory) -> None:
    """창에 줄이 없으면 entries 는 빈 목록, total 은 0, 양쪽 Side 는 (0, False) 이고 잘리지 않았다."""
    # given
    client, _ = make_loki([], count=0)
    # when
    ev = logs.around(client, SEL, TS, 60)
    # then
    assert ev.value.entries == []
    assert ev.value.total == 0
    assert ev.value.before == Side(0, False)
    assert ev.value.after == Side(0, False)
    assert not ev.value.truncated


def test_around_line_at_center_counts_once_as_after(make_loki: LokiFactory) -> None:
    """ts 와 정확히 같은 시각의 줄은 한 번만 실리고 뒤에 센다."""
    # given
    client, _ = make_loki([_entry(0, "at-center")], count=1)
    # when
    ev = logs.around(client, SEL, TS, 60)
    # then
    assert [e.line for e in ev.value.entries] == ["at-center"]
    assert ev.value.after.shown == 1
    assert ev.value.before.shown == 0


def test_around_limit_applies_per_side(make_loki: LokiFactory) -> None:
    """상한은 쪽마다다 — 앞 · 뒤 각각 limit 까지 실리고 둘 다 잘림이다."""
    # given — 창 안에 앞 3 · 뒤 3
    world = [_entry(-30, "b0"), _entry(-20, "b1"), _entry(-10, "b2"), _entry(10, "a0"), _entry(20, "a1"), _entry(30, "a2")]
    client, _ = make_loki(world, count=6)
    # when
    ev = logs.around(client, SEL, TS, 60, limit=2)
    # then
    assert ev.value.before == Side(2, True)
    assert ev.value.after == Side(2, True)
    assert ev.value.shown == 4


@pytest.mark.parametrize("span,limit", [(0.0, 5), (60.0, 0)], ids=["span", "limit"])
def test_around_rejects_bad_span_or_limit(make_client: ClientFactory, span: float, limit: int) -> None:
    """span 이 0 이하거나 limit 이 1 미만이면 질의 전에 ObsError 다."""
    # given
    client, t = make_client()
    # when · then
    with pytest.raises(ObsError):
        logs.around(client, SEL, TS, span, limit=limit)
    assert t.calls == []


# ── first_seen — pattern_filter → query_range 1회(forward · limit 1) ─────────────────

TEMPLATE: str = "<_> level=error <_>"


def test_first_seen_returns_first_match_timestamp(make_client: ClientFactory) -> None:
    """돌아온 첫 줄의 시각과 줄이 FirstSeen 에 실린다."""
    # given
    client, _ = make_client({("logs", RANGE): (200, _streams(({"pod": "p"}, [(100_000_000_000_000, "first line")])))})
    # when
    ev = logs.first_seen(client, SEL, TEMPLATE, W)
    # then
    assert ev.value.ts == 100_000.0
    assert ev.value.line == "first line"


def test_first_seen_asks_forward_first_line_with_pattern_filter(make_client: ClientFactory) -> None:
    """요청은 query_range 1회 — forward · limit 1 · query 는 pattern_filter 의 것이다."""
    # given
    client, t = make_client({("logs", RANGE): (200, _streams())})
    # when
    logs.first_seen(client, SEL, TEMPLATE, W)
    # then
    assert len(t.calls) == 1
    params = _params(t.calls[0])
    assert params["direction"] == "forward"
    assert params["limit"] == "1"


def test_first_seen_picks_earliest_across_streams(make_client: ClientFactory) -> None:
    """스트림 둘에 줄이 하나씩이면 더 이른 시각이다."""
    # given
    body = _streams(({"pod": "b"}, [(200_000_000_000_000, "later")]), ({"pod": "a"}, [(100_000_000_000_000, "earlier")]))
    client, _ = make_client({("logs", RANGE): (200, body)})
    # when
    ev = logs.first_seen(client, SEL, TEMPLATE, W)
    # then
    assert ev.value.ts == 100_000.0
    assert ev.value.line == "earlier"


def test_first_seen_absent_in_window(make_client: ClientFactory) -> None:
    """빈 스트림이면 ts 와 line 은 None 이고 template 은 남는다 — 창 안에 없음."""
    # given
    client, _ = make_client({("logs", RANGE): (200, _streams())})
    # when
    ev = logs.first_seen(client, SEL, TEMPLATE, W)
    # then
    assert ev.value.ts is None
    assert ev.value.line is None


def test_first_seen_requires_template(make_client: ClientFactory) -> None:
    """공백뿐인 템플릿은 질의 전에 ObsError 다."""
    # given
    client, t = make_client()
    # when · then
    with pytest.raises(ObsError):
        logs.first_seen(client, SEL, " ", W)
    assert t.calls == []


# ── 공통 거부 · 진입 — selector · logs_run ────────────────────────────────────────────

def _call_op(client: Client, op: str, selector: str) -> None:
    if op == "histogram":
        logs.histogram(client, selector, W)
    elif op == "templates":
        logs.templates(client, selector, W)
    elif op == "around":
        logs.around(client, selector, TS, 60)
    else:
        logs.first_seen(client, selector, TEMPLATE, W)


@pytest.mark.parametrize("op", ["histogram", "templates", "around", "first_seen"])
def test_logs_ops_require_selector(make_client: ClientFactory, op: str) -> None:
    """공백뿐인 selector 는 네 연산 모두 질의 전에 ObsError 로 거부한다."""
    # given
    client, t = make_client()
    # when · then
    with pytest.raises(ObsError):
        _call_op(client, op, " ")
    assert t.calls == []


ARGS: dict[str, cli.LogsArgs] = {
    "histogram": cli.LogsArgs(cmd="histogram", selector=SEL),
    "templates": cli.LogsArgs(cmd="templates", selector=SEL),
    "around": cli.LogsArgs(cmd="around", selector=SEL, ts="-10m"),
    "first-seen": cli.LogsArgs(cmd="first-seen", selector=SEL, template="<_> x"),
}


def test_logs_run_around_window_is_ts_plus_minus_span(make_client: ClientFactory) -> None:
    """around 의 RunResult 의 window 는 --ts ± --span 이다 — --since 창이 아니다."""
    # given — --since 는 기본 1h · 빈 응답
    client, _ = make_client({("logs", INSTANT): (200, _vector()), ("logs", RANGE): (200, _streams())})
    # when
    window = cli.logs_run(client, cli.LogsArgs(cmd="around", selector=SEL, ts="-30m", span="60s"), at=AT).window
    # then
    assert window == Window(AT - 30 * MINUTE - 60, AT - 30 * MINUTE + 60)




# ── logs_run — 환원 값을 이름으로 ────────────────────────────────────────────────────

LOGS_TYPES: dict[str, type[object]] = {"histogram": logs.Histogram, "templates": logs.Templates, "around": logs.Context, "first-seen": logs.FirstSeen}
"""서브커맨드 → logs_run 의 evidence.value 가 되는 환원 dataclass."""


@pytest.mark.parametrize("cmd", sorted(LOGS_TYPES))
def test_logs_run_returns_reduction_value_by_subcommand(make_client: ClientFactory, cmd: str) -> None:
    """logs_run 의 결과는 kind 가 logql-<cmd> 이고 evidence.value 가 그 연산의 환원 dataclass(Histogram · Templates · Context · FirstSeen)다 — 줄이 아니다."""
    # given — 최소 라우트
    line = _streams(({"pod": "p"}, [(_ns(AT - 60), "x")]))
    client, _ = make_client({("logs", RANGE): (200, _matrix(({"detected_level": "error"}, [(AT - 60, "1")])) if cmd == "histogram" else line),
                             ("logs", PATTERNS): (200, _patterns(("a <_>", [(100, 1)]))),
                             ("logs", INSTANT): (200, _vector(({}, "1")))})
    # when
    r = cli.logs_run(client, ARGS[cmd], at=AT)
    # then
    assert r.kind == "logql-%s" % cmd
    assert isinstance(r.evidence.value, LOGS_TYPES[cmd])


def test_around_lines_empty_names_the_ts_window() -> None:
    """빈 문맥(entries 없음)의 around_lines 는 "(데이터 없음 …" 한 줄이고, 그 줄은 ts ± span 의 창을 말하며 --since 창의 표기가 없다."""
    # given — 창 안 건수 0 인 손 입력 Context · 창은 center ± 60s
    center = AT - 30 * MINUTE
    ctx = logs.Context(center=center, span=60.0, limit=50, total=0, before=Side(shown=0, truncated=False), after=Side(shown=0, truncated=False), entries=[])
    w = Window(center - 60, center + 60)
    # when
    lines = cli.around_lines(ctx, w)
    # then
    assert len(lines) == 1
    assert lines[0].startswith("(데이터 없음")
    assert cli.window_str(w) in lines[0]
    assert cli.window_str(Window.last("1h", None, at=AT)) not in lines[0]
