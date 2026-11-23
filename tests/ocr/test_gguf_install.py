from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_ocr_model_plan_pins_gguf_source_files_and_modelfiles(tmp_path):
    from openmd.runtime.recommendations import install_plan

    plan = install_plan("ocr:chain", root=tmp_path, device="cpu")
    models = [item for item in plan["downloads"] if item["kind"] == "gguf_ollama"]
    assert {item["model"] for item in models} == {"ovis3-bf16", "minicpm-ocr-f16"}
    assert all(item["revision"] and item["license"] == "Apache-2.0" for item in models)
    assert all(item["destination"].startswith(str(tmp_path)) for item in models)
    assert all(len(digest) == 64 for item in models for digest in item["files"].values())
    assert all(item["modelfile"].startswith("ocr_models/") for item in models)
    assert plan["estimated_size_bytes"] == sum(item["size_bytes"] for item in plan["downloads"])


def test_gguf_file_verification_streams_and_rejects_wrong_hash(tmp_path):
    from openmd.runtime.recommendations import _gguf_files_verified

    payload = b"verified GGUF test data"
    item = {"files": {"model.gguf": sha256(payload).hexdigest()}}
    (tmp_path / "model.gguf").write_bytes(payload)
    assert _gguf_files_verified(tmp_path, item) == item["files"]
    item["files"]["model.gguf"] = "0" * 64
    with pytest.raises(RuntimeError, match="model_integrity_check_failed"):
        _gguf_files_verified(tmp_path, item)


def test_ollama_create_uses_bundled_modelfile_and_reads_back_expected_digests(tmp_path):
    from openmd.runtime.recommendations import _create_gguf_model

    hashes = {"model.gguf": "a" * 64, "projector.gguf": "b" * 64}
    item = {"model": "test-ocr-f16", "modelfile": "ocr_models/minicpm/Modelfile"}
    show_miss = SimpleNamespace(returncode=1, stdout="", stderr="")
    show_match = SimpleNamespace(returncode=0,
                                 stdout=(f"FROM /models/blobs/sha256-{hashes['model.gguf']}\n"
                                         f"FROM /models/blobs/sha256-{hashes['projector.gguf']}\n"
                                         "PARAMETER num_ctx 8192\nPARAMETER temperature 0\n"), stderr="")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if command[1] == "create":
            return SimpleNamespace(returncode=0, stdout="created", stderr="")
        return show_miss if sum(row[0][1] == "show" for row in calls) < 3 else show_match

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr("openmd.runtime.recommendations.run_interruptibly", run)
        verified, seconds = _create_gguf_model("ollama.exe", item, tmp_path, hashes)

    assert verified == "ollama create and model/projector SHA-256 readback"
    assert seconds >= 0
    create_call = next(row for row in calls if row[0][1] == "create")
    staged = Path(create_call[0][-1])
    assert staged.is_file()
    assert "PARAMETER num_ctx 8192" in staged.read_text(encoding="utf-8")
    assert create_call[1]["cwd"] == tmp_path
    assert create_call[1]["timeout"] == 4 * 60 * 60
    assert "capture_output" not in create_call[1]
    assert "creationflags" in create_call[1] or create_call[1].get("start_new_session") is True
    show_call = next(row for row in calls if row[0][1] == "show")
    assert show_call[1]["timeout"] == 60
    assert show_call[1]["capture_output"] is True


def test_existing_ollama_tag_with_matching_digest_skips_create(tmp_path, monkeypatch):
    from openmd.runtime import recommendations

    hashes = {"model.gguf": "a" * 64}
    item = {"model": "test-ocr", "modelfile": "ocr_models/ovis3/Modelfile"}
    run = Mock(return_value=SimpleNamespace(
        returncode=0,
        stdout=f"FROM /models/blobs/sha256-{hashes['model.gguf']}\nPARAMETER num_ctx 8192\nPARAMETER temperature 0\n",
        stderr="",
    ))
    monkeypatch.setattr(recommendations, "run_interruptibly", run)

    verified, seconds = recommendations._create_gguf_model("ollama.exe", item, tmp_path, hashes)

    assert verified == "existing_model_digest_matches"
    assert seconds == 0
    assert run.call_count == 1
    assert run.call_args.kwargs["timeout"] == 60
    assert run.call_args.kwargs["capture_output"] is True
    assert "creationflags" in run.call_args.kwargs or run.call_args.kwargs.get("start_new_session") is True
