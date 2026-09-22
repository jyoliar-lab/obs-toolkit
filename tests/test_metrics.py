"""메트릭 환원 연산 — summarize · compare · top (#95). 계산은 백엔드가 한다 — 여기서 검사하는 것은 질의 조립 · 응답 → 계약 · 없음/0 판정 · 예산.
fixture 는 서버 환원 응답의 녹화(careerhub-prod pod CPU 의 일곱 통계 · 1d offset 비교 · 보존 밖 offset · 빈 응답 · topk 메모리)."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

from obs import metrics
from obs.adapters import cli
from obs.core import JSON, Evidence, Lines, ObsError, Window, to_dict
from tests.conftest import ClientFactory, load_fixture

PHYSICAL: tuple[str, ...] = ("obs.mg", "jyo-liar.com/api", "datasources/proxy", "uid/prometheus")
CPU_QUERY: str = 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="careerhub-prod"}[5m]))'
AT: float = 100000.0          # 테스트의 now — 창 [96400, 100000], 1d 기준선은 offset 86400s
SQ: str = "(%s)[3600s:60s]" % CPU_QUERY


def _no_physical(text: str) -> None:
    for word in PHYSICAL:
        assert word not in text, "agent 출력에 물리 정보: %s" % word


def _vector(*items: tuple[dict[str, str], str]) -> dict[str, JSON]:
    """Prometheus 즉시 조회 응답 모양을 손으로 — (레이블, 값) 목록."""
    result: list[JSON] = [{"metric": dict(labels), "value": [AT, v]} for labels, v in items]
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def _stat(pod: str, stat: str, v: str) -> tuple[dict[str, str], str]:
    return {"pod": pod, "stat": stat}, v


# ── 질의 조립 — 계산은 이 문자열로 서버에 넘어간다 ─────────────────────────────────

def test_summarize_query_is_seven_stats_in_one_expression() -> None:
    """일곱 통계가 서브쿼리 `(Q)[창:step]` 위의 *_over_time · deriv 로, `stat` 이름표(label_replace)를 달고 `or` 로 한 식이다."""
    q = metrics.summarize_query(CPU_QUERY, Window.last("1h", at=AT), "60s")
    parts = q.split(" or ")
    assert len(parts) == 7 and all(p.startswith('label_replace(') and p.endswith('"stat", "%s", "", "")' % s)
                                   for p, s in zip(parts, metrics.SUMMARY_STATS))
    assert "count_over_time(%s)" % SQ in q and "quantile_over_time(0.95, %s)" % SQ in q and "deriv(%s)" % SQ in q
    assert "last_over_time(%s)" % SQ in q and "min_over_time(%s)" % SQ in q and "max_over_time(%s)" % SQ in q


def test_compare_query_uses_offset_of_same_expression() -> None:
    """기준선은 같은 식의 `offset` — 지금 p95 와 `offset <기준선>` p95 가 한 식. 두 창은 정의상 같은 길이·해상도다."""
    q = metrics.compare_query(CPU_QUERY, Window.last("1h", at=AT), "60s", "86400s")
    assert q == ('label_replace(quantile_over_time(0.95, %s), "stat", "now", "", "") or '
                 'label_replace(quantile_over_time(0.95, %s offset 86400s), "stat", "base", "", "")' % (SQ, SQ))


def test_auto_step_is_subquery_resolution() -> None:
    """step 자동 — 창 길이/60, 최소 15s. 서브쿼리 해상도로 쓰이고 promql --range 와 같은 함수다."""
    assert metrics.auto_step(1800) == "30s" and metrics.auto_step(60) == "15s" and metrics.auto_step(86400) == "1440s"
    assert metrics.duration_literal(3600.0) == "3600s" and metrics.duration_literal(0.4) == "0s"
    assert cli.auto_step is metrics.auto_step


def test_baseline_offset_rules() -> None:
    """offset 은 창 끝의 차이. 기준선이 없으면 DEFAULT_BASELINE(1d) · prev 는 창 길이 · 길이가 다르거나 뒤에 있는 기준선은 거부(질의 전에)."""
    w = Window.last("1h", at=AT)
    assert metrics.DEFAULT_BASELINE == "1d"
    assert metrics.baseline_offset(w) == (w.with_baseline("1d"), "86400s")
    assert metrics.baseline_offset(w.with_baseline("prev")) == (w.with_baseline("prev"), "3600s")
    assert metrics.baseline_offset(w.with_baseline("1w")) == (w.with_baseline("1w"), "604800s")
    for bad in (Window(0.0, 3600.0, baseline=Window(0.0, 1800.0)), Window(0.0, 3600.0, baseline=Window(100.0, 3700.0))):
        with pytest.raises(ObsError):
            metrics.baseline_offset(bad)


# ── 응답 → 계약 — 계산 없이 판정만 ───────────────────────────────────────────────

def test_split_stats_drops_non_finite_and_strips_stat_label() -> None:
    """`stat` 이름표로 시계열별 {통계: 값}. NaN · +Inf · 숫자 아님 · 이름표 없음은 부재로 빠진다. 0 은 값이다."""
    res = metrics.parse_metric(metrics.check_status(_vector(
        _stat("a", "n", "60"), _stat("a", "p95", "NaN"), _stat("a", "min", "0"), _stat("a", "max", "+Inf"),
        _stat("b", "p95", "x"), ({"pod": "c"}, "1")), "metrics"))
    assert metrics.split_stats(res) == {(("pod", "a"),): {"n": 60.0, "min": 0.0}}


def test_summaries_of_requires_n_and_marks_missing_as_nan() -> None:
    """n 이 없는 시계열은 부재(목록에서 빠진다). 있는데 다른 통계가 부재면 그 값은 NaN — 0 이 아니다."""
    res = metrics.parse_metric(_vector(_stat("a", "n", "60"), _stat("a", "p95", "0.4"), _stat("b", "p95", "1")))
    out = metrics.summaries_of(res)
    assert len(out) == 1 and out[0].labels == {"pod": "a"} and out[0].n == 60 and out[0].p95 == 0.4
    assert math.isnan(out[0].min) and math.isnan(out[0].slope)


# ── summarize ────────────────────────────────────────────────────────────────────

def test_summarize_fixture_one_query(make_client: ClientFactory) -> None:
    """녹화한 서버 환원 응답(20 pod × 7 통계 = 140 값) — GET 1 회(예산), 즉시 조회 URL 에 조립 질의·창 끝 시각, 시계열 20개
    각각 n=60(서브쿼리 점 수) · min ≤ p50 ≤ p95 ≤ max. Evidence.query 는 조립 질의(재실행 가능), window 는 받은 창."""
    client, t = make_client({"api/v1/query?": (200, load_fixture("promql-instant-summarize-cpu-by-pod.json"))})
    ev, raw = metrics.summarize(client, CPU_QUERY, Window.last("1h", at=AT))
    assert len(t.calls) == 1 and "api/v1/query?" in t.calls[0].url and "time=100000.0" in t.calls[0].url
    assert "query_range" not in t.calls[0].url and "count_over_time" in t.calls[0].url and "deriv" in t.calls[0].url
    assert ev.backend == "metrics" and ev.query.startswith("label_replace(count_over_time(") and ev.window == Window(96400.0, 100000.0)
    assert ev.value.step == "60s" and len(ev.value.series) == 20
    for s in ev.value.series:
        assert s.n == 60 and s.min <= s.p50 <= s.p95 <= s.max and math.isfinite(s.slope) and "pod" in s.labels
    assert [s.labels["pod"] for s in ev.value.series] == sorted(s.labels["pod"] for s in ev.value.series)
    assert isinstance(raw, dict) and raw.get("status") == "success"
    json.loads(cli.to_json(ev))   # 환원물이 직렬화된다(MCP outputSchema 대비)
    lines = cli.summary_lines(ev.value, ev.window or Window(0, 0))
    assert len(lines) == 20 and all(re.match(r"^n=60  min=\S+  p50=\S+  p95=\S+  max=\S+  last=\S+  slope=[+-]\S+/s  \{pod=", l) for l in lines)
    _no_physical("\n".join(lines))


def test_summarize_empty_is_no_data_not_zero(make_client: ClientFactory) -> None:
    """녹화한 빈 응답(없는 namespace) — series 가 비고, 렌더링은 "데이터 없음" 이지 0 이 아니다."""
    client, _ = make_client({"api/v1/query?": (200, load_fixture("promql-instant-empty.json"))})
    ev, _ = metrics.summarize(client, CPU_QUERY, Window.last("1h", at=AT))
    assert ev.value.series == []
    lines = cli.summary_lines(ev.value, Window.last("1h", at=AT))
    assert len(lines) == 1 and lines[0].startswith("(데이터 없음") and "n=0" not in lines[0]


def test_summarize_requires_query(make_client: ClientFactory) -> None:
    client, _ = make_client()
    with pytest.raises(ObsError):
        metrics.summarize(client, "  ", Window.last("1h", at=AT))


# ── compare ──────────────────────────────────────────────────────────────────────

def test_compare_fixture_one_query_offset(make_client: ClientFactory) -> None:
    """AC 2 의 fixture 판 — 녹화한 `now` · `base`(offset 86400s) p95 40 값. GET 1 회, URL 에 `offset 86400s`, Delta 20개 비율 내림차순,
    Evidence.window 에 기준선 창이 달린다. 렌더링은 헤더 1줄 + 20행이고 물리 정보가 없다."""
    client, t = make_client({"api/v1/query?": (200, load_fixture("promql-instant-compare-cpu-by-pod-1d.json"))})
    ev, raw = metrics.compare(client, CPU_QUERY, Window.last("1h", at=AT).with_baseline("1d"))
    assert len(t.calls) == 1 and "offset+86400s" in t.calls[0].url and "time=100000.0" in t.calls[0].url
    assert ev.window is not None and ev.window.baseline == Window(10000.0, 13600.0)
    v = ev.value
    assert (v.step, v.offset) == ("60s", "86400s") and not v.now_empty and not v.baseline_empty and len(v.deltas) == 20
    ratios = [d.ratio for d in v.deltas]
    assert all(r is not None for r in ratios) and ratios == sorted((r for r in ratios if r is not None), reverse=True)
    for d in v.deltas:
        assert d.now is not None and d.base is not None and d.delta == pytest.approx(d.now - d.base)
    assert isinstance(raw, dict) and raw.get("status") == "success"
    lines = cli.compare_lines(v, ev.window)
    assert len(lines) == 21 and lines[0].startswith("지금 ") and "(offset 86400s)" in lines[0] and "step=60s" in lines[0]
    assert all(re.match(r"^×[\d.e+-]+  Δ[+-]\S+  now p95=\S+  base p95=\S+  \{pod=", l) for l in lines[1:])
    _no_physical("\n".join(lines))


def test_compare_default_baseline_is_one_day(make_client: ClientFactory) -> None:
    """기준선이 없는 Window 를 주면 DEFAULT_BASELINE(1d) — offset 86400s. #102 의 red 도 같은 기본값을 쓴다."""
    client, t = make_client({"api/v1/query?": (200, load_fixture("promql-instant-empty.json"))})
    ev, _ = metrics.compare(client, CPU_QUERY, Window.last("1h", at=AT))
    assert ev.window is not None and ev.window.baseline == Window(AT - 3600 - 86400, AT - 86400)
    assert ev.value.offset == "86400s" and "offset+86400s" in t.calls[0].url


