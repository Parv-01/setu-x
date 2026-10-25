import pytest

from setu_x.common.config import (
    ConfigError,
    load_controller_settings,
    load_gateway_settings,
    load_service_settings,
)


def test_defaults_load():
    s = load_gateway_settings({})
    assert s.controller_url == "http://controller:8000" and s.poll_interval_ms == 2000 and s.mode == "local"
    c = load_controller_settings({})
    assert c.gateway_url == "http://gateway:8000" and c.config_dir == "configs"


def test_env_overrides_are_parsed():
    s = load_gateway_settings({"SETU_POLL_INTERVAL_MS": "500", "SETU_MODE": "simulated", "SETU_CONTROLLER_URL": ""})
    assert s.poll_interval_ms == 500 and s.mode == "simulated"
    assert s.controller_url == "http://controller:8000"  # empty env value means "not set"


@pytest.mark.parametrize(
    "env,var",
    [
        ({"SETU_POLL_INTERVAL_MS": "50"}, "SETU_POLL_INTERVAL_MS"),
        ({"SETU_POLL_INTERVAL_MS": "abc"}, "SETU_POLL_INTERVAL_MS"),
        ({"SETU_MODE": "production"}, "SETU_MODE"),
        ({"SETU_CONTROLLER_URL": "not a url"}, "SETU_CONTROLLER_URL"),
        ({"SETU_PORT": "70000"}, "SETU_PORT"),
        ({"SETU_LOG_LEVEL": "LOUD"}, "SETU_LOG_LEVEL"),
    ],
)
def test_invalid_values_fail_naming_the_variable(env, var):
    with pytest.raises(ConfigError) as exc:
        load_gateway_settings(env)
    assert var in str(exc.value)


def test_service_settings_instance_and_env():
    s = load_service_settings("inventory", {"SETU_INSTANCE": "inventory-b", "SETU_ENV": "gcp-simulated"})
    assert (s.service_name, s.instance, s.env) == ("inventory", "inventory-b", "gcp-simulated")
    with pytest.raises(ConfigError, match="SETU_INSTANCE"):
        load_service_settings("inventory", {"SETU_INSTANCE": "Bad Name"})
