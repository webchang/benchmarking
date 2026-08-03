import json

from benchmarking_service.instances import InstanceRegistry


def test_load_and_exact_match(tmp_path, instance_dict):
    (tmp_path / "keycloak.localtest.me_8080.json").write_text(json.dumps(instance_dict))
    reg = InstanceRegistry.load(tmp_path)
    assert len(reg) == 1
    assert reg.allowlist == frozenset({instance_dict["iss"]})
    assert reg.get(instance_dict["iss"]).rossoctl_base_url == instance_dict["rossoctl_base_url"]


def test_out_of_scope_iss_returns_none(tmp_path, instance_dict):
    (tmp_path / "a.json").write_text(json.dumps(instance_dict))
    reg = InstanceRegistry.load(tmp_path)
    assert reg.get("http://evil.example.com/realms/x") is None


def test_missing_dir_is_empty(tmp_path):
    reg = InstanceRegistry.load(tmp_path / "nope")
    assert len(reg) == 0


def test_malformed_file_skipped(tmp_path, instance_dict):
    (tmp_path / "good.json").write_text(json.dumps(instance_dict))
    (tmp_path / "bad.json").write_text("{not json")
    reg = InstanceRegistry.load(tmp_path)
    assert len(reg) == 1
