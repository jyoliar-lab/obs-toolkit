"""replay 검사 부품 — request_key 정규화 · ReplayTransport 의 순서 비교 · 불일치 실패 문구 · unchecked · manual 경고."""
from __future__ import annotations

import warnings

import pytest

from obs.core import Client, Config, ObsError
from tests.conftest import ReplayFactory
from tests.record import RECORDED_MANUAL, Reply, Request, RecordedCall, normalize_key, parse_params
from tests.replay import Loaded, ReplayTransport, load_call, request_key, shared_at
from tests.record import Fixture

PHYS: str = "https://obs.mg.jyo-liar.com/api/datasources/proxy/uid/prometheus/"   # 기본 설정의 접두 — 테스트 config 가 만드는 것과 같다


def test_request_key_strips_physical_and_ignores_param_order(config: Config) -> None:
    """request_key 는 물리를 지우고 파라미터 순서에 무관한 키를 만든다."""
    a = request_key(PHYS + "api/v1/query?query=up&time=1000.0", config)
    b = request_key(PHYS + "api/v1/query?time=1000.0&query=up", config)
    assert a == b == "api/v1/query?query=up&time=1000.0"
    assert "obs.mg" not in a
    assert "datasources" not in a
    assert "prometheus" not in a
    assert request_key(PHYS + "api/v1/labels", config) == "api/v1/labels"
    assert request_key("https://obs.mg.jyo-liar.com/api/datasources/proxy/uid/loki-careerhub/loki/api/v1/query_range?limit=5&query=%7Ba%3D%22b%22%7D", config) \
        == 'loki/api/v1/query_range?limit=5&query=%7Ba%3D%22b%22%7D'


def test_request_key_distinguishes_different_queries(config: Config) -> None:
    """다른 질의 · 파라미터 값 · 경로 · 값 집합은 다른 키다."""
    base = request_key(PHYS + "api/v1/query?query=up&time=1000.0", config)
    assert request_key(PHYS + "api/v1/query?query=up%7Bjob%3D%22x%22%7D&time=1000.0", config) != base
    assert request_key(PHYS + "api/v1/query?query=up&time=1000", config) != base          # 표기가 다르면 다르다 — 코드가 같은 경로로 만들므로 녹화와 replay 는 같다
    assert request_key(PHYS + "api/v1/query_range?query=up&time=1000.0", config) != base
    assert request_key(PHYS + "api/v1/series?match%5B%5D=a&match%5B%5D=b", config) == request_key(PHYS + "api/v1/series?match%5B%5D=b&match%5B%5D=a", config)
    assert request_key(PHYS + "api/v1/series?match%5B%5D=a", config) != request_key(PHYS + "api/v1/series?match%5B%5D=a&match%5B%5D=b", config)
    with pytest.raises(ValueError):
        request_key("https://elsewhere.example/api/v1/query", config)


def test_normalize_key_matches_recorded_request_key() -> None:
    """녹화 쪽(Request.key)과 나간 쪽(request_key)이 같은 정규화를 쓴다."""
    rq = Request(at=1.0, at_rfc3339="x", backend="metrics", path="api/v1/query", query_string="time=1.0&query=up", params=parse_params("time=1.0&query=up"))
    assert rq.key == normalize_key("api/v1/query", {"query": "up", "time": "1.0"}) == "api/v1/query?query=up&time=1.0"


def _loaded(name: str, path: str, params: dict[str, str], body: object = None, manual: bool = False, at: float | None = 1000.0) -> Loaded:
    call = RecordedCall(Request(at=at, at_rfc3339=None, backend="metrics", path=path, query_string="", params=dict(params)),
                        Reply(200, {"status": "success", "data": {"resultType": "vector", "result": []}} if body is None else body))   # pyright: ignore[reportArgumentType]
    fx = Fixture(format="obs-fixture/1", recorded=RECORDED_MANUAL if manual else "tests/record.py", reason="이유" if manual else None, calls=[call])
    return Loaded(name=name, fixture=fx, call=call)


def test_replay_transport_compares_each_call_in_order_and_shows_both_keys(config: Config) -> None:
    """나간 요청이 녹화 요청과 다르면 실패하고, 실패 문구에 두 요청이 나란히 보인다. 파라미터 순서만 다른 요청은 같다."""
    t = ReplayTransport(expected=[_loaded("a.json", "api/v1/query", {"query": "up", "time": "1000.0"}),
                                  _loaded("b.json", "api/v1/labels", {})], config=config)
    client = Client(config=config, token="t", transport=t)
    assert client.get("metrics", "api/v1/query", {"time": 1000.0, "query": "up"}).status == 200
    with pytest.raises(AssertionError) as ei:
        client.get("metrics", "api/v1/labels", {"match[]": "up"})
    msg = str(ei.value)
    assert msg.startswith("요청 불일치 — 호출 2/2 (b.json)")
    assert "나간:  api/v1/labels?match[]=up" in msg
    assert "녹화:  api/v1/labels" in msg


def test_replay_transport_rejects_calls_beyond_recording(config: Config) -> None:
    """녹화보다 많이 부르면 실패한다."""
    t = ReplayTransport(expected=[_loaded("a.json", "api/v1/query", {})], config=config)
    client = Client(config=config, token="t", transport=t)
    client.get("metrics", "api/v1/query")
    with pytest.raises(AssertionError, match="녹화보다 많은 호출"):
        client.get("metrics", "api/v1/query")


