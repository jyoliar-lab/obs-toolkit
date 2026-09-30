"""Config 해석 순서 — env → 설정 파일 → 내장 기본값. 토큰은 env 에서만."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from obs.core import (
    DEFAULT_GRAFANA_URL, DEFAULT_UIDS, Client, Config, ObsError, config_file_candidates, load_config, to_dict, token_from_env,
)


def _write(p: Path, data: dict[str, object]) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_defaults_when_nothing_set(base_env: dict[str, str], tmp_path: Path) -> None:
    """env 도 설정 파일도 없으면 내장 기본값이 쓰이고 모든 값의 출처가 "default" 다."""
    cfg = load_config(base_env, home=tmp_path / "home")
    assert cfg.grafana_url.value == DEFAULT_GRAFANA_URL
    assert cfg.grafana_url.source == "default"
    assert {k: v.value for k, v in cfg.uids.items()} == DEFAULT_UIDS
    assert all(v.source == "default" for v in cfg.uids.values())
    assert cfg.config_path is None


def test_file_overrides_default(base_env: dict[str, str], tmp_path: Path) -> None:
    """설정 파일의 값은 기본값을 덮고 출처는 "file:<경로>" 이며, 파일에 없는 키는 여전히 기본값이다."""
    p = _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json",
               {"grafana_url": "http://grafana.svc", "uids": {"logs": "loki-other"}})
    cfg = load_config(base_env, home=tmp_path / "home")
    assert cfg.grafana_url.value == "http://grafana.svc"
    assert cfg.grafana_url.source == "file:%s" % p
    assert cfg.uids["logs"].value == "loki-other"
    assert cfg.uids["logs"].source.startswith("file:")
    assert cfg.uids["metrics"].value == DEFAULT_UIDS["metrics"]
    assert cfg.uids["metrics"].source == "default"
    assert cfg.config_path == str(p)


def test_env_overrides_file(base_env: dict[str, str], tmp_path: Path) -> None:
    """env 의 값은 설정 파일 값까지 덮고 출처는 "env:<이름>" 이다."""
    _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json",
           {"grafana_url": "http://from-file", "uids": {"metrics": "from-file"}})
    env = {**base_env, "OBS_GRAFANA_URL": "http://from-env", "OBS_UID_METRICS": "from-env"}
    cfg = load_config(env, home=tmp_path / "home")
    assert cfg.grafana_url.value == "http://from-env"
    assert cfg.grafana_url.source == "env:OBS_GRAFANA_URL"
    assert cfg.uids["metrics"].value == "from-env"
    assert cfg.uids["metrics"].source == "env:OBS_UID_METRICS"


def test_candidate_order(base_env: dict[str, str], tmp_path: Path) -> None:
    """설정 파일 후보는 OBS_CONFIG 하나 · 아니면 ${CLAUDE_PLUGIN_DATA}/config.json → ~/.config/obs-toolkit/config.json 순이다."""
    home = tmp_path / "home"
    cands = config_file_candidates(base_env, home)
    assert cands == [Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", home / ".config" / "obs-toolkit" / "config.json"]
    explicit = {**base_env, "OBS_CONFIG": str(tmp_path / "x.json")}
    assert config_file_candidates(explicit, home) == [tmp_path / "x.json"]
    no_data = {k: v for k, v in base_env.items() if k != "CLAUDE_PLUGIN_DATA"}
    assert config_file_candidates(no_data, home) == [home / ".config" / "obs-toolkit" / "config.json"]


def test_home_config_used_when_plugin_data_absent(base_env: dict[str, str], tmp_path: Path) -> None:
    """플러그인 데이터 디렉터리에 파일이 없으면 홈의 설정 파일이 읽힌다."""
    home = tmp_path / "home"
    _write(home / ".config" / "obs-toolkit" / "config.json", {"uids": {"traces": "tempo-home"}})
    cfg = load_config(base_env, home=home)
    assert cfg.uids["traces"].value == "tempo-home"


def test_config_file_token_is_ignored(base_env: dict[str, str], tmp_path: Path) -> None:
    """설정 파일의 token 키는 무시된다 — 해석된 Config 를 직렬화해도 그 값이 어디에도 실리지 않는다."""
    _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", {"token": "file-token-must-be-ignored"})
    cfg = load_config(base_env, home=tmp_path / "home")
    assert "file-token" not in json.dumps(to_dict(cfg))


def test_token_from_env_is_empty_when_absent(base_env: dict[str, str]) -> None:
    """token_from_env 는 env 의 값을 돌려주고, 없으면 예외가 아니라 빈 문자열이다 — 판단은 Client.from_env 가 한다."""
    assert token_from_env(base_env) == "test-token-not-real"
    assert token_from_env({}) == ""


def test_client_from_env_requires_token(base_env: dict[str, str]) -> None:
    """토큰이 없으면 Client.from_env 가 거부하고 오류 문구가 env 이름(OBS_GRAFANA_TOKEN)을 말한다."""
    env = {k: v for k, v in base_env.items() if k != "OBS_GRAFANA_TOKEN"}
    with pytest.raises(ObsError) as ei:
        Client.from_env(env)
    assert "OBS_GRAFANA_TOKEN" in str(ei.value)


def test_non_string_config_value_is_an_error(base_env: dict[str, str], tmp_path: Path) -> None:
    """설정 파일의 값이 문자열이 아니면 조용히 str() 하지 않고 ObsError 로 거부한다."""
    _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", {"uids": {"metrics": 42}})
    with pytest.raises(ObsError):
        load_config(base_env, home=tmp_path / "home")


def test_bad_config_file_is_an_error(base_env: dict[str, str], tmp_path: Path) -> None:
    """깨진 설정 파일은 기본값으로 넘어가지 않고 ObsError 다."""
    p = Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ObsError):
        load_config(base_env, home=tmp_path / "home")


def test_unknown_backend(config: Config) -> None:
    """논리 이름은 metrics · logs · traces 셋뿐이고 그 밖의 이름으로 uid 를 물으면 ObsError 다."""
    with pytest.raises(ObsError):
        config.uid("nope")


# ── 설정 엣지 — 형태가 틀린 파일 · 빈 값 ─────────────────────────────────────────

def test_empty_file_value_falls_back_to_default(base_env: dict[str, str], tmp_path: Path) -> None:
    """설정 파일의 값이 빈 문자열이면 없는 것과 같다 — 기본값이 쓰이고 출처가 default 다."""
    _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", {"grafana_url": "", "proxy_prefix": "", "uids": {"logs": ""}})
    cfg = load_config(base_env, home=tmp_path / "home")
    assert cfg.grafana_url.source == "default"
    assert cfg.proxy_prefix.source == "default"
    assert cfg.uids["logs"].source == "default"
    assert cfg.uids["logs"].value == DEFAULT_UIDS["logs"]


def test_uids_not_an_object_is_an_error(base_env: dict[str, str], tmp_path: Path) -> None:
    """uids 가 객체가 아니면 조용히 무시하지 않고 ObsError 로 거부한다."""
    _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", {"uids": "prometheus"})
    with pytest.raises(ObsError, match="uids"):
        load_config(base_env, home=tmp_path / "home")


def test_top_level_not_an_object_is_an_error(base_env: dict[str, str], tmp_path: Path) -> None:
    """설정 파일의 최상위가 객체가 아니면(목록) ObsError 다 — 기본값으로 넘어가지 않는다."""
    p = Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json"
    p.parent.mkdir(parents=True)
    p.write_text("[]", encoding="utf-8")
    with pytest.raises(ObsError, match="최상위"):
        load_config(base_env, home=tmp_path / "home")


# ── 명시 경로(OBS_CONFIG) — 사람이 놓은 경로는 몰래 버려지지 않는다 ──────────────────

@pytest.mark.parametrize("given", ["missing", "directory", "missing-with-implicit"])
def test_explicit_config_not_a_file_is_an_error(base_env: dict[str, str], tmp_path: Path, given: str) -> None:
    """OBS_CONFIG 가 파일이 아닌 경로를 가리키면 기본값 · 암묵 후보로 넘어가지 않고 ObsError 이며, 문구가 그 경로와 OBS_CONFIG 를 말하고 "사람에게" 로 끝난다."""
    # given — 명시 경로가 파일이 아니다(없음 · 디렉터리 · 없음 + 암묵 후보에는 유효 파일)
    explicit = tmp_path / "explicit" / "obs-config.json"
    if given == "directory":
        explicit.mkdir(parents=True)
    if given == "missing-with-implicit":
        _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", {"grafana_url": "http://implicit"})
    env = {**base_env, "OBS_CONFIG": str(explicit)}
    # when
    with pytest.raises(ObsError) as ei:
        load_config(env, home=tmp_path / "home")
    # then
    msg = str(ei.value)
    assert str(explicit) in msg
    assert "OBS_CONFIG" in msg
    assert msg.endswith("사람에게")


def test_explicit_config_file_is_read(base_env: dict[str, str], tmp_path: Path) -> None:
    """OBS_CONFIG 가 유효 파일을 가리키면 그 파일의 값이 쓰이고 출처가 "file:<그 경로>" 다."""
    # given — 명시 경로에 유효 파일
    p = _write(tmp_path / "explicit" / "obs-config.json", {"grafana_url": "http://explicit"})
    env = {**base_env, "OBS_CONFIG": str(p)}
    # when
    cfg = load_config(env, home=tmp_path / "home")
    # then
    assert cfg.grafana_url.value == "http://explicit"
    assert cfg.grafana_url.source == "file:%s" % p


def test_blank_explicit_config_is_unset(base_env: dict[str, str], tmp_path: Path) -> None:
    """OBS_CONFIG 가 공백뿐이면 미설정과 같다 — 암묵 후보가 전부 없으면 기본값이고 config_path 가 없다."""
    # given — OBS_CONFIG 는 공백뿐 · 암묵 후보 전부 없음
    env = {**base_env, "OBS_CONFIG": "   "}
    # when
    cfg = load_config(env, home=tmp_path / "home")
    # then
    assert cfg.grafana_url.source == "default"
    assert cfg.config_path is None
