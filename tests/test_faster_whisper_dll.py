from pathlib import Path
from types import SimpleNamespace
import sys

from openmd.adapters.local_audio.backends import faster_whisper as backend


def test_windows_nvidia_directories_are_registered_and_retained(tmp_path, monkeypatch):
    site = tmp_path / "site-packages"
    origin = site / "faster_whisper" / "__init__.py"
    directories = [site / "nvidia" / name / "bin" for name in ("cublas", "cudnn", "cuda_runtime")]
    for directory in directories:
        directory.mkdir(parents=True)
    # A package with no bin directory is not a DLL search path.
    (site / "nvidia" / "without_bin").mkdir()
    monkeypatch.setattr(backend.sys, "platform", "win32")
    monkeypatch.setattr(backend.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=str(origin)))
    handles = {}
    monkeypatch.setattr(backend, "_DLL_DIRECTORY_HANDLES", handles)
    calls = []

    def add_directory(path):
        handle = object()
        calls.append((path, handle))
        return handle

    monkeypatch.setattr(backend.os, "add_dll_directory", add_directory, raising=False)
    monkeypatch.setenv("PATH", str(directories[0]))
    backend._prepare_windows_dll_paths()
    backend._prepare_windows_dll_paths()

    assert len(calls) == 3
    assert list(handles.values()) == [handle for _, handle in calls]
    assert set(backend.os.environ["PATH"].split(backend.os.pathsep)) == {str(path) for path in directories}
    assert len(backend.os.environ["PATH"].split(backend.os.pathsep)) == 3


def test_dll_setup_does_nothing_on_other_platforms(monkeypatch):
    monkeypatch.setattr(backend.sys, "platform", "linux")

    def unexpected_find_spec(name):
        raise AssertionError("Non-Windows setup must not inspect optional packages")

    monkeypatch.setattr(backend.importlib.util, "find_spec", unexpected_find_spec)
    backend._prepare_windows_dll_paths()


def test_windows_setup_tolerates_missing_nvidia_packages(tmp_path, monkeypatch):
    monkeypatch.setattr(backend.sys, "platform", "win32")
    origin = tmp_path / "faster_whisper" / "__init__.py"
    monkeypatch.setattr(backend.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=str(origin)))
    monkeypatch.setenv("PATH", "unchanged")
    backend._prepare_windows_dll_paths()
    assert backend.os.environ["PATH"] == "unchanged"


def test_windows_setup_tolerates_missing_faster_whisper(monkeypatch):
    monkeypatch.setattr(backend.sys, "platform", "win32")
    monkeypatch.setattr(backend.importlib.util, "find_spec", lambda name: None)
    monkeypatch.setenv("PATH", "unchanged")
    backend._prepare_windows_dll_paths()
    assert backend.os.environ["PATH"] == "unchanged"


def test_dll_setup_precedes_model_loading(tmp_path, monkeypatch):
    audio = tmp_path / "audio.mp3"
    audio.write_bytes(b"test fixture")
    events = []
    monkeypatch.setattr(backend, "_prepare_windows_dll_paths", lambda: events.append("dll"))

    class Model:
        def __init__(self, *args, **kwargs):
            assert events == ["dll"]
            events.append("model")

        def transcribe(self, *args, **kwargs):
            return iter([SimpleNamespace(text="recognized", start=0, end=1)]), SimpleNamespace(language="en", duration=1)

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(backend, "require_model", lambda *args, **kwargs: tmp_path)
    instance = backend.FasterWhisperASRBackend(device="cuda")
    monkeypatch.setattr(instance, "is_available", lambda: True)
    result = instance.transcribe(Path(audio))
    assert result.status == "verified"
    assert events == ["dll", "model"]
