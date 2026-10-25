import pytest

from setu_x.common.config import ConfigError, load_gateway_settings, load_service_settings


def test_defaults_load():
    s = load_gateway_settings({})
    assert s.call_mode == "parallel" and s.timeout_ms == 800 and s.outer_deadline_ms == 3000
    assert s.mode == "local"


def test_env_overrides_are_parsed():
    s = load_gateway_settings(
        {"SETU_CALL_MODE": "sequential", "SETU_TIMEOUT_MS": "250", "SETU_ENV": "local", "SETU_MODE": "simulated"}
    )
    assert s.call_mode == "sequential" and s.timeout_ms == 250 and s.mode == "simulated"


@pytest.mark.parametrize(
    "env,var",
    [
        ({"SETU_TIMEOUT_MS": "-5"}, "SETU_TIMEOUT_MS"),
        ({"SETU_TIMEOUT_MS": "abc"}, "SETU_TIMEOUT_MS"),
        ({"SETU_CALL_MODE": "random"}, "SETU_CALL_MODE"),
        ({"SETU_MODE": "production"}, "SETU_MODE"),
        ({"SETU_INVENTORY_URL": "not a url"}, "SETU_INVENTORY_URL"),
        ({"SETU_OUTER_DEADLINE_MS": "20000"}, "SETU_OUTER_DEADLINE_MS"),
        ({"SETU_PORT": "70000"}, "SETU_PORT"),
    ],
)
def test_invalid_values_fail_naming_the_variable(env, var):
    with pytest.raises(ConfigError) as exc:
        load_gateway_settings(env)
    assert var in str(exc.value)


def test_timeout_larger_than_deadline_is_rejected():
    with pytest.raises(ConfigError, match="SETU_TIMEOUT_MS must not exceed SETU_OUTER_DEADLINE_MS"):
        load_gateway_settings({"SETU_TIMEOUT_MS": "2000", "SETU_OUTER_DEADLINE_MS": "1000"})


def test_service_settings_instance_and_env():
    s = load_service_settings("inventory", {"SETU_INSTANCE": "inventory-b", "SETU_ENV": "gcp-simulated"})
    assert (s.service_name, s.instance, s.env) == ("inventory", "inventory-b", "gcp-simulated")
    with pytest.raises(ConfigError, match="SETU_INSTANCE"):
        load_service_settings("inventory", {"SETU_INSTANCE": "Bad Name"})
