"""doctor 의 출력 — 물리가 보이는 유일한 렌더링."""
from __future__ import annotations

from obs.adapters import cli
from obs.core import DoctorReport, Probe, Resolved


def test_doctor_lines_show_url_and_hide_token() -> None:
    """AC 2 — obs-doctor.py 렌더링에는 URL·uid·출처가 보이고(물리가 보이는 유일한 출력), 토큰은 있음/없음만이며, 실패 probe 는 ✗·HTTP 상태·문구와 "문제 있음" 결론으로 이어진다."""
    rep = DoctorReport(
        grafana_url=Resolved("https://gw.example", "default"), proxy_prefix=Resolved("/p", "default"),
        uids={"metrics": Resolved("prom", "env:OBS_UID_METRICS")}, config_path=None, config_candidates=["/x/config.json"],
        token_present=True, spill_dir="/proj/tmp/obs",
        probes=[Probe("metrics", "prom", "https://gw.example/p/prom/api", False, 401, "metrics 백엔드 인증 실패(401)")],
    )
    text = "\n".join(cli.doctor_lines(rep))
    assert "https://gw.example" in text and "uid metrics   prom  (env:OBS_UID_METRICS)" in text
    assert "token         있음 (env)" in text and "✗ metrics  HTTP 401" in text and "결론: 문제 있음" in text
