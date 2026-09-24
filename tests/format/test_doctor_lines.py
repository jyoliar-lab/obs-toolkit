"""doctor 의 출력 — 물리가 보이는 유일한 렌더링."""
from __future__ import annotations

import pytest

from obs.adapters import cli
from obs.core import DoctorReport, Probe, Resolved
from tests.conftest import assert_snapshot


def test_doctor_lines(request: pytest.FixtureRequest) -> None:
    """doctor_lines 는 URL · uid · 출처 · 토큰 유무 · probe 결과 · 결론을 차례로 보인다."""
    rep = DoctorReport(
        grafana_url=Resolved("https://gw.example", "default"), proxy_prefix=Resolved("/p", "default"),
        uids={"metrics": Resolved("prom", "env:OBS_UID_METRICS")}, config_path=None, config_candidates=["/x/config.json"],
        token_present=True, spill_dir="/proj/tmp/obs",
        probes=[Probe("metrics", "prom", "https://gw.example/p/prom/api", False, 401, "metrics 백엔드 인증 실패(401)")],
    )
    assert_snapshot(request, cli.doctor_lines(rep))
