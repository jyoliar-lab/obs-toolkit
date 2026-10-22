"""CLI 진입점 중 하위층이 없는 것 — main_guard(ObsError → stderr · exit 1)와 doctor_main(exit 0/1 · --json).
promql_main · logql_main · traceql_main 은 argparse → *_run → emit 의 배선뿐이라 여기서 다시 검사하지 않는다 — *_run 은 contracts · format 이,
emit 은 test_cli.py 가, 세 파서의 구성은 --help 테스트가 이미 지난다."""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path

import pytest

from obs.adapters import cli
from obs.core import JSON, ObsError, Window, json_list, json_obj, spill_root
from obs.metrics import DEFAULT_TOP_AGG, DEFAULT_TOP_K
from tests.conftest import AT, ClientFactory, Config, MainFn, MainRunner, RouteKey, _vector
from tests.record import strip_physical


def test_main_guard_prints_obs_error_to_stderr_and_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """main_guard 는 ObsError 의 문구만 stderr 에 한 줄로 찍고 exit 1 이다 — stdout 은 비어 있다."""
    def fn() -> None:
        raise ObsError("metrics 백엔드에 닿지 못했다")
    with pytest.raises(SystemExit) as ei:
        cli.main_guard(fn)
    assert ei.value.code == 1
    captured = capsys.readouterr()
    assert captured.err == "metrics 백엔드에 닿지 못했다\n"
    assert captured.out == ""


def _doctor_routes(traces_status: int) -> dict[RouteKey, tuple[int, object]]:
    """doctor 의 probe 셋 — traces 만 상태를 바꿔 준다(tests/contracts/test_core.py 의 것과 같은 셋)."""
    return {("metrics", "api/v1/status/buildinfo"): (200, {}), ("logs", "loki/api/v1/labels"): (200, {}), ("traces", "api/echo"): (traces_status, b"")}


@pytest.mark.parametrize("traces_status,code", [(200, 0), (401, 1)], ids=["ok", "fail"])
def test_doctor_main_exit_code_follows_report(base_env: dict[str, str], make_client: ClientFactory, capsys: pytest.CaptureFixture[str],
                                              traces_status: int, code: int) -> None:
    """doctor_main 은 probe 를 인자 transport 로 백엔드마다 1회 보내고, 보고서가 정상이면 exit 0, 아니면 exit 1 이며 stdout 마지막 줄이 결론이다."""
    # given — 명시 env(base_env · 토큰 있음) · probe 라우트 셋(traces 만 상태가 갈린다)
    _, t = make_client(_doctor_routes(traces_status))
    # when
    with pytest.raises(SystemExit) as ei:
        cli.doctor_main([], base_env, t)
    # then
    assert ei.value.code == code
    assert len(t.calls) == 3
    assert sorted((rq.backend, rq.path) for rq in (strip_physical(c.url, t.config, None) for c in t.calls)) == sorted(_doctor_routes(traces_status))
    assert capsys.readouterr().out.splitlines()[-1].startswith("결론: ")


def test_doctor_main_reads_token_and_config_from_env(base_env: dict[str, str], make_client: ClientFactory, capsys: pytest.CaptureFixture[str]) -> None:
    """doctor_main 의 토큰 · 설정은 인자 env 에서 읽힌다 — 토큰 없는 env 면 probe 요청 0 · exit 1 이고, 그 env 의 OBS_GRAFANA_URL 이 보고서에 보인다."""
    # given — 토큰 없음 · 게이트웨이 URL 은 손으로 만든 값 · 라우트 없는 fake transport
    env = {k: v for k, v in base_env.items() if k != "OBS_GRAFANA_TOKEN"}
    env["OBS_GRAFANA_URL"] = "https://gw.example"
    _, t = make_client({})
    # when
    with pytest.raises(SystemExit) as ei:
        cli.doctor_main([], env, t)
    # then
    assert t.calls == []
    assert ei.value.code == 1
    out = capsys.readouterr().out
    assert "gw.example" in out
    url_lines = [line for line in out.splitlines() if line.startswith("grafana_url ")]
    assert len(url_lines) == 1
    assert "https://gw.example" in url_lines[0]


