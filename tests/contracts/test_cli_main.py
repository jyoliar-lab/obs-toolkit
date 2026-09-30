"""CLI 진입점 중 하위층이 없는 것 — main_guard(ObsError → stderr · exit 1)와 doctor_main(exit 0/1 · --json).
promql_main · logql_main · traceql_main 은 argparse → *_run → emit 의 배선뿐이라 여기서 다시 검사하지 않는다 — *_run 은 contracts · format 이,
emit 은 test_cli.py 가, 세 파서의 구성은 --help 테스트가 이미 지난다."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Mapping

import pytest

from obs.adapters import cli
from obs.core import Config, DoctorReport, ObsError, Probe, Resolved, Transport, to_dict


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