def test_compare_prev_baseline(make_client: ClientFactory) -> None:
    """--baseline prev — 직전 창 = offset 창 길이(3600s). 급변 감지용 규약."""
    client, t = make_client({"api/v1/query?": (200, load_fixture("promql-instant-empty.json"))})
    ev, _ = metrics.compare(client, CPU_QUERY, Window.last("1h", at=AT).with_baseline("prev"))
    assert ev.window is not None and ev.window.baseline == Window(AT - 7200, AT - 3600)
    assert ev.value.offset == "3600s" and "offset+3600s" in t.calls[0].url


def test_compare_rejects_baseline_not_expressible_as_offset(make_client: ClientFactory) -> None:
    """길이가 다른 기준선은 offset 으로 표현할 수 없다 — 질의 전에 거부."""
    client, t = make_client()
    with pytest.raises(ObsError):
        metrics.compare(client, CPU_QUERY, Window(0.0, 3600.0, baseline=Window(0.0, 1800.0)))
    assert t.calls == []


def test_compare_missing_series_and_zero_do_not_divide(make_client: ClientFactory) -> None:
    """AC 4 — 시계열 단위: 기준선에 없는 시계열은 "기준선 없음"(base None), 기준선이 0 인 시계열은 비율 없음(base 는 있다),
    지금 창에만 없는 시계열은 "지금 없음". 어느 경우도 ZeroDivisionError 없이 절대 차이는 남는다. 순서: 비율 → 기준선 0 → 기준선 없음 → 지금 없음."""
    body = _vector(_stat("a", "now", "0.4"), _stat("a", "base", "0"), _stat("b", "now", "1"), _stat("c", "base", "3"),
                   _stat("d", "now", "3.9"), _stat("d", "base", "1"))
    client, _ = make_client({"api/v1/query?": (200, body)})
    ev, _ = metrics.compare(client, "q", Window.last("1h", at=AT).with_baseline("1d"))
    by = {d.labels["pod"]: d for d in ev.value.deltas}
    assert [d.labels["pod"] for d in ev.value.deltas] == ["d", "a", "b", "c"]

    def fields(d: metrics.Delta) -> tuple[float | None, float | None, float | None, float | None, bool]:
        return d.now, d.base, d.ratio, d.delta, d.base_is_zero

    assert fields(by["a"]) == (0.4, 0.0, None, pytest.approx(0.4), True)     # 기준선 0 — 비율 없음, Δ 는 남는다
    assert fields(by["b"]) == (1.0, None, None, None, False)                 # 기준선 없음
    assert fields(by["c"]) == (None, 3.0, None, None, False)                 # 지금 없음
    assert fields(by["d"]) == (3.9, 1.0, pytest.approx(3.9), pytest.approx(2.9), False)   # 보통 — 비율·Δ
    assert not ev.value.now_empty and not ev.value.baseline_empty
    lines = cli.compare_lines(ev.value, ev.window or Window(0, 0))
    assert lines[1].startswith("×3.9  Δ+2.9  now p95=3.9  base p95=1  {pod=d}")
    assert lines[2].startswith("×—  Δ+0.4  now p95=0.4  base p95=0  (기준선 0)  {pod=a}")
    assert lines[3] == "기준선 없음  now p95=1  {pod=b}" and lines[4] == "지금 없음  base p95=3  {pod=c}"


