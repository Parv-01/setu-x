"""One test per configuration validation rule (configs/environments.yaml + configs/placement.yaml)."""
import copy

import pytest
import yaml

from setu_x import config_check
from setu_x.common.config import ConfigError
from setu_x.placement import load_config_dir, parse_environments, parse_placement
from tests.conftest import CONFIG_DIR, ROOT, raw_config


def envs_raw():
    return copy.deepcopy(raw_config()[0])


def placement_raw():
    return copy.deepcopy(raw_config()[1])


def envs_ok():
    return parse_environments(envs_raw())


def expect_env_error(data, *fragments):
    with pytest.raises(ConfigError) as exc:
        parse_environments(data, "configs/environments.yaml")
    msg = str(exc.value)
    assert msg.startswith("configs/environments.yaml: ")
    for f in fragments:
        assert f in msg, msg


def expect_placement_error(data, *fragments):
    with pytest.raises(ConfigError) as exc:
        parse_placement(data, envs_ok(), "configs/placement.yaml")
    msg = str(exc.value)
    assert msg.startswith("configs/placement.yaml: ")
    for f in fragments:
        assert f in msg, msg


# ---------------------------------------------------------------------------- shipped files
def test_shipped_configs_are_valid():
    envs, placement = load_config_dir(CONFIG_DIR)
    assert set(envs.environments) == {"local", "aws-simulated", "azure-simulated", "gcp-simulated"}
    assert [e for e, v in envs.environments.items() if not v.simulated] == ["local"]
    assert [i.name for i in placement.services.inventory.instances] == ["inventory-a", "inventory-b"]


def test_rtt_lookup_symmetric_and_zero_within_env():
    envs = envs_ok()
    assert envs.rtt_ms("local", "aws-simulated") == envs.rtt_ms("aws-simulated", "local") == 40
    assert envs.rtt_ms("gcp-simulated", "gcp-simulated") == 0


def test_compose_env_labels_match_placement():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    _, placement = load_config_dir(CONFIG_DIR)
    for _svc, sp in placement.service_items():
        for inst in sp.instances:
            env_block = compose["services"][inst.name]["environment"]
            assert env_block["SETU_ENV"] == inst.env, inst.name
            assert env_block["SETU_INSTANCE"] == inst.name


# ---------------------------------------------------------------------------- environments rules
def test_env_empty():
    expect_env_error({"environments": {}, "link_rtt_ms": {}}, "at least one environment")


def test_env_bad_name():
    d = envs_raw()
    d["environments"]["Bad_Name"] = {"provider": "aws", "simulated": True}
    expect_env_error(d, "environments: ", "Bad_Name")


def test_env_local_cannot_be_simulated():
    d = envs_raw()
    d["environments"]["local"]["simulated"] = True
    expect_env_error(d, "provider 'local' cannot be simulated")


def test_env_simulated_needs_suffix():
    d = envs_raw()
    d["environments"]["aws-like"] = d["environments"].pop("aws-simulated")
    expect_env_error(d, "'aws-like': simulated environments must be named")


def test_env_real_cloud_not_allowed():
    d = envs_raw()
    d["environments"]["aws-simulated"]["simulated"] = False
    expect_env_error(d, "non-simulated aws environments are not allowed")


def test_env_required_local():
    d = envs_raw()
    del d["environments"]["local"]
    d["link_rtt_ms"] = {k: v for k, v in d["link_rtt_ms"].items() if "local" not in k}
    expect_env_error(d, "non-simulated 'local' environment is required")


def test_env_only_one_local():
    d = envs_raw()
    d["environments"]["edge"] = {"provider": "local", "simulated": False}
    expect_env_error(d, "only one 'local' environment")


def test_env_unknown_field():
    d = envs_raw()
    d["environments"]["local"]["region"] = "x"
    expect_env_error(d, "environments.local.region")


@pytest.mark.parametrize(
    "key,value,fragment",
    [
        ("local-aws-simulated", 10, "must look like"),
        ("local--mars-simulated", 10, "unknown environment 'mars-simulated'"),
        ("local--local", 0, "same-environment links"),
        ("aws-simulated--local", 40, "duplicates 'local--aws-simulated'"),
        ("local--aws-simulated", -1, "outside 0..2000"),
        ("local--aws-simulated", 2001, "outside 0..2000"),
    ],
)
def test_env_link_rules(key, value, fragment):
    d = envs_raw()
    d["link_rtt_ms"][key] = value
    expect_env_error(d, "link_rtt_ms", fragment)


def test_env_links_must_be_complete():
    d = envs_raw()
    del d["link_rtt_ms"]["azure-simulated--gcp-simulated"]
    expect_env_error(d, "missing pairs: azure-simulated--gcp-simulated")


# ---------------------------------------------------------------------------- placement rules
def inv(d):
    return d["services"]["inventory"]


