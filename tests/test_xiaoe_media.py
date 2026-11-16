"""The platform adapter discovers media; core owns download, ASR and output."""
import json
from pathlib import Path
from types import SimpleNamespace

from openmd.backends.base import BackendExtractRequest
from openmd.backends.xiaoe import OpenCliXiaoeTranscriptProvider
from openmd.core.models import ContentItem, TranscriptResult


def request():
    return BackendExtractRequest(item=ContentItem(platform="xiaoe", input_type="single", source_type="video", url="https://demo.xet.citv.cn/p/course/video/v_ab12"), backend_chain=("opencli.xiaoe.transcript",))


def test_no_cloud_or_media_before_explicit_gates():
    calls = []
    result = OpenCliXiaoeTranscriptProvider(runner=lambda *a, **k: calls.append(a)).extract(request())
    assert result.status == "failed"
    assert result.error_code == "media_asr_not_enabled"
    assert calls == []


def test_core_download_asr_cleanup_and_safe_metadata():
    paths, calls = [], []
    def runner(args, **kwargs):
        calls.append(args)
        return 0, json.dumps([{"resource_id": "v_ab12", "title": "Lesson", "m3u8_url": "https://media.example/a.m3u8?sign=private", "duration_sec": 5, "method": "api_direct"}]), ""
    def download(url, path, **kwargs):
        assert "sign=private" in url
        path.write_bytes(b"fixture audio")
        paths.append(path)
    asr = SimpleNamespace(transcribe=lambda p: TranscriptResult(text="[00:00:00] 原始逐字稿", status="verified", source_level="media_asr", backend="asr.auto"))
    result = OpenCliXiaoeTranscriptProvider(runner=runner, allow_local_asr=True, allow_media_download=True, local_asr=asr, downloader=download).extract(request())
    assert result.text == "[00:00:00] 原始逐字稿"
    assert result.status == "verified"
    assert result.title == "Lesson"
    assert calls[0][:3] == ["opencli", "xiaoe", "play-url"]
    assert all(not p.exists() for p in paths)
    assert "private" not in repr(result)
    assert set(result.metadata["stage_seconds"]) >= {"media_discovery", "download_audio", "asr"}


def test_wrong_resource_never_downloaded():
    result = OpenCliXiaoeTranscriptProvider(runner=lambda *a, **k: (0, json.dumps([{"resource_id": "v_dead", "m3u8_url": "https://media.example/a.m3u8"}]), ""), allow_local_asr=True, allow_media_download=True, downloader=lambda *a, **k: (_ for _ in ()).throw(AssertionError("wrong resource"))).extract(request())
    assert result.error_code == "media_identity_mismatch"


def test_adapter_install_is_packaged_and_does_not_overwrite_user_edits(tmp_path):
    from openmd.runtime.opencli_adapters import install_xiaoe_adapters
    root = install_xiaoe_adapters(tmp_path)
    assert {p.name for p in root.glob("*.js")} == {"play-url.js", "catalog.js", "content.js", "dispatch.js"}
    source = (root / "play-url.js").read_text(encoding="utf-8")
    assert "document.cookie" not in source
    assert "tingwu" not in source.lower()
    (root / "content.js").write_text("user edit", encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="adapter_modified"):
        install_xiaoe_adapters(tmp_path)
