import json

import pytest

from openmd.core.local_config import load_local_config, qwen3_batch_size


def test_explicit_config_rejects_credentials(tmp_path, monkeypatch):
    path = tmp_path / "positions.json"
    path.write_text(json.dumps({"schema": "openmd.local/v1", "tools": {"demo": {"token": "test-secret"}}}), encoding="utf-8")
    monkeypatch.setenv("OPENMD_CONFIG", str(path))
    with pytest.raises(ValueError, match="private_field"):
        load_local_config()


def test_explicit_missing_config_fails_instead_of_using_machine_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENMD_CONFIG", str(tmp_path / "absent.json"))
    with pytest.raises(ValueError, match="missing"):
        load_local_config()


def test_doctor_reports_bad_config_without_traceback(tmp_path, monkeypatch, capsys):
    from openmd.cli import main

    monkeypatch.setenv("OPENMD_CONFIG", str(tmp_path / "absent.json"))
    assert main(["doctor", "--profile", "core"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["profile_diagnostics"]["error_code"] == "local_configuration_invalid"
    assert "Traceback" not in output.err


@pytest.mark.parametrize("value", [0, -1, 33, True, 1.5, "2", None])
def test_qwen3_local_batch_rejects_invalid_values(tmp_path, monkeypatch, value):
    path = tmp_path / "positions.json"
    path.write_text(json.dumps({"schema": "openmd.local/v1", "qwen3_batch_size": value}), encoding="utf-8")
    monkeypatch.setenv("OPENMD_CONFIG", str(path))
    with pytest.raises(ValueError, match="qwen3_batch_size_invalid"):
        load_local_config()


@pytest.mark.parametrize("value", ["0", "-1", "33", "true", "2.0", "", "1e1"])
def test_qwen3_environment_batch_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setattr("openmd.core.local_config.load_local_config", lambda: {})
    monkeypatch.setenv("OPENMD_QWEN3_BATCH_SIZE", value)
    with pytest.raises(ValueError, match="qwen3_batch_size_invalid"):
        qwen3_batch_size()


def test_qwen3_batch_precedence_and_default(tmp_path, monkeypatch):
    path = tmp_path / "positions.json"
    path.write_text(json.dumps({"schema": "openmd.local/v1"}), encoding="utf-8")
    monkeypatch.setenv("OPENMD_CONFIG", str(path))
    monkeypatch.delenv("OPENMD_QWEN3_BATCH_SIZE", raising=False)
    assert qwen3_batch_size() == 4
    path.write_text(json.dumps({"schema": "openmd.local/v1", "qwen3_batch_size": 1}), encoding="utf-8")
    assert qwen3_batch_size() == 1
    monkeypatch.setenv("OPENMD_QWEN3_BATCH_SIZE", "2")
    assert qwen3_batch_size() == 2


@pytest.mark.parametrize("backend", ["asr.qwen3", "asr.auto"])
def test_local_batch_changes_execution_fingerprint(tmp_path, monkeypatch, backend):
    from openmd.runtime.asr import backend_execution_config

    path = tmp_path / "positions.json"
    monkeypatch.setenv("OPENMD_CONFIG", str(path))
    monkeypatch.delenv("OPENMD_QWEN3_BATCH_SIZE", raising=False)
    fingerprints = []
    for size in [1, 4]:
        path.write_text(json.dumps({"schema": "openmd.local/v1", "qwen3_batch_size": size}), encoding="utf-8")
        config = backend_execution_config(backend)
        actual = config if backend == "asr.qwen3" else config["qwen3"]
        assert actual["batch_size"] == size
        fingerprints.append(config["runtime_config_id"])
    assert fingerprints[0] != fingerprints[1]
