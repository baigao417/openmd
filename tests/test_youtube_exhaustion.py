import json
import contextlib
import io
from pathlib import Path
from unittest.mock import patch

import pytest

from openmd.backends.base import DiscoveryRequest
from openmd.backends.youtube_discovery import YouTubeDataApiPlaylistProvider, YouTubeDataApiChannelProvider


def page(ids, token=None):
    value = {"items": [{"contentDetails": {"videoId": v}, "snippet": {"title": v}} for v in ids]}
    if token is not None:
        value["nextPageToken"] = token
    return value


def make_provider(pages, cls=YouTubeDataApiPlaylistProvider, **kwargs):
    calls = []
    sequence = iter(pages)
    def fetch(endpoint, params, **options):
        calls.append((endpoint, {k: v for k, v in params.items() if k != "key"}))
        if endpoint == "channels":
            return 200, json.dumps({"items": [{"id": "UCfixture", "contentDetails": {"relatedPlaylists": {"uploads": "UUfixture"}}}]})
        if endpoint == "videos":
            return 200, json.dumps({"items": []})
        response = next(sequence)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, tuple):
            return response
        return 200, json.dumps(response)
    return cls(api_key="fixture-key", fetcher=fetch, request_interval=0, **kwargs), calls


def discover(provider, **kwargs):
    target = "https://www.youtube.com/@fixture" if provider.name == "youtube.data-api.discovery" else "PLfixture"
    return provider.discover(DiscoveryRequest(target, "youtube", allow_all=True, **kwargs))


@pytest.mark.parametrize("cls", [YouTubeDataApiPlaylistProvider, YouTubeDataApiChannelProvider])
def test_all_follows_real_cursor_until_source_exhausted(cls):
    provider, calls = make_provider([page(["one"], "second"), page(["two"], "third"), page(["three"])], cls)
    result = discover(provider)
    assert result.ok and result.metadata["source_exhausted"]
    assert [i.id for i in result.items] == ["one", "two", "three"]
    assert result.metadata["page_count"] == 3
    assert all(params["maxResults"] == "50" for endpoint, params in calls if endpoint == "playlistItems")
    assert "fixture-key" not in repr(result)


@pytest.mark.parametrize("pages,code", [
    ([page(["one"], "same"), page(["two"], "same")], "youtube_data_api_cursor_loop"),
    ([page(["one"], "next"), page([], "more")], "youtube_data_api_empty_page_with_cursor"),
    ([page(["one"], "next"), page(["one"], "more")], "youtube_data_api_repeated_page"),
    ([page(["one"], "next"), (503, "unavailable")], "youtube_data_api_playlist_request_failed"),
    ([page(["one"], "next"), KeyboardInterrupt()], "youtube_data_api_interrupted"),
    ([page(["one"], "next"), {"nextPageToken": "bad"}], "youtube_data_api_invalid_items"),
])
def test_incomplete_all_preserves_discovered_items(pages, code):
    provider, calls = make_provider(pages)
    result = discover(provider)
    assert not result.ok
    assert result.error_code == code
    assert not result.metadata["source_exhausted"]
    assert result.items[0].id == "one"
    assert all(endpoint != "videos" for endpoint, _ in calls)


@pytest.mark.parametrize("budget,code", [({"max_pages": 1}, "youtube_data_api_page_budget_exceeded"), ({"max_requests": 1}, "youtube_data_api_request_budget_exceeded")])
def test_budget_stops_with_partial_manifest_records(budget, code):
    provider, calls = make_provider([page(["one"], "next")], **budget)
    result = discover(provider)
    assert not result.ok and result.error_code == code
    assert len(result.items) == 1 and len(calls) == 1


def test_empty_exhausted_all_is_success_not_missing_source():
    provider, _ = make_provider([page([])])
    result = discover(provider)
    assert result.ok and result.items == [] and result.metadata["source_exhausted"]


def test_bounded_limit_does_not_claim_all():
    provider, calls = make_provider([page(["one"], "next")])
    result = provider.discover(DiscoveryRequest("PLfixture", "youtube", limit=1))
    assert result.ok and not result.metadata["source_exhausted"]
    assert result.metadata["stop_reason"] == "limit_reached"


def test_duplicate_videos_are_counted_and_do_not_silently_disappear():
    provider, _ = make_provider([page(["one", "one"], "next"), page(["two"])])
    result = discover(provider)
    assert result.ok
    assert result.metadata["duplicate_item_count"] == 1
    assert result.metadata["playlist_item_count"] == 3
    assert len(result.items) == 2


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("complete", [False, True])
def test_cli_all_manifest_does_not_extract_and_preserves_partial_accounting(tmp_path, compact, complete):
    from openmd.cli import main
    pages = [page(["one"], "next"), page(["two"]) if complete else (503, "unavailable")]
    provider, calls = make_provider(pages)
    args = ["collect", "PLfixture", "--all", "--enable-network-backends", "--manifest-only", "--out", str(tmp_path)]
    if compact:
        args += ["--receipt", "compact", "--package"]
    output = io.StringIO()
    with patch("openmd.backends.registry.YouTubeDataApiPlaylistProvider", return_value=provider), patch("openmd.cli._build_backend_executor", side_effect=AssertionError("must not extract")), contextlib.redirect_stdout(output):
        code = main(args)
    result = json.loads(output.getvalue())
    assert code == (0 if complete else 1), result
    manifests = list((tmp_path / "manifests").glob("*.jsonl"))
    assert len(manifests) == 1
    rows = [json.loads(line) for line in manifests[0].read_text().splitlines()]
    assert len(rows) == (2 if complete else 1)
    assert all(row["discovery_only"] and row["process_status"] == "discovered" for row in rows)
    summary = json.loads((tmp_path / "youtube-discovery-summary.json").read_text())
    assert summary["source_exhausted"] is complete
    if compact:
        assert result["counts"]["discovered"] == len(rows)
        assert result["counts"]["unattempted"] == len(rows)
        assert result["counts"]["verified"] == 0
        receipt = json.loads((tmp_path / result["summary_ref"]).read_text())
        assert receipt["accounting_complete"]


@pytest.mark.parametrize("flags", [[], ["--enable-browser-backends"], ["--enable-network-backends", "--discovery-backend", "youtube-opencli"]])
def test_cli_all_gate_blocks_before_any_provider(tmp_path, flags):
    from openmd.cli import main
    with patch("openmd.cli._build_discovery_executor", side_effect=AssertionError("provider ran")), contextlib.redirect_stdout(io.StringIO()):
        assert main(["collect", "PLfixture", "--all", "--manifest-only", "--out", str(tmp_path), *flags]) == 1


@pytest.mark.parametrize("compact", [False, True])
def test_empty_all_manifest_cli(tmp_path, compact):
    from openmd.cli import main
    provider, _ = make_provider([page([])])
    args = ["collect", "PLfixture", "--all", "--enable-network-backends", "--manifest-only", "--out", str(tmp_path)]
    if compact:
        args += ["--receipt", "compact", "--package"]
    output = io.StringIO()
    with patch("openmd.backends.registry.YouTubeDataApiPlaylistProvider", return_value=provider), contextlib.redirect_stdout(output):
        code = main(args)
    assert code == 0, output.getvalue()


def test_opencli_all_remains_blocked_in_plan():
    from openmd.core.planner import build_plan
    result = build_plan("https://www.youtube.com/playlist?list=PLfixture", request_all=True, available_gates={"browser"})
    assert not result.execution_ready
    assert result.blocked_reason == "source exhaustion is not implemented for this operation"
