from pathlib import Path
from unittest.mock import Mock, patch

import pytest


class FakeCuda:
    def __init__(self, available):
        self.available = available

    def is_available(self):
        return self.available


def test_layout_device_setting_accepts_environment_override(monkeypatch):
    from openmd.ocr.config import settings

    monkeypatch.setenv("OPENMD_OCR_LAYOUT_DEVICE", "cuda")
    monkeypatch.setattr("openmd.ocr.config.load_local_config", lambda: {})
    assert settings()["layout_device"] == "cuda"


@pytest.mark.parametrize("mode", ["auto", "cpu", "cuda"])
def test_layout_worker_resolves_requested_device(mode):
    from openmd.ocr.layout_worker import select_device

    torch = Mock(cuda=FakeCuda(True))
    assert select_device(mode, torch) == ("cpu" if mode == "cpu" else "cuda:0")


def test_layout_auto_falls_back_to_cpu_and_forced_cuda_fails_closed():
    from openmd.ocr.layout_worker import select_device

    torch = Mock(cuda=FakeCuda(False))
    assert select_device("auto", torch) == "cpu"
    with pytest.raises(RuntimeError, match="layout_cuda_unavailable"):
        select_device("cuda", torch)


def test_layout_controller_keeps_cuda_visible_unless_cpu_is_forced(tmp_path):
    from openmd.ocr.layout import _run

    for device, expected in (("auto", "0"), ("cuda", "0"), ("cpu", "-1")):
        with patch("openmd.ocr.layout.layout_runtime", return_value=(Path("python.exe"), tmp_path)), \
             patch("openmd.ocr.layout.owned_popen") as popen, \
             patch("openmd.ocr.layout.wait_interruptibly"):
            popen.return_value.returncode = 0
            _run(["--probe"], 10, tmp_path, device=device)
        env = popen.call_args.kwargs["env"]
        if device == "cpu":
            assert env["CUDA_VISIBLE_DEVICES"] == expected
        else:
            assert env.get("CUDA_VISIBLE_DEVICES") != "-1"


def test_layout_cuda_probe_reports_build_device_and_memory():
    from openmd.ocr.layout_worker import probe_payload

    cuda = Mock(is_available=lambda: True, mem_get_info=lambda _index: (6 * 1024**3, 8 * 1024**3),
                get_device_name=lambda _index: "GPU")
    torch = Mock(cuda=cuda, version=Mock(cuda="12.4"))
    report = probe_payload(torch, {"torch": True, "transformers": True, "PIL": True,
                                   "numpy": True, "torchvision": True}, {})
    assert report["device"] == "cuda:0"
    assert report["torch_cuda_build"] == "12.4"
    assert report["gpu_memory_total_bytes"] == 8 * 1024**3
    assert report["gpu_memory_free_bytes"] == 6 * 1024**3


def test_layout_runtime_spec_selects_cuda_only_when_requested_or_driver_exists(monkeypatch):
    from openmd.runtime import recommendations

    monkeypatch.setattr(recommendations, "_nvidia_driver_available", lambda: True)
    automatic = recommendations._layout_runtime_spec("auto")
    forced_cpu = recommendations._layout_runtime_spec("cpu")
    forced_cuda = recommendations._layout_runtime_spec("cuda")
    assert automatic["selected_device"] == "cuda"
    assert "torch==2.5.1+cu124" in automatic["required"]
    assert forced_cpu["selected_device"] == "cpu"
    assert "torch==2.5.1+cpu" in forced_cpu["required"]
    assert forced_cuda["selected_device"] == "cuda"


def test_cuda_runtime_installer_keeps_gpu_visible_and_uses_cu124(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from openmd.runtime import recommendations

    target = tmp_path / "cuda-runtime"
    monkeypatch.setenv("OPENMD_OCR_LAYOUT_RUNTIME", str(target))
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.setattr(recommendations.venv.EnvBuilder, "create", lambda _self, path: target.mkdir(parents=True))
    calls = []
    monkeypatch.setattr(recommendations, "run_interruptibly", lambda command, **kwargs: (
        calls.append((command, kwargs)) or SimpleNamespace(stdout='{"modules":{"torch":true,"transformers":true,"PIL":true,"numpy":true,"torchvision":true},"cuda_visible":true,"torch_cuda_build":"12.4"}\n')
    ))
    monkeypatch.setattr(recommendations, "_nvidia_driver_available", lambda: True)

    receipt = recommendations.install_layout_runtime("auto")

    assert "torch==2.5.1+cu124" in calls[0][0]
    assert "torchvision==0.20.1+cu124" in calls[0][0]
    assert "https://download.pytorch.org/whl/cu124" in calls[0][0]
    assert "CUDA_VISIBLE_DEVICES" not in calls[-1][1]["env"]
    assert receipt["device"] == "cuda"