def test_doctor_main_json_is_report_dict(base_env: dict[str, str], make_client: ClientFactory, capsys: pytest.CaptureFixture[str]) -> None:
    """--json 은 보고서 하나를 JSON 으로 찍는다 — probes 가 백엔드 수(3)만큼 · token_present 가 true · 토큰 값은 없고 · exit 코드는 같은 규칙(traces 401 → 1)."""
    # given — 토큰 있는 env · traces 만 401
    _, t = make_client(_doctor_routes(401))
    # when
    with pytest.raises(SystemExit) as ei:
        cli.doctor_main(["--json"], base_env, t)
    # then
    out = capsys.readouterr().out
    rep = json_obj(json.loads(out))
    assert ei.value.code == 1
    assert len(json_list(rep.get("probes"))) == 3
    assert rep.get("token_present") is True
    assert base_env["OBS_GRAFANA_TOKEN"] not in out


def test_doctor_main_exits_1_on_missing_explicit_config(base_env: dict[str, str], make_client: ClientFactory, tmp_path: Path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    """env 의 OBS_CONFIG 가 없는 파일을 가리키면 obs-doctor.py 의 배선(main_guard(doctor_main))은 exit 1 이고 stderr 가 OBS_CONFIG 를 말하며 probe 는 나가지 않는다."""
    # given — OBS_CONFIG 는 없는 파일 · 정상 라우트
    env = {**base_env, "OBS_CONFIG": str(tmp_path / "nonexistent" / "obs-config.json")}
    _, t = make_client(_doctor_routes(200))
    # when
    with pytest.raises(SystemExit) as ei:
        cli.main_guard(lambda: cli.doctor_main([], env, t))
    # then
    assert ei.value.code == 1
    assert "OBS_CONFIG" in capsys.readouterr().err
    assert t.calls == []


# ── 실패한 명령의 배선 — main 이 만든 Client 하나를 run 과 finish 가 같이 쓴다 ──────────

MAIN_FAILS: list[tuple[str, MainFn, list[str], RouteKey]] = [
    ("promql", cli.promql_main, ["up"], ("metrics", "api/v1/query")),
    ("promql-summarize", cli.promql_main, ["summarize", "up"], ("metrics", "api/v1/query")),
    ("logql", cli.logql_main, ['{a="b"}'], ("logs", "loki/api/v1/query_range")),
    ("logql-templates", cli.logql_main, ["templates", '{a="b"}'], ("logs", "loki/api/v1/patterns")),
    ("traceql", cli.traceql_main, ["search"], ("traces", "api/search")),
    ("traceql-exemplars", cli.traceql_main, ["exemplars"], ("metrics", "api/v1/query")),
]


@pytest.mark.parametrize("name,main,argv,route", MAIN_FAILS, ids=[m[0] for m in MAIN_FAILS])
def test_main_failure_leaves_call_directory(run_main: MainRunner, base_env: dict[str, str], make_client: ClientFactory,
                                            capsys: pytest.CaptureFixture[str], name: str, main: MainFn, argv: list[str],
                                            route: RouteKey) -> None:
    """첫 호출이 400 으로 끝난 명령은 exit 1 이고 stderr 가 <명령>-failed 디렉터리 경로를 말하며, 그 raw.json 에 그 호출 하나(status 400 · 본문 전체)가 있다."""
    # given — 첫 호출 경로에 400 을 주는 대역 Client · 명시 env(base_env)
    body: dict[str, JSON] = {"status": "error", "error": "parse error"}
    client, _ = make_client({route: (400, body)})
    # when
    with pytest.raises(SystemExit) as ei:
        cli.main_guard(lambda: run_main(main, argv, client))
    # then
    assert ei.value.code == 1
    err = capsys.readouterr().err
    root = spill_root(base_env)
    assert root.is_dir()
    dirs = sorted(root.iterdir())
    assert len(dirs) == 1
    assert dirs[0].name.endswith("-%s-failed" % name)
    assert dirs[0].name in err
    assert "%s/" % dirs[0].name in err
    items = json.loads((dirs[0] / "raw.json").read_text())
    assert len(items) == 1
    assert items[0]["status"] == 400
    assert items[0]["body"] == body


# ── 주입 — 프로세스를 모른다: client 함수 · env · at 가 전부 인자 ───────────────────────

INJECT_CASES: list[tuple[str, MainFn, list[str], dict[RouteKey, tuple[int, object]]]] = [
    ("promql", cli.promql_main, ["up"], {("metrics", "api/v1/query"): (200, _vector())}),
    ("logql", cli.logql_main, ['{a="b"}'], {("logs", "loki/api/v1/query_range"): (200, {"status": "success", "data": {"resultType": "streams", "result": []}})}),
    ("traceql", cli.traceql_main, ["search"], {("traces", "api/search"): (200, {"traces": []})}),
]


@pytest.mark.parametrize("main,argv,routes", [c[1:] for c in INJECT_CASES], ids=[c[0] for c in INJECT_CASES])
def test_main_uses_injected_client_without_token_in_env(main: MainFn, argv: list[str], routes: dict[RouteKey, tuple[int, object]],
                                                        base_env: dict[str, str], make_client: ClientFactory) -> None:
    """*_main 은 client 함수가 돌려준 Client 로 요청을 보낸다 — env 에 토큰 · OBS_CONFIG 가 없어도 그 transport 에 요청 1회가 나가고 예외가 없다."""
    # given — 토큰 · OBS_CONFIG 없는 env · fake transport 의 Client 를 돌려주는 함수
    env = {k: v for k, v in base_env.items() if k not in ("OBS_GRAFANA_TOKEN", "OBS_CONFIG")}
    client, t = make_client(routes)
    # when
    main(argv, env, AT, lambda: client)
    # then
    assert len(t.calls) == 1


def test_main_spills_under_env_project_dir(run_main: MainRunner, base_env: dict[str, str], make_client: ClientFactory) -> None:
    """호출 디렉터리는 명시한 env 의 CLAUDE_PROJECT_DIR 아래 tmp/obs 에 정확히 하나 생긴다 — 프로세스 env 의 값은 쓰이지 않는다."""
    # given — base_env 의 CLAUDE_PROJECT_DIR 은 tmp 아래(아직 없다) · 빈 응답
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    root = spill_root(base_env)
    assert not root.exists()
    # when
    run_main(cli.promql_main, ["up"], client)
    # then
    assert len(list(root.iterdir())) == 1


def test_main_window_end_is_at(run_main: MainRunner, base_env: dict[str, str], make_client: ClientFactory) -> None:
    """--end 를 주지 않으면 창의 끝이 인자 at 이다 — 요청의 time 파라미터와 summary.txt 의 `# 창` 줄 end 가 AT."""
    # given — 빈 응답 · at 은 AT(run_main 기본)
    client, t = make_client({("metrics", "api/v1/query"): (200, _vector())})
    # when
    run_main(cli.promql_main, ["up"], client)
    # then
    sent = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(t.calls[0].url).query))
    assert float(sent["time"]) == AT
    summary = next(spill_root(base_env).glob("*/summary.txt")).read_text(encoding="utf-8").splitlines()
    assert summary[0].startswith("# 창")
    assert "end=%.3f" % AT in summary[0]


