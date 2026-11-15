import json
from pathlib import Path

import pytest

from openmd import probe
from openmd.backends.base import DiscoveryResult
from openmd.backends.executor import BackendProviderRegistry, DiscoveryProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import ContentItem, TranscriptResult


def config(path, samples):
    path.write_text(json.dumps({"schema": probe.CONFIG_SCHEMA, "samples": samples}), encoding="utf-8")
    return path


def test_config_missing_and_empty_template(tmp_path):
    assert probe.load_probe_samples(tmp_path / "absent") == {}
    with pytest.raises(ValueError, match="probe_config_missing"):
        probe.load_probe_samples(tmp_path / "absent", required=True)
    assert probe.load_probe_samples(Path("docs/examples/probe-config.example.json")) == {}


@pytest.mark.parametrize("samples", [
    {"other": {"url": "https://example.com"}},
    {"x": {"url": "https://example.com", "token": "private"}},
    {"x": {"url": []}},
    {"x": {"url": "https://example.com", "mode": "all"}},
    {"x": {"url": "https://example.com", "discovery_backend": "mock.discovery"}},
])
def test_config_rejects_unknown_or_unsafe_fields(tmp_path, samples):
    with pytest.raises(ValueError, match="probe_config_"):
        probe.load_probe_samples(config(tmp_path / "config.json", samples))


def test_explicit_gates_closed_before_provider_creation(monkeypatch):
    monkeypatch.setattr(probe, "create_runtime_backend_provider_registry", lambda *a: pytest.fail("gate bypass"))
    row = probe.probe_platform("bilibili", probe.ProbeSample("https://www.bilibili.com/video/BV123456"), BackendRuntimeOptions())
    assert row.status == "broken"
    assert row.reason == "probe_network_gates_closed"
    assert not row.attempts


def test_chain_excludes_implicit_xiaoe_tingwu_and_heavy_fallbacks():
    options = BackendRuntimeOptions(enable_network_backends=True, enable_browser_backends=True)
    safe, excluded = probe._safe_chain("xiaoe", ("opencli.xiaoe.transcript", "opencli.xiaoe.content"), options)
    assert safe == ("opencli.xiaoe.content",)
    assert excluded[0]["branch"] == "opencli.xiaoe.transcript"
    safe, excluded = probe._safe_chain("bilibili", ("opencli.bilibili.subtitle", "asr.fallback", "yt-dlp.bilibili", "yutto.bilibili.media-asr"), options)
    assert safe == ("opencli.bilibili.subtitle",)
    assert len(excluded) == 3


def test_missing_samples_and_exception_isolation_sanitization_checkpoint(tmp_path, monkeypatch):
    url = "https://www.bilibili.com/video/PRIVATE?xsec_token=secret"
    snapshots = []

    def platform(platform, sample, options, timeout):
        assert not options.enable_asr_backends
        assert not options.enable_media_download_backends
        assert not options.enable_cloud_backends
        if platform == "bilibili":
            raise RuntimeError(f"failed {sample.url} token=abcdef cookie=sessionvalue")
        return probe.ProbeResult(platform=platform, configured=True, status="ok", branch="real.route")

    original = probe._write_report

    def write(path, report):
        original(path, report)
        snapshots.append(json.loads(path.read_text(encoding="utf-8")))

    monkeypatch.setattr(probe, "_probe_isolated", platform)
    monkeypatch.setattr(probe, "_write_report", write)
    report, path = probe.run_probe(
        {"bilibili": probe.ProbeSample(url), "x": probe.ProbeSample("https://x.com/example/status/123")},
        out=tmp_path, platforms=("bilibili", "youtube", "x"), interval_seconds=0,
        options=BackendRuntimeOptions(enable_network_backends=True, enable_asr_backends=True, enable_cloud_backends=True, enable_media_download_backends=True),
    )
    assert report["summary"] == {"ok": 1, "degraded": 1, "broken": 1}
    assert report["results"][1]["configured"] is False
    assert report["completed"] is True
    assert report["ok"] is False
    checkpoint = next(snapshot for snapshot in snapshots if len(snapshot["results"]) == 1)
    assert checkpoint["completed"] is False
    text = path.read_text(encoding="utf-8")
    assert "PRIVATE" not in text and "abcdef" not in text and "sessionvalue" not in text and url not in text
    assert "https://" not in text