def test_placement_unknown_env_names_field_path():
    d = placement_raw()
    inv(d)["instances"][1]["env"] = "aws-sim"
    expect_placement_error(d, "services.inventory.instances.1.env: unknown environment 'aws-sim'")


def test_placement_bad_url():
    d = placement_raw()
    inv(d)["instances"][0]["url"] = "ftp://inventory-a"
    expect_placement_error(d, "services.inventory.instances.0.url")


def test_placement_duplicate_instance_in_service():
    d = placement_raw()
    inv(d)["instances"][1]["name"] = "inventory-a"
    expect_placement_error(d, "services.inventory", "unique within a service")


@pytest.mark.parametrize("roles,count", [(["secondary", "secondary"], 0), (["primary", "primary"], 2)])
def test_placement_exactly_one_primary(roles, count):
    d = placement_raw()
    for inst, role in zip(inv(d)["instances"], roles, strict=True):
        inst["role"] = role
    expect_placement_error(d, f"exactly one instance must have role 'primary' (found {count})")


def test_placement_some_instance_enabled():
    d = placement_raw()
    for inst in inv(d)["instances"]:
        inst["enabled"] = False
    expect_placement_error(d, "at least one instance must be enabled")


def test_placement_weighted_needs_positive_weight():
    d = placement_raw()
    inv(d)["routing"] = "weighted"
    for inst in inv(d)["instances"]:
        inst["weight"] = 0
    expect_placement_error(d, "positive total weight")


def test_placement_weight_range():
    d = placement_raw()
    inv(d)["instances"][0]["weight"] = 101
    expect_placement_error(d, "services.inventory.instances.0.weight")


def test_placement_instance_name_unique_across_services():
    d = placement_raw()
    d["services"]["pricing"]["instances"][0]["name"] = "inventory-b"
    expect_placement_error(d, "'inventory-b' is used by both inventory and pricing")


@pytest.mark.parametrize("path,value,fragment", [
    (("services", "inventory", "routing"), "random", "services.inventory.routing"),
    (("call_mode",), "batch", "call_mode"),
])
def test_placement_enum_values(path, value, fragment):
    d = placement_raw()
    target = d
    for p in path[:-1]:
        target = target[p]
    target[path[-1]] = value
    expect_placement_error(d, fragment)


def test_placement_missing_service():
    d = placement_raw()
    del d["services"]["pricing"]
    expect_placement_error(d, "services.pricing")


def test_placement_unknown_service():
    d = placement_raw()
    d["services"]["shipping"] = copy.deepcopy(d["services"]["pricing"])
    expect_placement_error(d, "services.shipping")


def test_placement_unknown_instance_field():
    d = placement_raw()
    inv(d)["instances"][0]["region"] = "us"
    expect_placement_error(d, "services.inventory.instances.0.region")


@pytest.mark.parametrize("change,fragment", [
    ({"timeout_ms": 5000, "outer_deadline_ms": 1000}, "timeout_ms must not exceed outer_deadline_ms"),
    ({"connect_timeout_ms": 900, "timeout_ms": 800}, "connect_timeout_ms must not exceed timeout_ms"),
    ({"retries": 4}, "resilience.retries"),
    ({"backoff": {"base_ms": 500, "max_ms": 100}}, "backoff.max_ms must be >= backoff.base_ms"),
    ({"circuit_breaker": {"enabled": True, "consecutive_failures": 0}}, "consecutive_failures"),
    ({"circuit_breaker": {"reset_timeout_ms": 50}}, "reset_timeout_ms"),
    ({"outer_deadline_ms": 20000}, "outer_deadline_ms"),
])
def test_placement_resilience_rules(change, fragment):
    d = placement_raw()
    d["resilience"].update(change)
    expect_placement_error(d, fragment)


# ---------------------------------------------------------------------------- files and CLI
def write_dir(tmp_path, envs, placement_text):
    (tmp_path / "environments.yaml").write_text(yaml.safe_dump(envs), encoding="utf-8")
    (tmp_path / "placement.yaml").write_text(placement_text, encoding="utf-8")
    return tmp_path


def test_invalid_yaml_and_missing_file_fail_clearly(tmp_path):
    write_dir(tmp_path, envs_raw(), "services: [unclosed")
    with pytest.raises(ConfigError, match=r"placement.yaml: invalid YAML"):
        load_config_dir(tmp_path)
    (tmp_path / "placement.yaml").unlink()
    with pytest.raises(ConfigError, match=r"placement.yaml: cannot read file"):
        load_config_dir(tmp_path)


def test_config_check_cli(tmp_path, capsys):
    assert config_check.main(["x", str(CONFIG_DIR)]) == 0
    assert "VALID" in capsys.readouterr().out
    bad = placement_raw()
    inv(bad)["instances"][0]["env"] = "nowhere"
    write_dir(tmp_path, envs_raw(), yaml.safe_dump(bad))
    assert config_check.main(["x", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "INVALID" in err and "services.inventory.instances.0.env: unknown environment 'nowhere'" in err
