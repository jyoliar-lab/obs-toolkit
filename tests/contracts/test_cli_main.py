"""CLI 진입점 중 하위층이 없는 것 — main_guard(ObsError → stderr · exit 1)와 doctor_main(exit 0/1 · --json).
promql_main · logql_main · traceql_main 은 argparse → *_run → emit 의 배선뿐이라 여기서 다시 검사하지 않는다 — *_run 은 contracts · format 이,
emit 은 test_cli.py 가, 세 파서의 구성은 --help 테스트가 이미 지난다."""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Callable, Mapping

import pytest

from obs.adapters import cli
from obs.core import JSON, Config, DoctorReport, ObsError, Probe, Resolved, Transport, spill_root, to_dict
from tests.conftest import AT, ClientFactory, MainFn, MainRunner, RouteKey, _vector


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]) -> None:
    """doctor_main 이 os.environ 에서 읽는 것(설정 후보 · 토큰)을 tmp 로 고정한다."""
    for k, v in base_env.items():
        monkeypatch.setenv(k, v)


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


def _report(ok: bool) -> DoctorReport:
    """probe 하나짜리 보고서 — ok 에 따라 결론이 갈린다. 물리 값은 손으로 만든 것."""
    probe = Probe("metrics", "prom", "https://gw.example/p/prom/api", ok, 200 if ok else 401, "ok" if ok else "metrics 백엔드 인증 실패(401)")
    return DoctorReport(grafana_url=Resolved("https://gw.example", "default"), proxy_prefix=Resolved("/p", "default"),
                        uids={"metrics": Resolved("prom", "default")}, config_path=None, config_candidates=[], token_present=True,
                        spill_dir="/proj/tmp/obs", probes=[probe])


def _fake_doctor(rep: DoctorReport) -> Callable[..., DoctorReport]:
    def fake(config: Config, token: str, env: Mapping[str, str] | None = None, transport: Transport | None = None, cwd: Path | None = None) -> DoctorReport:
        return rep
    return fake


@pytest.mark.parametrize("ok,code", [(True, 0), (False, 1)])
def test_doctor_main_exit_code_follows_report(monkeypatch: pytest.MonkeyPatch, cli_env: None, capsys: pytest.CaptureFixture[str], ok: bool, code: int) -> None:
    """doctor_main 은 보고서가 정상이면 exit 0, 아니면 exit 1 이고 stdout 마지막 줄이 결론이다."""
    monkeypatch.setattr(cli, "doctor", _fake_doctor(_report(ok)))
    with pytest.raises(SystemExit) as ei:
        cli.doctor_main([])
    assert ei.value.code == code
    assert capsys.readouterr().out.splitlines()[-1].startswith("결론: ")


def test_doctor_main_json_prints_report_as_json(monkeypatch: pytest.MonkeyPatch, cli_env: None, capsys: pytest.CaptureFixture[str]) -> None:
    """--json 은 보고서를 to_dict 그대로의 JSON 으로 찍고 exit 코드는 같은 규칙이다."""
    monkeypatch.setattr(cli, "doctor", _fake_doctor(_report(False)))
    with pytest.raises(SystemExit) as ei:
        cli.doctor_main(["--json"])
    assert ei.value.code == 1
    assert json.loads(capsys.readouterr().out) == to_dict(_report(False))


def test_doctor_main_exits_1_on_missing_explicit_config(monkeypatch: pytest.MonkeyPatch, cli_env: None, tmp_path: Path) -> None:
    """OBS_CONFIG 가 없는 파일을 가리키면 obs-doctor.py 의 배선(main_guard(doctor_main))은 exit 1 이다."""
    # given — OBS_CONFIG 는 없는 파일 · doctor 는 정상 보고서를 내는 대역
    monkeypatch.setenv("OBS_CONFIG", str(tmp_path / "nonexistent" / "obs-config.json"))
    monkeypatch.setattr(cli, "doctor", _fake_doctor(_report(True)))
    # when
    with pytest.raises(SystemExit) as ei:
        cli.main_guard(lambda: cli.doctor_main([]))
    # then
    assert ei.value.code == 1


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
