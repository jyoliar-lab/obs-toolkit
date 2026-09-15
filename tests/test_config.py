"""Config 해석 순서 — env → 설정 파일 → 내장 기본값. 토큰은 env 에서만."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from obs.core import (
    DEFAULT_GRAFANA_URL, DEFAULT_UIDS, ENV_CONFIG, ENV_PROXY_PREFIX, ENV_TOKEN, ENV_UIDS, ENV_URL, Client, Config, ObsError, config_file_candidates, load_config, to_dict, token_from_env,
)


def _write(p: Path, data: dict[str, object]) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_defaults_when_nothing_set(base_env: dict[str, str], tmp_path: Path) -> None:
    """해석 순서의 바닥 — env 도 설정 파일도 없으면 내장 기본값이 쓰이고, 모든 값의 출처(source)가 "default" 로 기록된다."""
    cfg = load_config(base_env, home=tmp_path / "home")
    assert cfg.grafana_url.value == DEFAULT_GRAFANA_URL and cfg.grafana_url.source == "default"
    assert {k: v.value for k, v in cfg.uids.items()} == DEFAULT_UIDS
    assert all(v.source == "default" for v in cfg.uids.values())
    assert cfg.config_path is None


def test_file_overrides_default(base_env: dict[str, str], tmp_path: Path) -> None:
    """해석 순서 2단 — 설정 파일이 있으면 그 값이 기본값을 덮고 출처는 "file:<경로>" 가 된다.
    파일에 없는 키(metrics uid)는 여전히 기본값이라, 부분 설정이 허용됨을 함께 확인한다."""
    p = _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json",
               {"grafana_url": "http://grafana.svc", "uids": {"logs": "loki-other"}})
    cfg = load_config(base_env, home=tmp_path / "home")
    assert cfg.grafana_url.value == "http://grafana.svc" and cfg.grafana_url.source == "file:%s" % p
    assert cfg.uids["logs"].value == "loki-other" and cfg.uids["logs"].source.startswith("file:")
    assert cfg.uids["metrics"].value == DEFAULT_UIDS["metrics"] and cfg.uids["metrics"].source == "default"
    assert cfg.config_path == str(p)


def test_env_overrides_file(base_env: dict[str, str], tmp_path: Path) -> None:
    """해석 순서 1단 — env 가 있으면 설정 파일 값까지 덮고 출처는 "env:<이름>" 이 된다. env → 파일 → 기본값의 우선순위가 티켓 요구다."""
    _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json",
           {"grafana_url": "http://from-file", "uids": {"metrics": "from-file"}})
    env = {**base_env, "OBS_GRAFANA_URL": "http://from-env", "OBS_UID_METRICS": "from-env"}
    cfg = load_config(env, home=tmp_path / "home")
    assert cfg.grafana_url.value == "http://from-env" and cfg.grafana_url.source == "env:OBS_GRAFANA_URL"
    assert cfg.uids["metrics"].value == "from-env" and cfg.uids["metrics"].source == "env:OBS_UID_METRICS"


def test_candidate_order(base_env: dict[str, str], tmp_path: Path) -> None:
    """설정 파일 후보 경로의 순서 — OBS_CONFIG 가 있으면 그것 하나만, 없으면 ${CLAUDE_PLUGIN_DATA}/config.json → ~/.config/obs-toolkit/config.json.
    CLAUDE_PLUGIN_DATA 가 없으면 홈 경로만 남는다."""
    home = tmp_path / "home"
    cands = config_file_candidates(base_env, home)
    assert cands == [Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", home / ".config" / "obs-toolkit" / "config.json"]
    explicit = {**base_env, "OBS_CONFIG": str(tmp_path / "x.json")}
    assert config_file_candidates(explicit, home) == [tmp_path / "x.json"]
    no_data = {k: v for k, v in base_env.items() if k != "CLAUDE_PLUGIN_DATA"}
    assert config_file_candidates(no_data, home) == [home / ".config" / "obs-toolkit" / "config.json"]


def test_home_config_used_when_plugin_data_absent(base_env: dict[str, str], tmp_path: Path) -> None:
    """플러그인 데이터 디렉터리에 파일이 없을 때 홈의 설정 파일이 실제로 읽히는지 — 후보 순서가 문서가 아니라 동작으로 맞는지 확인한다."""
    home = tmp_path / "home"
    _write(home / ".config" / "obs-toolkit" / "config.json", {"uids": {"traces": "tempo-home"}})
    cfg = load_config(base_env, home=home)
    assert cfg.uids["traces"].value == "tempo-home"


def test_token_only_from_env(base_env: dict[str, str], tmp_path: Path) -> None:
    """토큰은 env 에서만 — 설정 파일에 token 키가 있어도 무시되고, 해석된 Config 를 직렬화해도 그 값이 어디에도 실리지 않는다.
    token_from_env 는 env 에 없으면 빈 문자열을 돌려준다(예외가 아니라 — 판단은 Client.from_env 가 한다)."""
    _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", {"token": "file-token-must-be-ignored"})
    cfg = load_config(base_env, home=tmp_path / "home")
    assert "file-token" not in json.dumps(to_dict(cfg))
    assert token_from_env(base_env) == "test-token-not-real"
    assert token_from_env({}) == ""


def test_client_from_env_requires_token(base_env: dict[str, str]) -> None:
    """토큰이 없으면 Client.from_env 가 거부한다 — 오류 문구에 env 이름(OBS_GRAFANA_TOKEN)이 들어 있어 사람이 무엇을 놓을지 안다."""
    env = {k: v for k, v in base_env.items() if k != "OBS_GRAFANA_TOKEN"}
    with pytest.raises(ObsError) as ei:
        Client.from_env(env)
    assert "OBS_GRAFANA_TOKEN" in str(ei.value)


def test_non_string_config_value_is_an_error(base_env: dict[str, str], tmp_path: Path) -> None:
    """설정 파일의 값은 문자열이어야 한다 — uid 에 숫자를 넣으면 조용히 str() 하지 않고 ObsError 로 거부한다. 외부 경계(JSON)를 dataclass 로 옮길 때 형태를 검증하는 자리다."""
    _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", {"uids": {"metrics": 42}})
    with pytest.raises(ObsError):
        load_config(base_env, home=tmp_path / "home")


def test_bad_config_file_is_an_error(base_env: dict[str, str], tmp_path: Path) -> None:
    """깨진 설정 파일(JSON 아님)은 무시하고 기본값으로 가는 것이 아니라 오류다 — 사람이 놓은 설정이 몰래 버려지면 안 된다."""
    p = Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ObsError):
        load_config(base_env, home=tmp_path / "home")


def test_unknown_backend(config: Config) -> None:
    """논리 이름은 metrics·logs·traces 셋뿐 — 그 밖의 이름으로 uid 를 물으면 ObsError."""
    with pytest.raises(ObsError):
        config.uid("nope")


def test_env_example_documents_every_env_the_core_reads() -> None:
    """`.env.example` 은 코어가 읽는 env 이름을 빠짐없이 담고(새 env 를 코어에 더하면 여기서 걸린다), 실제 토큰 값이 없다."""
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    names = {ENV_TOKEN, ENV_URL, ENV_PROXY_PREFIX, ENV_CONFIG, *ENV_UIDS.values(), "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_DATA",
             "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"}
    missing = [n for n in names if n not in text]
    assert not missing, ".env.example 에 없는 env: %s" % missing
    assert "OBS_GRAFANA_TOKEN=glsa_replace_me" in text and text.count("glsa_") == 1
