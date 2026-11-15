from pathlib import Path
from unittest.mock import Mock

from openmd.adapters.local_audio.backends.faster_whisper import FasterWhisperASRBackend
from openmd.backends.registry import BackendRuntimeOptions
from openmd.backends.xiaohongshu_providers import XiaohongshuAssetBundle, XiaohongshuCompositeProvider


def test_general_asr_preserves_language_detection_and_no_domain_bias(monkeypatch):
    for key in ("PROFILE", "LANGUAGE", "INITIAL_PROMPT", "HOTWORDS"):
        monkeypatch.delenv("OPENMD_FASTER_WHISPER_" + key, raising=False)
    backend = FasterWhisperASRBackend()
    assert backend.language is None
    assert not backend.initial_prompt
    assert not backend.hotwords
    monkeypatch.setenv("OPENMD_FASTER_WHISPER_LANGUAGE", "zh")
    assert FasterWhisperASRBackend(language="").language is None


def test_podcast_profile_is_opt_in_and_config_tracks_effective_parameters(monkeypatch):
    monkeypatch.setenv("OPENMD_FASTER_WHISPER_PROFILE", "zh_podcast_v2")
    monkeypatch.delenv("OPENMD_FASTER_WHISPER_LANGUAGE", raising=False)
    backend = FasterWhisperASRBackend(model_size="small")
    assert backend.language == "zh"
    assert "Leiya" in backend.hotwords
    baseline = backend.execution_config()
    assert baseline != FasterWhisperASRBackend(model_size="large-v3").execution_config()
    assert baseline != FasterWhisperASRBackend(model_size="small", language="en").execution_config()
    assert baseline != FasterWhisperASRBackend(model_size="small", hotwords="custom").execution_config()


def _bundle(native=""):
    return XiaohongshuAssetBundle(
        note_id="fixture", title="fixture", author="", native_text="", tags=[],
        media_kind="video", native_transcript=native, image_count=0, video_count=1,
        image_paths=[], video_paths=[Path("fixture.mp4")], image_errors=[], video_errors=[],
    )


def test_xhs_cloud_gate_precedes_audio_preparation_and_preserves_native():
    client = Mock(name="fake client")
    client.name = "fake-cloud"
    client.is_available.return_value = True
    client.transcribe.return_value = "A meaningful fake transcript."
    provider = XiaohongshuCompositeProvider(BackendRuntimeOptions(
        enable_media_download_backends=True, enable_asr_backends=True,
    ), asr_client=client)
    provider._extract_audio = Mock()
    _, native = provider._video_component(_bundle("A native transcript remains usable."), download=True)
    assert native.status == "verified"
    _, blocked = provider._video_component(_bundle(), download=True)
    assert blocked.error_code == "xiaohongshu_cloud_backends_not_enabled"
    provider._extract_audio.assert_not_called()
    client.is_available.assert_not_called()
    client.transcribe.assert_not_called()


def test_xhs_explicit_cloud_gate_allows_fake_client():
    client = Mock()
    client.name = "fake-cloud"
    client.is_available.return_value = True
    client.transcribe.return_value = "A meaningful fake transcript."
    provider = XiaohongshuCompositeProvider(BackendRuntimeOptions(
        enable_media_download_backends=True, enable_asr_backends=True, enable_cloud_backends=True,
    ), asr_client=client)
    provider._extract_audio = Mock()
    _, result = provider._video_component(_bundle(), download=True)
    assert result.status == "verified"
    assert client.transcribe.call_count == 1
