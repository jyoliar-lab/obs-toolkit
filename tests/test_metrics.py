"""메트릭 환원 연산 — summarize · compare · top (#95). 녹화 응답(cAdvisor CPU 두 창 · topk 메모리 · 빈 응답)으로 돈다."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from obs import metrics
from obs.adapters import cli
from obs.core import Evidence, Lines, ObsError, Window, to_dict
from tests.conftest import ClientFactory, load_fixture

PHYSICAL: tuple[str, ...] = ("obs.mg", "jyo-liar.com/api", "datasources/proxy", "uid/prometheus")
CPU_QUERY: str = 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="careerhub-prod"}[5m]))'
AT: float = 100000.0          # 테스트의 now — 지금 창 [96400, 100000], 1d 기준선 [10000, 13600]
NOW_START: str = "start=96400.0"
BASE_1D_START: str = "start=10000.0"


def _no_physical(text: str) -> None:
    for word in PHYSICAL:
        assert word not in text, "agent 출력에 물리 정보: %s" % word


def _matrix(*series: tuple[dict[str, str], list[str]]) -> dict[str, object]:
    """Prometheus query_range 응답 모양을 손으로 — 값 문자열 목록을 60초 간격으로 놓는다."""
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": labels, "values": [[1000.0 + 60 * i, v] for i, v in enumerate(vals)]} for labels, vals in series]}}


# ── 계산 — 순수 ──────────────────────────────────────────────────────────────────

def test_percentile_interpolates() -> None:
    """분위수는 이웃 두 값의 선형 보간(numpy 기본) — 홀수·짝수 길이와 양 끝을 확인한다."""
    assert metrics.percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert metrics.percentile([1.0, 2.0, 3.0], 0.5) == 2.0
    assert metrics.percentile([10.0], 0.95) == 10.0
    assert metrics.percentile([0.0, 100.0], 0.95) == 95.0
    with pytest.raises(ValueError):
        metrics.percentile([], 0.5)


def test_slope_least_squares() -> None:
    """기울기는 최소제곱(값/초) — 선형 증가면 정확히 그 기울기, 상수면 0, 점 하나면 0."""
    assert metrics.slope([(0.0, 1.0), (60.0, 2.0), (120.0, 3.0)]) == pytest.approx(1 / 60)
    assert metrics.slope([(0.0, 5.0), (60.0, 5.0)]) == 0.0
    assert metrics.slope([(0.0, 5.0)]) == 0.0
    assert metrics.slope([(0.0, 1.0), (0.0, 2.0)]) == 0.0


def test_summarize_series_skips_non_finite_and_distinguishes_none() -> None:
    """NaN · +Inf · 숫자 아님은 값이 아니라 부재 — n 에서 빠지고, 전부 부재면 None(데이터 없음). 0 은 값이다(n 에 들어간다)."""
    s = metrics.Series(labels={"pod": "a"}, values=[(1.0, "NaN"), (2.0, "0"), (3.0, "+Inf"), (4.0, "x"), (5.0, "4")])
    sm = metrics.summarize_series(s)
    assert sm is not None and (sm.n, sm.min, sm.max, sm.last) == (2, 0.0, 4.0, 4.0)
    assert metrics.summarize_series(metrics.Series(labels={}, values=[(1.0, "NaN")])) is None
    assert metrics.summarize_series(metrics.Series(labels={}, values=[])) is None
    zero = metrics.summarize_series(metrics.Series(labels={}, values=[(1.0, "0"), (2.0, "0")]))
    assert zero is not None and zero.n == 2 and zero.p95 == 0.0


def test_auto_step_moved_to_core() -> None:
    """step 자동 — 창 길이/60, 최소 15s. promql --range 와 세 연산이 같은 함수를 쓴다."""
    assert metrics.auto_step(1800) == "30s" and metrics.auto_step(60) == "15s" and metrics.auto_step(86400) == "1440s"
    assert cli.auto_step is metrics.auto_step


# ── summarize ────────────────────────────────────────────────────────────────────

def test_summarize_fixture(make_client: ClientFactory) -> None:
    """녹화한 careerhub-prod pod CPU 1시간(20 시계열 × 61점) — 시계열마다 요약 하나, n=61, min ≤ p50 ≤ p95 ≤ max,
    URL 에 query_range·창·자동 step 이 있고, Evidence 는 metrics/질의/창을 담는다."""
    client, t = make_client({"api/v1/query_range": (200, load_fixture("promql-range-cpu-by-pod-now.json"))})
    ev, raw = metrics.summarize(client, CPU_QUERY, Window.last("1h", at=AT))
    assert ev.backend == "metrics" and ev.query == CPU_QUERY and ev.window == Window(96400.0, 100000.0)
    assert ev.value.step == "60s" and len(ev.value.series) == 20
    for s in ev.value.series:
        assert s.n == 61 and s.min <= s.p50 <= s.p95 <= s.max and "pod" in s.labels
    mongo = next(s for s in ev.value.series if s.labels["pod"] == "careerhub-mongodb-0")
    assert 0.07 < mongo.p50 < 0.08 and mongo.last == pytest.approx(0.0726297, rel=1e-5)
    assert "api/v1/query_range" in t.calls[0].url and NOW_START in t.calls[0].url and "step=60s" in t.calls[0].url
    assert isinstance(raw, dict) and raw.get("status") == "success"
    json.loads(cli.to_json(ev))   # 환원물이 직렬화된다(MCP outputSchema 대비)


def test_summarize_empty_is_no_data_not_zero(make_client: ClientFactory) -> None:
    """녹화한 빈 응답(없는 namespace) — series 가 비고, 렌더링은 "데이터 없음" 이지 0 이 아니다."""
    client, _ = make_client({"api/v1/query_range": (200, load_fixture("promql-range-empty.json"))})
    ev, _ = metrics.summarize(client, CPU_QUERY, Window.last("1h", at=AT))
    assert ev.value.series == []
    lines = cli.summary_lines(ev.value, Window.last("1h", at=AT))
    assert len(lines) == 1 and lines[0].startswith("(데이터 없음") and "n=0" not in lines[0]


def test_summarize_requires_query(make_client: ClientFactory) -> None:
    client, _ = make_client()
    with pytest.raises(ObsError):
        metrics.summarize(client, "  ", Window.last("1h", at=AT))


# ── compare ──────────────────────────────────────────────────────────────────────

def test_compare_fixture_same_step_and_deltas(make_client: ClientFactory) -> None:
    """AC 2 의 fixture 판 — 녹화한 지금 창과 1일 전 창(둘 다 20 시계열 × 61점). 두 질의가 같은 step 이고 창 길이가 같으며,
    시계열별 Delta 20개가 비율 내림차순, 원본에 두 응답이 다 있다. mongodb-0 은 하루 전과 비슷해 비율 ≈ 1."""
    now, base = load_fixture("promql-range-cpu-by-pod-now.json"), load_fixture("promql-range-cpu-by-pod-1d-ago.json")
    client, t = make_client({NOW_START: (200, now), BASE_1D_START: (200, base)})
    ev, raw = metrics.compare(client, CPU_QUERY, Window.last("1h", at=AT).with_baseline("1d"))
    assert ev.window is not None and ev.window.baseline == Window(10000.0, 13600.0)
    assert len(t.calls) == 2 and all("step=60s" in c.url for c in t.calls)
    assert NOW_START in t.calls[0].url and BASE_1D_START in t.calls[1].url
    v = ev.value
    assert v.step == "60s" and not v.now_empty and not v.baseline_empty and len(v.deltas) == 20
    ratios = [d.ratio for d in v.deltas]
    assert all(r is not None for r in ratios) and ratios == sorted((r for r in ratios if r is not None), reverse=True)
    mongo = next(d for d in v.deltas if d.labels == {"pod": "careerhub-mongodb-0"})
    assert mongo.now is not None and mongo.base is not None and mongo.ratio is not None and mongo.delta is not None
    assert 0.9 < mongo.ratio < 1.1 and mongo.delta == pytest.approx(mongo.now.p95 - mongo.base.p95)
    assert isinstance(raw, dict) and set(raw) == {"now", "baseline"}
    lines = cli.compare_lines(v, ev.window)
    assert len(lines) == 21 and lines[0].startswith("지금 ") and "기준선 " in lines[0] and "step=60s" in lines[0]
    assert all(re.match(r"^×[\d.e+-]+  Δ[+-]", l) for l in lines[1:]) and all("n=61/61" in l for l in lines[1:])
    _no_physical("\n".join(lines))


def test_compare_default_baseline_is_one_day(make_client: ClientFactory) -> None:
    """기준선이 없는 Window 를 주면 DEFAULT_BASELINE(1d — 1일 전 같은 시각)을 단다. #102 의 red 도 같은 기본값을 쓴다."""
    client, t = make_client({"api/v1/query_range": (200, load_fixture("promql-range-empty.json"))})
    ev, _ = metrics.compare(client, CPU_QUERY, Window.last("1h", at=AT))
    assert metrics.DEFAULT_BASELINE == "1d"
    assert ev.window is not None and ev.window.baseline == Window(AT - 3600 - 86400, AT - 86400)
    assert BASE_1D_START in t.calls[1].url


