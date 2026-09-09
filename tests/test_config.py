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
    cfg = load_config(base_env, home=tmp_path / "home")
    assert cfg.grafana_url.value == DEFAULT_GRAFANA_URL and cfg.grafana_url.source == "default"
    assert {k: v.value for k, v in cfg.uids.items()} == DEFAULT_UIDS
    assert all(v.source == "default" for v in cfg.uids.values())
    assert cfg.config_path is None


def test_file_overrides_default(base_env: dict[str, str], tmp_path: Path) -> None:
    p = _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json",
               {"grafana_url": "http://grafana.svc", "uids": {"logs": "loki-other"}})
    cfg = load_config(base_env, home=tmp_path / "home")
    assert cfg.grafana_url.value == "http://grafana.svc" and cfg.grafana_url.source == "file:%s" % p
    assert cfg.uids["logs"].value == "loki-other" and cfg.uids["logs"].source.startswith("file:")
    assert cfg.uids["metrics"].value == DEFAULT_UIDS["metrics"] and cfg.uids["metrics"].source == "default"
    assert cfg.config_path == str(p)


def test_env_overrides_file(base_env: dict[str, str], tmp_path: Path) -> None:
    _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json",
           {"grafana_url": "http://from-file", "uids": {"metrics": "from-file"}})
    env = {**base_env, "OBS_GRAFANA_URL": "http://from-env", "OBS_UID_METRICS": "from-env"}
    cfg = load_config(env, home=tmp_path / "home")
    assert cfg.grafana_url.value == "http://from-env" and cfg.grafana_url.source == "env:OBS_GRAFANA_URL"
    assert cfg.uids["metrics"].value == "from-env" and cfg.uids["metrics"].source == "env:OBS_UID_METRICS"


def test_candidate_order(base_env: dict[str, str], tmp_path: Path) -> None:
    home = tmp_path / "home"
    cands = config_file_candidates(base_env, home)
    assert cands == [Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", home / ".config" / "obs-toolkit" / "config.json"]
    explicit = {**base_env, "OBS_CONFIG": str(tmp_path / "x.json")}
    assert config_file_candidates(explicit, home) == [tmp_path / "x.json"]
    no_data = {k: v for k, v in base_env.items() if k != "CLAUDE_PLUGIN_DATA"}
    assert config_file_candidates(no_data, home) == [home / ".config" / "obs-toolkit" / "config.json"]


def test_home_config_used_when_plugin_data_absent(base_env: dict[str, str], tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write(home / ".config" / "obs-toolkit" / "config.json", {"uids": {"traces": "tempo-home"}})
    cfg = load_config(base_env, home=home)
    assert cfg.uids["traces"].value == "tempo-home"


def test_token_only_from_env(base_env: dict[str, str], tmp_path: Path) -> None:
    _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", {"token": "file-token-must-be-ignored"})
    cfg = load_config(base_env, home=tmp_path / "home")
    assert "file-token" not in json.dumps(to_dict(cfg))
    assert token_from_env(base_env) == "test-token-not-real"
    assert token_from_env({}) == ""


def test_client_from_env_requires_token(base_env: dict[str, str]) -> None:
    env = {k: v for k, v in base_env.items() if k != "OBS_GRAFANA_TOKEN"}
    with pytest.raises(ObsError) as ei:
        Client.from_env(env)
    assert "OBS_GRAFANA_TOKEN" in str(ei.value)


def test_non_string_config_value_is_an_error(base_env: dict[str, str], tmp_path: Path) -> None:
    _write(Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json", {"uids": {"metrics": 42}})
    with pytest.raises(ObsError):
        load_config(base_env, home=tmp_path / "home")


def test_bad_config_file_is_an_error(base_env: dict[str, str], tmp_path: Path) -> None:
    p = Path(base_env["CLAUDE_PLUGIN_DATA"]) / "config.json"
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ObsError):
        load_config(base_env, home=tmp_path / "home")


def test_unknown_backend(config: Config) -> None:
    with pytest.raises(ObsError):
        config.uid("nope")
