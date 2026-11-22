from argparse import Namespace

from openmd import cli as cli_module
from openmd.adapters.registry import BackendSelection
from openmd.core.models import ContentItem


def test_successful_public_backend_does_not_prepare_opencli_fallback(tmp_path, monkeypatch):
    class Adapter:
        id = "web_article"

        def resolve_items(self, input_value, *, limit=None, all=False):
            return [ContentItem(platform=self.id, input_type="single", url=input_value, source_type="article")]

    class Registry:
        def adapter_for(self, input_value):
            return Adapter()

        def select_backend(self, platform, requested=None, *, allow_fallback=True):
            return BackendSelection(platform, "mock", ("opencli.web.read",))

    calls = []
    monkeypatch.setattr(cli_module, "create_default_registry", lambda: Registry())
    from openmd.runtime import managed_tools
    monkeypatch.setattr(managed_tools, "ensure_opencli", lambda *args, **kwargs: calls.append("prepare"))

    result = cli_module._process_input(
        "https://example.org/article",
        Namespace(
            all=False, as_snapshot=False, snapshot_url=None, include_thinking=False,
            ocr_engine=None, fallback=True, limit=None, backend=None,
            enable_network_backends=True, out=str(tmp_path), overwrite=False,
            runlog=None, enable_browser_backends=False, enable_media_download=False,
            enable_asr=False, enable_cloud_backends=False, enable_local_export=False,
            keep_media=False, progress=False,
        ),
        command="go",
    )

    assert result.ok
    assert calls == []