def test_replay_transport_at_is_the_recorded_at(config: Config) -> None:
    """t.at 은 녹화 호출의 at 이다 — 테스트가 그 시각을 *_run 에 주입한다."""
    t = ReplayTransport(expected=[_loaded("a.json", "api/v1/query", {}, at=1000.0)], config=config)
    assert t.at == 1000.0


def test_replay_transport_fails_on_wrong_query_not_just_path(config: Config) -> None:
    """경로가 같아도 질의가 다르면 replay 가 실패한다."""
    t = ReplayTransport(expected=[_loaded("a.json", "api/v1/query", {"query": 'label_replace(x, "stat", "n", "", "")', "time": "1000.0"})], config=config)
    client = Client(config=config, token="t", transport=t)
    with pytest.raises(AssertionError) as ei:
        client.get("metrics", "api/v1/query", {"query": 'label_replace(x, "stats", "n", "", "")', "time": 1000.0})
    assert '"stat"' in str(ei.value)
    assert '"stats"' in str(ei.value)


def test_replay_transport_unchecked_needs_a_reason_and_skips_comparison(config: Config) -> None:
    """unchecked=<이유> 만이 검사를 끄고, 끄면 어떤 질의든 녹화 응답이 돌아온다."""
    t = ReplayTransport(expected=[_loaded("empty.json", "api/v1/query", {"query": "other", "time": "1.0"})], config=config,
                        unchecked="공유 fixture — 다른 질의로도 같은 빈 응답을 쓴다")
    client = Client(config=config, token="t", transport=t)
    assert client.get("metrics", "api/v1/query", {"query": "up", "time": 1000.0}).body == {"status": "success", "data": {"resultType": "vector", "result": []}}
    assert ReplayTransport.__dataclass_fields__["unchecked"].default is None


def test_replay_transport_rejects_empty_unchecked_reason(config: Config) -> None:
    """unchecked="" 는 검사를 끄지 못한다 — 이유가 비면 ValueError."""
    with pytest.raises(ValueError, match="unchecked"):
        ReplayTransport(expected=[_loaded("a.json", "api/v1/query", {})], config=config, unchecked="")


def test_replay_transport_warns_and_skips_manual_fixture(config: Config) -> None:
    """recorded: manual 은 비교하지 않고 경고로 알린다."""
    with pytest.warns(UserWarning, match="recorded: manual"):
        t = ReplayTransport(expected=[_loaded("m.json", "api/traces/abc", {}, manual=True, at=None)], config=config)
    client = Client(config=config, token="t", transport=t)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert client.get("traces", "api/traces/other").status == 200          # 비교 없음
    with pytest.raises(ValueError):
        shared_at(t.expected)                                                   # at 불명 — 주입할 것이 없다


def test_shared_at_requires_one_at(config: Config) -> None:
    """한 replay 의 fixture 들은 at 이 하나여야 한다."""
    with pytest.raises(ValueError):
        shared_at([_loaded("a.json", "api/v1/query", {}, at=1.0), _loaded("b.json", "api/v1/query", {}, at=2.0)])


def test_load_call_returns_request_key_and_at() -> None:
    """load_call 은 응답만이 아니라 요청 키 · at 도 돌려준다."""
    l = load_call("promql-instant-up.json")
    assert l.call.request.key.startswith("api/v1/query?query=up&time=")
    assert l.call.request.at is not None
    assert not l.manual


def test_load_call_picks_one_call_by_path_fragment() -> None:
    """호출이 여럿인 파일은 경로 조각으로 하나를 고른다 — 조각 없이 부르면 거부한다."""
    tr = load_call(("traceql-trace.json", "api/traces/"))
    assert tr.call.request.path.startswith("api/traces/")
    assert tr.call.request.key == tr.call.request.path
    with pytest.raises(ValueError):
        load_call("traceql-trace.json")


def test_load_call_marks_manual_fixture() -> None:
    """recorded: manual 인 fixture 는 manual 로 표시된다."""
    assert load_call("traceql-trace-error.json").manual


def test_replay_passes_when_recorded_at_is_injected(replay: ReplayFactory) -> None:
    """녹화 at 을 주입한 실제 실행은 요청 정합 검사를 통과한다."""
    from obs.adapters import cli
    client, t = replay("promql-instant-up.json")
    ev, _, _ = cli.promql_run(client, cli.PromqlArgs(query="up"), at=t.at)
    assert len(ev.value.lines) == 65
    t.assert_exhausted()


def test_replay_fails_on_request_mismatch_when_at_differs(replay: ReplayFactory) -> None:
    """다른 at 을 주면 나간 질의의 time 이 달라 요청 불일치로 실패한다."""
    from obs.adapters import cli
    client, t = replay("promql-instant-up.json")
    with pytest.raises(AssertionError, match="요청 불일치"):
        cli.promql_run(client, cli.PromqlArgs(query="up"), at=t.at + 1)


def test_replay_exhausted_check_fails_when_nothing_was_called(replay: ReplayFactory) -> None:
    """transport 전에 거부된 실행(질의 없음)은 호출이 0 이라 assert_exhausted 가 실패한다."""
    from obs.adapters import cli
    client, t = replay("promql-instant-up.json")
    with pytest.raises(ObsError):
        cli.promql_run(client, cli.PromqlArgs())
    with pytest.raises(AssertionError, match="중 0개만"):
        t.assert_exhausted()