def test_extract_uses_real_executor_and_records_fallback(monkeypatch):
    class Provider:
        def __init__(self, backend, success):
            self.backend, self.success = backend, success

        def extract(self, request):
            assert request.backend_chain == (self.backend,)
            return TranscriptResult(text="这里是一个真实正文测试片段。" if self.success else "", status="verified" if self.success else "failed", source_level="platform_transcript", backend=self.backend, error_code=None if self.success else "source_missing")

    registry = BackendProviderRegistry({"opencli.bilibili.subtitle": Provider("opencli.bilibili.subtitle", False), "bilibili-harvest.core": Provider("bilibili-harvest.core", True)})
    monkeypatch.setattr(probe, "create_runtime_backend_provider_registry", lambda *args: registry)
    row = probe.probe_platform("bilibili", probe.ProbeSample("https://www.bilibili.com/video/BV123456"), BackendRuntimeOptions(enable_network_backends=True, enable_browser_backends=True))
    assert row.status == "degraded"
    assert row.branch == "bilibili-harvest.core"
    assert [a["branch"] for a in row.attempts] == ["opencli.bilibili.subtitle", "bilibili-harvest.core"]
    assert row.extraction_status == "verified"
    assert row.source_level == "platform_transcript"


def test_manifest_is_bounded_and_never_extracts(monkeypatch):
    class Provider:
        name = "opencli.xiaoe.catalog"

        def discover(self, request):
            assert request.limit == 1 and request.allow_all is False
            return DiscoveryResult.success(provider="opencli.xiaoe.catalog", items=[ContentItem(platform="xiaoe", input_type="single", url="https://example.test/item")])

    registry = DiscoveryProviderRegistry({"opencli.xiaoe.catalog": Provider()}, platform_defaults={"xiaoe": "opencli.xiaoe.catalog"})
    monkeypatch.setattr(probe, "create_runtime_discovery_provider_registry", lambda *args: registry)
    monkeypatch.setattr(probe, "create_runtime_backend_provider_registry", lambda *a: pytest.fail("manifest extracted body"))
    row = probe.probe_platform("xiaoe", probe.ProbeSample("https://xet.citv.cn/page", mode="manifest"), BackendRuntimeOptions(enable_browser_backends=True))
    assert row.status == "ok" and row.item_count == 1
    assert row.branch == "opencli.xiaoe.catalog"