def test_compare_prev_baseline(make_client: ClientFactory) -> None:
    """--baseline prev — 직전 창(창 길이만큼 앞). 급변 감지용 규약."""
    client, t = make_client({"api/v1/query_range": (200, load_fixture("promql-range-empty.json"))})
    ev, _ = metrics.compare(client, CPU_QUERY, Window.last("1h", at=AT).with_baseline("prev"))
    assert ev.window is not None and ev.window.baseline == Window(AT - 7200, AT - 3600)
    assert "start=92800.0" in t.calls[1].url and "end=96400.0" in t.calls[1].url


def test_compare_rejects_baseline_of_different_length(make_client: ClientFactory) -> None:
    """창 길이가 다르면 같은 step 이라도 점 수가 달라 비교가 틀린다 — 손으로 만든 어긋난 기준선은 거부한다(질의 전에)."""
    client, t = make_client()
    with pytest.raises(ObsError) as ei:
        metrics.compare(client, CPU_QUERY, Window(0.0, 3600.0, baseline=Window(0.0, 1800.0)))
    assert "step" in str(ei.value) and t.calls == []


def test_compare_baseline_missing_series_and_zero_do_not_divide(make_client: ClientFactory) -> None:
    """AC 4 — 시계열 단위: 기준선에 없는 시계열은 "기준선 없음"(base None), 기준선이 0 인 시계열은 비율 없음(base 는 있다),
    지금 창에만 없는 시계열은 "지금 없음". 어느 경우도 ZeroDivisionError 없이 절대 차이는 남는다. 순서: 비율 → 기준선 0 → 기준선 없음 → 지금 없음."""
    now = _matrix(({"pod": "a"}, ["0.4", "0.4"]), ({"pod": "b"}, ["1", "1"]), ({"pod": "d"}, ["2", "4"]))
    base = _matrix(({"pod": "a"}, ["0", "0"]), ({"pod": "c"}, ["3", "3"]), ({"pod": "d"}, ["1", "1"]))
    client, _ = make_client({NOW_START: (200, now), BASE_1D_START: (200, base)})
    ev, _ = metrics.compare(client, "q", Window.last("1h", at=AT).with_baseline("1d"))
    by = {d.labels["pod"]: d for d in ev.value.deltas}
    assert [d.labels["pod"] for d in ev.value.deltas] == ["d", "a", "b", "c"]
    assert by["a"].base is not None and by["a"].ratio is None and by["a"].delta == pytest.approx(0.4) and by["a"].base_is_zero
    assert by["b"].base is None and by["b"].ratio is None and by["b"].delta is None
    assert by["c"].now is None and by["c"].base is not None
    assert by["d"].ratio == pytest.approx(3.9) and by["d"].delta == pytest.approx(2.9)
    lines = cli.compare_lines(ev.value, ev.window or Window(0, 0))
    assert lines[1].startswith("×3.9  Δ+2.9  now p95=3.9  base p95=1  n=2/2")
    assert lines[2].startswith("×—  Δ+0.4  now p95=0.4  base p95=0  (기준선 0)")
    assert lines[3].startswith("기준선 없음  now p95=1  n=2/-") and lines[4].startswith("지금 없음  base p95=3  n=-/2")