def test_compare_baseline_out_of_retention_says_so(make_client: ClientFactory) -> None:
    """AC 4 — 창 단위: 녹화한 `offset 5184000s`(60d, 보존 밖) 응답에는 `base` 시계열이 부재다(0 이 아니다) → baseline_empty,
    렌더링에 "기준선 없음" 이 명시되고 비율은 어디에도 없다. 지금 창의 값은 남는다."""
    client, _ = make_client({"api/v1/query?": (200, load_fixture("promql-instant-compare-cpu-by-pod-no-baseline.json"))})
    ev, _ = metrics.compare(client, CPU_QUERY, Window.last("1h", at=AT).with_baseline("60d"))
    v = ev.value
    assert v.offset == "5184000s" and v.baseline_empty and not v.now_empty and len(v.deltas) == 20
    assert all(d.ratio is None and d.base is None and d.now is not None for d in v.deltas)
    lines = cli.compare_lines(v, ev.window or Window(0, 0))
    assert lines[1].startswith("기준선 없음 — ") and "0 으로 나누지 않는다" in lines[1]
    assert all(l.startswith("기준선 없음  now p95=") for l in lines[2:]) and not any("×" in l for l in lines[1:])


def test_compare_both_empty(make_client: ClientFactory) -> None:
    client, _ = make_client({"api/v1/query?": (200, load_fixture("promql-instant-empty.json"))})
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
    client, _ = make_client({"api/v1/query?": (200, load_fixture("promql-instant-empty.json"))})
    ev, _ = metrics.top(client, "nothing", Window.last("1h", at=AT))
    assert ev.value.entries == []
    lines = cli.top_lines(ev.value, ev.window or Window(0, 0))
    assert len(lines) == 1 and lines[0].startswith("(데이터 없음")


# ── CLI 서브커맨드 ────────────────────────────────────────────────────────────────

def test_metrics_run_dispatch(make_client: ClientFactory) -> None:
    """metrics_run — 세 서브커맨드가 Evidence[Lines] 와 kind 를 내고, compare 의 Evidence.window 에 기준선이 달리며, top 의 query 는 감싼 질의."""
    client, _ = make_client({"count_over_time": (200, load_fixture("promql-instant-summarize-cpu-by-pod.json")),
                             "offset": (200, load_fixture("promql-instant-compare-cpu-by-pod-1d.json")),
                             "topk": (200, load_fixture("promql-instant-topk-memory-by-pod.json"))})
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
        assert "--baseline" in text and "prev" in text and "offset" in text
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