def test_main_call_directory_named_by_at(run_main: MainRunner, base_env: dict[str, str], make_client: ClientFactory) -> None:
    """호출 디렉터리 이름의 시각은 인자 at 의 UTC 표기다 — AT 이면 19700102T034640.000000Z-promql."""
    # given — 빈 응답 · at 은 AT
    client, _ = make_client({("metrics", "api/v1/query"): (200, _vector())})
    # when
    run_main(cli.promql_main, ["up"], client)
    # then
    assert [p.name for p in spill_root(base_env).iterdir()] == ["19700102T034640.000000Z-promql"]


# ── 옵션 → 요청 — 종류마다 대표 하나. 매핑 · 선언이 어디 있든 요청은 같아야 한다 ────────────

EMPTY_LABELS: dict[str, JSON] = {"status": "success", "data": []}
EMPTY_STREAMS: dict[str, JSON] = {"status": "success", "data": {"resultType": "streams", "result": []}}
EMPTY_PATTERNS: dict[str, JSON] = {"status": "success", "data": []}

OPTION_CASES: list[tuple[str, MainFn, list[str], dict[RouteKey, tuple[int, object]], RouteKey, dict[str, str], list[str]]] = [
    # (옵션 종류, main, 그 종류를 명시한 argv, 라우트, 첫 요청의 (backend, path), 요청에 그대로 있어야 하는 파라미터, query 파라미터에 들어 있어야 하는 조각)
    ("positional", cli.promql_main, ["up"], {("metrics", "api/v1/query"): (200, _vector())}, ("metrics", "api/v1/query"), {"query": "up"}, []),
    ("flag", cli.promql_main, ["up", "--labels"], {("metrics", "api/v1/labels"): (200, EMPTY_LABELS)}, ("metrics", "api/v1/labels"), {"match[]": "up"}, []),
    ("valued", cli.logql_main, ['{a="b"}', "--limit", "7", "--direction", "forward"], {("logs", "loki/api/v1/query_range"): (200, EMPTY_STREAMS)},
     ("logs", "loki/api/v1/query_range"), {"limit": "7", "direction": "forward"}, []),
    ("subcommand-valued", cli.promql_main, ["top", "up", "--by", "ns", "--k", "3"], {("metrics", "api/v1/query"): (200, _vector())},
     ("metrics", "api/v1/query"), {}, ["topk(3,", "by (ns)"]),
]