def test_compare_baseline_window_empty_says_so(make_client: ClientFactory) -> None:
    """AC 4 — 창 단위: 기준선 창에 시계열이 하나도 없으면(녹화한 빈 응답) baseline_empty 이고 렌더링에 "기준선 없음" 이 명시되며,
    비율은 어디에도 없다. 지금 창의 값은 남는다(0 이 아니라 "기준선 없음")."""
    client, _ = make_client({NOW_START: (200, load_fixture("promql-range-cpu-by-pod-now.json")),
                             BASE_1D_START: (200, load_fixture("promql-range-empty.json"))})
    ev, _ = metrics.compare(client, CPU_QUERY, Window.last("1h", at=AT).with_baseline("1d"))
    v = ev.value
    assert v.baseline_empty and not v.now_empty and len(v.deltas) == 20
    assert all(d.ratio is None and d.base is None and d.now is not None for d in v.deltas)
    lines = cli.compare_lines(v, ev.window or Window(0, 0))
    assert lines[1].startswith("기준선 없음 — ") and "0 으로 나누지 않는다" in lines[1]
    assert all(l.startswith("기준선 없음  now p95=") for l in lines[2:]) and not any("×" in l for l in lines[1:])


def test_compare_both_empty(make_client: ClientFactory) -> None:
    client, _ = make_client({"api/v1/query_range": (200, load_fixture("promql-range-empty.json"))})
    ev, _ = metrics.compare(client, CPU_QUERY, Window.last("1h", at=AT))
    assert ev.value.now_empty and ev.value.baseline_empty and ev.value.deltas == []
    lines = cli.compare_lines(ev.value, ev.window or Window(0, 0))
    assert len(lines) == 1 and lines[0].startswith("(데이터 없음")


