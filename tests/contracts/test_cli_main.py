"""CLI 진입점의 약속 — *_main 이 _client() 를 거쳐 *_run 을 부르고 stdout 을 절제한다(--json · --max-lines) · main_guard 의 stderr · exit 1 ·
doctor_main 의 exit 0/1 · --json. 계산 · 모양은 다시 검사하지 않는다 — 디스패치와 stdout 절제만."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Mapping

import pytest

from obs.adapters import cli
from obs.core import Config, DoctorReport, ObsError, Probe, Resolved, Transport, to_dict
from tests.conftest import ClientFactory, FakeTransport, RouteKey, _vector
from tests.contracts.test_cli import SEARCH, STREAMS

ROUTES: dict[RouteKey, tuple[int, object]] = {
    ("metrics", "api/v1/query"): (200, _vector(({"pod": "a"}, "1"), ({"pod": "b"}, "2"), ({"pod": "c"}, "3"))),
    ("logs", "loki/api/v1/query_range"): (200, STREAMS),
    ("traces", "api/search"): (200, SEARCH),
}
"""세 명령의 기본 경로 — 진입점이 *_run 에 닿는지만 본다."""

REF_PREFIX: str = "원본·요약: tmp/obs/"


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, base_env: dict[str, str]) -> Path:
    """*_main 이 os.environ 에서 읽는 것(spill 자리 · 설정 후보 · 토큰)을 tmp 로 고정한다. 돌려주는 값은 spill 의 기준 디렉터리다."""
    for k, v in base_env.items():
        monkeypatch.setenv(k, v)
    return Path(base_env["CLAUDE_PROJECT_DIR"])


@pytest.fixture
def patched_client(monkeypatch: pytest.MonkeyPatch, make_client: ClientFactory) -> FakeTransport:
    """_client() 가 ROUTES 를 가진 FakeTransport 의 Client 를 돌려주게 한다 — 나간 요청은 돌려준 transport 의 calls 에 남는다."""
    client, t = make_client(ROUTES)
    monkeypatch.setattr(cli, "_client", lambda: client)
    return t


def _stdout_lines(capsys: pytest.CaptureFixture[str]) -> list[str]:
    return capsys.readouterr().out.splitlines()


def test_promql_main_prints_summary_then_ref(patched_client: FakeTransport, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """promql_main 은 즉시 조회의 요약 줄을 찍고 마지막 줄에 호출 디렉터리를 안내한다 — 그 디렉터리에 raw.json · summary.txt 가 있다."""
    cli.promql_main(["up"])
    out = _stdout_lines(capsys)
    assert len(out) == 1 + 3
    assert out[-1].startswith(REF_PREFIX)
    ref = cli_env / out[-1][len("원본·요약: "):].split(" ", 1)[0].rstrip("/")
    assert sorted(p.name for p in ref.iterdir()) == ["raw.json", "summary.txt"]


def test_promql_main_json_prints_only_ref(patched_client: FakeTransport, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--json 은 stdout 에 요약 없이 호출 디렉터리 한 줄만 찍는다."""
    cli.promql_main(["up", "--json"])
    out = _stdout_lines(capsys)
    assert len(out) == 1
    assert out[0].startswith(REF_PREFIX)


def test_promql_main_max_lines_truncates_and_tells_the_rest(patched_client: FakeTransport, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--max-lines N 은 요약을 N 줄에서 자르고 몇 줄이 더 있는지 알린다."""
    cli.promql_main(["up", "--max-lines", "1"])
    out = _stdout_lines(capsys)
    assert len(out) == 2
    assert out[-1].startswith("… 2줄 더 있음 (총 3줄)")


def test_promql_main_subcommand_goes_to_metrics_main(patched_client: FakeTransport, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """첫 인자가 summarize · compare · top 이면 metrics_main 이 받아 조립된 질의로 api/v1/query 를 한 번 부른다."""
    cli.promql_main(["top", "up", "--by", "pod", "--k", "2"])
    assert len(patched_client.calls) == 1
    assert "topk" in patched_client.calls[0].url


def test_logql_main_reaches_query_range(patched_client: FakeTransport, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """logql_main 은 질의를 query_range 로 한 번 보내고 요약 뒤 경로 한 줄을 찍는다."""
    cli.logql_main(['{namespace="n"}', "--limit", "5"])
    assert len(patched_client.calls) == 1
    assert "loki/api/v1/query_range?" in patched_client.calls[0].url
    assert _stdout_lines(capsys)[-1].startswith(REF_PREFIX)


def test_traceql_main_reaches_search(patched_client: FakeTransport, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """traceql_main 은 search 를 api/search 로 한 번 보내고 요약 뒤 경로 한 줄을 찍는다."""
    cli.traceql_main(["search", "--since", "24h"])
    assert len(patched_client.calls) == 1
    assert "/api/search?" in patched_client.calls[0].url
    assert _stdout_lines(capsys)[-1].startswith(REF_PREFIX)


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
def test_doctor_main_exit_code_follows_report(monkeypatch: pytest.MonkeyPatch, cli_env: Path, capsys: pytest.CaptureFixture[str], ok: bool, code: int) -> None:
    """doctor_main 은 보고서가 정상이면 exit 0, 아니면 exit 1 이고 stdout 마지막 줄이 결론이다."""
    monkeypatch.setattr(cli, "doctor", _fake_doctor(_report(ok)))
    with pytest.raises(SystemExit) as ei:
        cli.doctor_main([])
    assert ei.value.code == code
    assert _stdout_lines(capsys)[-1].startswith("결론: ")


def test_doctor_main_json_prints_report_as_json(monkeypatch: pytest.MonkeyPatch, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--json 은 보고서를 to_dict 그대로의 JSON 으로 찍고 exit 코드는 같은 규칙이다."""
    monkeypatch.setattr(cli, "doctor", _fake_doctor(_report(False)))
    with pytest.raises(SystemExit) as ei:
        cli.doctor_main(["--json"])
    assert ei.value.code == 1
    assert json.loads(capsys.readouterr().out) == to_dict(_report(False))
