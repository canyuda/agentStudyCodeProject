"""Configuration system: env expansion, fallback, fail-fast validation (task 1.2, scenario 15)."""

from __future__ import annotations

import pytest

from gateway.config import ConfigError, GatewayConfig, load_config, load_config_or_exit


def write_cfg(tmp_path, text: str) -> str:
    path = tmp_path / "cfg.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


BASE = """
auth:
  gateway_keys: ["${GW_TEST_KEY:-default-key}"]
providers:
  p1:
    protocol: openai_responses
    base_url: https://x.test
    api_key: "k"
aliases:
  a1:
    strategy: priority
    routes: [{provider: p1, model: m1}]
"""


def test_env_var_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("GW_TEST_KEY", "expanded-value")
    cfg = load_config(write_cfg(tmp_path, BASE))
    assert cfg.auth.gateway_keys == ["expanded-value"]


def test_env_var_default_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("GW_TEST_KEY", raising=False)
    cfg = load_config(write_cfg(tmp_path, BASE))
    assert cfg.auth.gateway_keys == ["default-key"]


def test_env_var_missing_without_default_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("GW_TEST_KEY", raising=False)
    with pytest.raises(ConfigError, match="GW_TEST_KEY"):
        load_config(write_cfg(tmp_path, 'auth: {gateway_keys: ["${GW_TEST_KEY}"]}'))


def test_missing_required_section_fails(tmp_path):
    with pytest.raises(ConfigError, match="invalid config"):
        load_config(write_cfg(tmp_path, "service: {name: x}"))


def test_yaml_syntax_error_fails(tmp_path):
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(write_cfg(tmp_path, "auth: [unclosed"))


def test_unknown_provider_reference_fails_startup(tmp_path):
    """Scenario 15: route referencing a non-existent provider -> startup failure."""
    bad = BASE.replace("provider: p1", "provider: ghost")
    with pytest.raises(ConfigError, match="ghost"):
        load_config(write_cfg(tmp_path, bad))


def test_unknown_protocol_type_fails_startup(tmp_path):
    """Scenario 15: provider declaring an unknown protocol -> startup failure."""
    bad = BASE.replace("protocol: openai_responses", "protocol: graphql")
    with pytest.raises(ConfigError, match="unknown protocol"):
        load_config(write_cfg(tmp_path, bad))


def test_built_in_example_config_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_CONFIG", raising=False)
    cfg = load_config()  # no path -> built-in example, runs out of the box
    assert isinstance(cfg, GatewayConfig)
    assert cfg.providers["deepseek_responses"].protocol == "openai_responses"


def test_gateway_config_env_var_selects_file(tmp_path, monkeypatch):
    path = write_cfg(tmp_path, BASE)
    monkeypatch.setenv("GATEWAY_CONFIG", path)
    cfg = load_config()
    assert cfg.aliases["a1"].routes[0].model == "m1"


def test_load_config_or_exit_exits_nonzero(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        load_config_or_exit(write_cfg(tmp_path, "auth: broken"))
    assert excinfo.value.code == 2