# ── top ──────────────────────────────────────────────────────────────────────────

def test_top_wraps_query_server_side(make_client: ClientFactory) -> None:
    """AC 3 의 fixture 판 — 녹화한 topk(10, sum by (pod) (container_memory_working_set_bytes)). 나간 질의가 topk 로 감싸져
    서버가 k 개만 돌려주고(카디널리티는 여기서 막힌다), 즉시 조회는 창 끝 시각, 항목은 값 내림차순 10개, 줄도 10개 이하."""
    client, t = make_client({"api/v1/query?": (200, load_fixture("promql-instant-topk-memory-by-pod.json"))})
    ev, _ = metrics.top(client, "container_memory_working_set_bytes", Window.last("1h", at=AT), k=10, by=["pod"])
    assert ev.query == "topk(10, sum by (pod) (container_memory_working_set_bytes))"
    assert "query=topk%2810%2C+sum+by+%28pod%29" in t.calls[0].url and "time=100000.0" in t.calls[0].url
    assert ev.window == Window(AT, AT)
    v = ev.value
    assert (v.k, v.by, v.agg) == (10, ["pod"], "sum") and len(v.entries) == 10
    assert [e.value for e in v.entries] == sorted((e.value for e in v.entries), reverse=True)
    assert v.entries[0].labels["pod"].startswith("prometheus-")
    lines = cli.top_lines(v, ev.window or Window(0, 0))
    assert len(lines) <= 10 and all(re.match(r"^\d+  \{pod=", l) for l in lines)
    _no_physical("\n".join(lines))


def test_top_clips_to_k_even_if_server_returns_more(make_client: ClientFactory) -> None:
    """서버가 k 보다 많이 돌려줘도(다른 프록시 · 동률) 함수가 k 로 자른다 — 상한은 함수의 것."""
    client, _ = make_client({"api/v1/query?": (200, load_fixture("promql-instant-topk-memory-by-pod.json"))})
    ev, _ = metrics.top(client, "container_memory_working_set_bytes", Window.last("1h", at=AT), k=3, by=["pod"])
    assert len(ev.value.entries) == 3 and ev.query.startswith("topk(3, ")


def test_top_query_axes_and_validation() -> None:
    """--by 는 쉼표로 여러 축, 집계는 정해진 것만, k ≥ 1. 빈 축은 거부."""
    assert metrics.top_query("up", 5, ["namespace", " pod "], "max") == "topk(5, max by (namespace, pod) (up))"
    for bad in (lambda: metrics.top_query("up", 0, ["pod"], "sum"), lambda: metrics.top_query("up", 5, ["pod"], "p95"),
                lambda: metrics.top_query("up", 5, [" "], "sum")):
        with pytest.raises(ObsError):
            bad()


def test_top_empty_is_no_data(make_client: ClientFactory) -> None:
    client, _ = make_client({"api/v1/query?": (200, {"status": "success", "data": {"resultType": "vector", "result": []}})})
    ev, _ = metrics.top(client, "nothing", Window.last("1h", at=AT))
    assert ev.value.entries == []
    lines = cli.top_lines(ev.value, ev.window or Window(0, 0))
    assert len(lines) == 1 and lines[0].startswith("(데이터 없음")


# ── CLI 서브커맨드 ────────────────────────────────────────────────────────────────