def _first_request(t_calls_url: str, config: Config) -> tuple[RouteKey, dict[str, str]]:
    """나간 첫 요청의 (backend, path) 와 문자열 파라미터."""
    rq = strip_physical(t_calls_url, config, None)
    return (rq.backend, rq.path), {k: str(v) for k, v in rq.params.items()}


@pytest.mark.parametrize("main,argv,routes,route,expected,fragments", [c[1:] for c in OPTION_CASES], ids=[c[0] for c in OPTION_CASES])
def test_main_option_reaches_request(main: MainFn, argv: list[str], routes: dict[RouteKey, tuple[int, object]], route: RouteKey,
                                     expected: dict[str, str], fragments: list[str], run_main: MainRunner, make_client: ClientFactory,
                                     config: Config) -> None:
    """명시한 옵션(위치 인자 · 플래그 · 값 있는 옵션 · 서브커맨드의 값 있는 옵션)의 효과가 나간 요청의 경로 · 파라미터에 있다."""
    # given — 그 종류의 옵션을 명시한 argv · 첫 요청 경로에 빈 응답
    client, t = make_client(routes)
    # when
    run_main(main, argv, client)
    # then
    got_route, params = _first_request(t.calls[0].url, config)
    assert got_route == route
    assert {k: v for k, v in params.items() if k in expected} == expected
    for fragment in fragments:
        assert fragment in params["query"]


DEFAULT_WINDOW: Window = Window.last(cli.DEFAULT_SINCE, None, at=AT)

OMITTED_CASES: list[tuple[str, MainFn, list[str], dict[RouteKey, tuple[int, object]], dict[str, str], list[str]]] = [
    # (옵션 종류, main, 그 종류를 생략한 argv, 라우트, 요청에 있어야 하는 기본값 파라미터, query 파라미터의 기본값 조각)
    ("valued", cli.logql_main, ['{a="b"}'], {("logs", "loki/api/v1/query_range"): (200, EMPTY_STREAMS)},
     {"limit": str(cli.DEFAULT_LOG_LIMIT), "direction": "backward"}, []),
    ("subcommand-valued", cli.promql_main, ["top", "up"], {("metrics", "api/v1/query"): (200, _vector())},
     {}, ["topk(%d," % DEFAULT_TOP_K, "%s by (pod)" % DEFAULT_TOP_AGG]),
    ("common", cli.logql_main, ["templates", '{a="b"}'], {("logs", "loki/api/v1/patterns"): (200, EMPTY_PATTERNS)},
     {"start": str(int(DEFAULT_WINDOW.start * 1e9)), "end": str(int(DEFAULT_WINDOW.end * 1e9))}, []),
]