def test_probe_cli_dispatch_platform_filter_and_help(tmp_path, monkeypatch, capsys):
    from openmd.cli import build_parser, main

    parser = build_parser()
    assert "{go,batch}" in parser.format_help()
    assert "probe" in parser.format_help()  # advanced-command epilog
    args = parser.parse_args(["probe", "--platform", "x"])
    assert args.func is probe.cmd_probe
    path = config(tmp_path / "config.json", {})
    assert main(["probe", "--config", str(path), "--out", str(tmp_path / "reports"), "--platform", "x", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert len(report["results"]) == 1
    assert report["results"][0]["platform"] == "x"
    assert report["results"][0]["reason"] == "sample_not_configured"


def test_platform_mismatch_is_a_reported_failure():
    row = probe.probe_platform("youtube", probe.ProbeSample("https://www.bilibili.com/video/BV123456"), BackendRuntimeOptions(enable_network_backends=True))
    assert row.error_code == "sample_platform_mismatch"


def test_manifest_mock_is_never_live_health(monkeypatch):
    monkeypatch.setattr(probe, "create_runtime_discovery_provider_registry", lambda *args: DiscoveryProviderRegistry())
    row = probe.probe_platform("youtube", probe.ProbeSample("https://youtube.com/@sample", mode="manifest", discovery_backend="mock.discovery"), BackendRuntimeOptions(enable_network_backends=True))
    assert row.status == "broken"


def test_manifest_refuses_full_creator_discovery_even_when_registered(monkeypatch):
    class FullCreator:
        name = "opencli.openmd-douyin.creator-videos"

        def discover(self, request):
            pytest.fail("full creator discovery must not run during probe")

    registry = DiscoveryProviderRegistry({FullCreator.name: FullCreator()}, platform_defaults={"douyin": FullCreator.name})
    monkeypatch.setattr(probe, "create_runtime_discovery_provider_registry", lambda *args: registry)
    row = probe.probe_platform("douyin", probe.ProbeSample("https://douyin.com/user/sample", mode="manifest", discovery_backend=FullCreator.name), BackendRuntimeOptions(enable_browser_backends=True))
    assert row.status == "broken" and row.error_code == "probe_discovery_not_bounded_or_gate_closed"


def test_douyin_detail_readable_is_separate_from_transcription(monkeypatch):
    from openmd.backends.douyin_detail import DouyinDetailProvider
    monkeypatch.setattr(DouyinDetailProvider, "resolve", lambda self, item: item)
    row = probe.probe_platform("douyin", probe.ProbeSample("https://www.douyin.com/video/1234567890"), BackendRuntimeOptions(enable_browser_backends=True))
    assert row.status == "degraded"
    assert row.detail_readable is True
    assert row.transcription == "not_probed_media_asr_disallowed"
    assert row.branch == "opencli.douyin.detail"
    assert row.live_route_attempted is True


def test_douyin_invalid_manifest_sample_does_not_claim_platform_broken():
    row = probe.probe_platform("douyin", probe.ProbeSample("https://www.douyin.com/video/1234567890", mode="manifest"), BackendRuntimeOptions(enable_browser_backends=True))
    assert row.status == "degraded"
    assert row.error_code == "douyin_single_video_not_supported_for_discovery"
    assert "missing_valid_probe_sample" in row.reason
    assert row.live_route_attempted is False


def test_daemon_preflight_precedes_owned_platform_worker(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr("openmd.runtime.services.ensure_opencli_daemon", lambda *args: events.append("preflight"))

    def worker(platform, sample, options, timeout):
        events.append("worker")
        return probe.ProbeResult(platform=platform, configured=True, status="ok")

    monkeypatch.setattr(probe, "_probe_isolated", worker)
    probe.run_probe({"x": probe.ProbeSample("https://x.com/example/status/123")}, out=tmp_path, platforms=("x",), options=BackendRuntimeOptions(enable_browser_backends=True))
    assert events == ["preflight", "worker"]


def test_metadata_only_degraded_does_not_set_report_ok(tmp_path, monkeypatch):
    monkeypatch.setattr("openmd.runtime.services.ensure_opencli_daemon", lambda *args: None)
    monkeypatch.setattr(probe, "_probe_isolated", lambda *args: probe.ProbeResult(platform="douyin", configured=True, status="degraded", reason="missing_valid_probe_sample"))
    report, _ = probe.run_probe({"douyin": probe.ProbeSample("https://douyin.com/video/1234567890")}, out=tmp_path, platforms=("douyin",), options=BackendRuntimeOptions(enable_browser_backends=True))
    assert report["ok"] is False


def test_timeout_uses_owned_runner_and_reports_failure(monkeypatch):
    import subprocess

    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 2
        assert "https://" not in " ".join(command)
        assert json.loads(kwargs["input"])["platform"] == "x"
        raise subprocess.TimeoutExpired(command, 2)

    monkeypatch.setattr(probe, "run_interruptibly", timeout)
    row = probe._probe_isolated("x", probe.ProbeSample("https://x.com/sample/status/123"), BackendRuntimeOptions(), 2)
    assert row.status == "broken" and row.error_code == "probe_timeout"


def test_isolated_worker_fresh_local_gate_denial():
    row = probe._probe_isolated("x", probe.ProbeSample("https://x.com/sample/status/123"), BackendRuntimeOptions(), 10)
    assert row.error_code == "probe_network_gates_closed"