def test_metrics_run_dispatch(make_client: ClientFactory) -> None:
    """metrics_run — 세 서브커맨드가 Evidence[Lines] 와 kind 를 내고, compare 의 Evidence.window 에 기준선이 달리며, top 의 query 는 감싼 질의."""
    client, _ = make_client({NOW_START: (200, load_fixture("promql-range-cpu-by-pod-now.json")),
                             BASE_1D_START: (200, load_fixture("promql-range-cpu-by-pod-1d-ago.json")),
                             "api/v1/query?": (200, load_fixture("promql-instant-topk-memory-by-pod.json"))})
    ev, _, kind = cli.metrics_run(client, cli.MetricsArgs(cmd="summarize", query=CPU_QUERY), at=AT)
    assert kind == "promql-summarize" and isinstance(ev, Evidence) and isinstance(ev.value, Lines) and len(ev.value.lines) == 20
    ev, _, kind = cli.metrics_run(client, cli.MetricsArgs(cmd="compare", query=CPU_QUERY, baseline="1d"), at=AT)
    assert kind == "promql-compare" and ev.window is not None and ev.window.baseline is not None and len(ev.value.lines) == 21
    ev, _, kind = cli.metrics_run(client, cli.MetricsArgs(cmd="top", query="container_memory_working_set_bytes", by="pod", k=10), at=AT)
    assert kind == "promql-top" and ev.query.startswith("topk(10, sum by (pod)") and len(ev.value.lines) <= 10
    with pytest.raises(ObsError):
        cli.metrics_run(client, cli.MetricsArgs(cmd="red", query="x"), at=AT)


def test_promql_main_dispatches_subcommand(make_client: ClientFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    """AC 3 의 CLI 판 — `promql.py top '<q>' --by pod --k 10` 이 서브커맨드로 분기해 stdout 에 표 10줄 이하 + 호출 디렉터리 한 줄만 찍는다.
    기존 형태(`promql.py 'up'`)는 그대로 즉시 조회다."""
    client, t = make_client({"api/v1/query?": (200, load_fixture("promql-instant-topk-memory-by-pod.json"))})
    monkeypatch.setattr(cli, "_client", lambda: client)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    cli.promql_main(["top", "container_memory_working_set_bytes", "--by", "pod", "--k", "10"])
    out = capsys.readouterr().out.splitlines()
    assert len(out) <= 11 and out[-1].startswith("원본·요약: tmp/obs/") and out[-1].endswith("-promql-top/ (raw.json · summary.txt)")
    assert all(re.match(r"^\d+  \{pod=", l) for l in out[:-1]) and "topk%2810" in t.calls[0].url
    cli.promql_main(["up"])
    assert "query=up" in t.calls[1].url and "topk" not in t.calls[1].url


@pytest.mark.parametrize("argv", [["--help"], ["compare", "--help"], ["top", "--help"], ["summarize", "--help"]])
def test_help_mentions_operations_without_physical(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """--help — 최상위에 세 연산이 보이고, 서브커맨드 도움말에 --baseline(compare) · --by/--k(top) 이 있으며, 물리 인자·문자열이 없다."""
    with pytest.raises(SystemExit) as ei:
        cli.promql_main(argv)
    assert ei.value.code == 0
    text = capsys.readouterr().out
    if argv == ["--help"]:
        assert "summarize" in text and "compare" in text and "top" in text
    if argv[0] == "compare":
        assert "--baseline" in text and "prev" in text
    if argv[0] == "top":
        assert "--by" in text and "--k" in text
    for flag in ("--uid", "--url", "--token", "--host"):
        assert flag not in text
    _no_physical(text)


def test_window_baseline_prev_and_serialization() -> None:
    """Window.with_baseline 규약 — 'prev' 는 창 길이만큼 앞, 기간은 그만큼 앞의 같은 시각, 잘못된 spec 은 ObsError. to_dict 에 기준선이 중첩된다."""
    w = Window(1000.0, 1600.0)
    assert w.with_baseline("prev").baseline == Window(400.0, 1000.0)
    assert w.with_baseline("1w").baseline == Window(1000.0 - 604800, 1600.0 - 604800)
    with pytest.raises(ObsError):
        w.with_baseline("yesterday")
    d = to_dict(w.with_baseline("prev"))
    assert isinstance(d, dict) and d["baseline"] == {"start": 400.0, "end": 1000.0, "baseline": None}