@pytest.mark.parametrize("main,argv,routes,expected,fragments", [c[1:] for c in OMITTED_CASES], ids=[c[0] for c in OMITTED_CASES])
def test_main_omitted_option_uses_default(main: MainFn, argv: list[str], routes: dict[RouteKey, tuple[int, object]], expected: dict[str, str],
                                          fragments: list[str], run_main: MainRunner, make_client: ClientFactory, config: Config) -> None:
    """생략한 옵션(값 있는 옵션 · 서브커맨드의 값 있는 옵션 · 서브커맨드 공통 옵션)은 요청에 그 기본값으로 나타난다 — limit 30 · backward · k 10 · pod · sum · 1h 창."""
    # given — 그 종류의 옵션을 생략한 argv · 첫 요청 경로에 빈 응답 · at 은 AT
    client, t = make_client(routes)
    # when
    run_main(main, argv, client)
    # then
    _, params = _first_request(t.calls[0].url, config)
    assert {k: v for k, v in params.items() if k in expected} == expected
    for fragment in fragments:
        assert fragment in params["query"]


def test_main_omitted_query_is_rejected_before_request(run_main: MainRunner, make_client: ClientFactory) -> None:
    """위치 인자(PromQL)를 생략한 promql.py 는 요청 없이 ObsError 다 — nargs="?" 의 생략이 query=None 으로 *_run 에 닿아 거기서 거부된다(argparse 의 exit 2 가 아니다)."""
    # given — 라우트 없는 fake transport · 빈 argv
    client, t = make_client()
    # when · then
    with pytest.raises(ObsError, match="PromQL 이 필요하다"):
        run_main(cli.promql_main, [], client)
    assert t.calls == []


THREE_SERIES: dict[str, JSON] = _vector(({"pod": "a"}, "1"), ({"pod": "b"}, "2"), ({"pod": "c"}, "3"))


@pytest.mark.parametrize("option,shown,tail", [(["--max-lines", "1"], 1, "… 2줄 더 있음"), (["--json"], 0, "원본·요약:")], ids=["max-lines", "json"])
def test_main_stdout_follows_max_lines_and_json(option: list[str], shown: int, tail: str, run_main: MainRunner, base_env: dict[str, str],
                                                make_client: ClientFactory, capsys: pytest.CaptureFixture[str]) -> None:
    """--max-lines 1 이면 stdout 은 요약 1줄 + "… 2줄 더 있음" 안내, --json 이면 경로 한 줄뿐이다 — 어느 쪽이든 summary.txt 에는 창 머리 뒤 3줄 전부가 있다."""
    # given — 시계열 셋의 즉시 조회 응답 · base_env
    client, _ = make_client({("metrics", "api/v1/query"): (200, THREE_SERIES)})
    # when
    run_main(cli.promql_main, ["up", *option], client)
    # then
    out = capsys.readouterr().out.splitlines()
    assert len(out) == shown + 1
    assert out[-1].startswith(tail)
    summary = next(spill_root(base_env).glob("*/summary.txt")).read_text(encoding="utf-8").splitlines()
    assert summary[0].startswith("# 창")
    assert summary[1] == ""
    assert len(summary[2:]) == 3


# ── (결과 없음) — 빈 결과의 한 줄은 finish 가 붙인다 ─────────────────────────────────

EMPTY_LOOKUPS: list[tuple[str, MainFn, list[str], RouteKey]] = [
    ("promql-labels", cli.promql_main, ["--labels"], ("metrics", "api/v1/labels")),
    ("logql-series", cli.logql_main, ["--series", '{a="b"}'], ("logs", "loki/api/v1/series")),
]


@pytest.mark.parametrize("main,argv,route", [c[1:] for c in EMPTY_LOOKUPS], ids=[c[0] for c in EMPTY_LOOKUPS])
def test_main_prints_no_result_marker_for_empty_lookup(main: MainFn, argv: list[str], route: RouteKey, run_main: MainRunner, base_env: dict[str, str],
                                                       make_client: ClientFactory, capsys: pytest.CaptureFixture[str]) -> None:
    """빈 결과의 조회는 stdout 첫 줄이 (결과 없음) 이고 그다음이 경로 한 줄뿐이며, summary.txt 도 창 머리 뒤 (결과 없음) 한 줄이다."""
    # given — 빈 목록을 주는 라우트 · base_env
    client, _ = make_client({route: (200, EMPTY_LABELS)})
    # when
    run_main(main, argv, client)
    # then
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "(결과 없음)"
    assert out[1].startswith("원본·요약:")
    assert len(out) == 2
    summary = next(spill_root(base_env).glob("*/summary.txt")).read_text(encoding="utf-8").splitlines()
    assert summary[0].startswith("# 창")
    assert summary[1:] == ["", "(결과 없음)"]
