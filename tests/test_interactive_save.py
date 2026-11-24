from __future__ import annotations

import json
from pathlib import Path

from openmd.cli import main
from openmd import interactive


def test_noninteractive_missing_destination_does_not_start_extraction(monkeypatch, capsys):
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)
    assert main(["go", "https://example.org/article", "--yes", "--backend", "mock"]) == 2
    assert "--save-to" in capsys.readouterr().err


def test_bare_link_saves_valid_package_and_relocates_receipt(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)
    assert main(["https://example.org/article", "--backend", "mock", "--yes", "--package", "--receipt", "compact", "--json", "--save-to", str(tmp_path / "dest")]) == 0
    output = json.loads(capsys.readouterr().out.splitlines()[-1])
    target = Path(output["saved_to"])
    from openmd.core.package_validator import validate_package

    packages = list(target.rglob("*.openmd"))
    assert len(packages) == 1 and validate_package(packages[0]).ok
    for path in target.rglob("*"):
        if path.suffix in {".json", ".jsonl"}:
            assert "openmd-staging-" not in path.read_text(encoding="utf-8")
    assert all(Path(p).exists() for p in output["outputs"])
    assert not (target / ".openmd-compact.lock").exists()
    assert not (target / ".openmd-compact.guard").exists()
    assert not (target / "runlog.jsonl").exists()
    records = Path(output["records"])
    assert records.is_relative_to(tmp_path / "data")
    runlog = [json.loads(line) for line in (records / "runlog.jsonl").read_text(encoding="utf-8").splitlines()]
    assert runlog[0]["route_trace"]["route_version"]
    assert runlog[0]["route_trace"]["overlay_applied"] is False
    receipt = next((records / "runs").glob("*/items.jsonl"))
    assert json.loads(receipt.read_text(encoding="utf-8"))["route_trace"] == runlog[0]["route_trace"]


def test_interactive_only_prompts_for_save_after_success(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(interactive, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        assert "保存在哪里" in prompt
        assert "已取得正文" in capsys.readouterr().out
        return str(tmp_path / "dest")

    monkeypatch.setattr("builtins.input", answer)
    assert main(["go", "https://example.org/article", "--backend", "mock"]) == 0
    assert len(prompts) == 1
    assert len(list((tmp_path / "dest").glob("*.md"))) == 1
    assert "已保存：" in capsys.readouterr().out


def test_failed_extraction_does_not_prompt_for_save(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setenv("OPENMD_DATA_DIR", str(tmp_path / "home/.openmd"))
    monkeypatch.setattr(interactive, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(AssertionError("save prompt on failure")))
    assert main(["go", "https://example.org/article", "--backend", "not-installed", "--yes", "--save-to", str(tmp_path)]) == 1
    assert not (tmp_path / "packages").exists()
    assert len(list((tmp_path / "home/.openmd/runs").glob("*/runlog.jsonl"))) == 1


def test_yes_without_destination_fails_without_prompt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(interactive, "is_interactive", lambda: True)
    monkeypatch.setattr(interactive.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(AssertionError("--yes must not prompt")))
    assert main(["go", "https://example.org/article", "--backend", "mock", "--yes"]) == 2
    assert "保存目录" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_existing_permission_flags_remain_narrow(monkeypatch):
    from openmd.cli import build_parser

    args = build_parser().parse_args(["go", "https://example.org", "--enable-network-backends"])
    monkeypatch.setattr(interactive, "_ask", lambda prompt: (_ for _ in ()).throw(AssertionError("start prompt")))
    assert interactive._authorize(args, args.url, tty=True)
    assert args.enable_network_backends
    assert not args.enable_browser_backends
    assert not args.enable_media_download
    assert not args.enable_asr


def test_daily_start_has_no_prompt_and_enables_needed_local_gates(monkeypatch):
    from openmd.cli import build_parser

    args = build_parser().parse_args(["go", "https://example.org"])
    monkeypatch.setattr(interactive, "_ask", lambda prompt: (_ for _ in ()).throw(AssertionError("start prompt")))
    assert interactive._authorize(args, args.url, tty=True)
    assert args.enable_network_backends
    assert not args.enable_cloud_backends


def test_known_command_not_shadowed_by_local_filename(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "routes").write_text("user file", encoding="utf-8")
    assert main(["routes", "--source", "x", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["routes"][0]["source"] == "x"


def test_noninteractive_destination_alone_is_enough(tmp_path, monkeypatch):
    # User decision 2026-11-15: a destination is the grant; --yes is optional.
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)
    assert main(["https://example.org/article", "--backend", "mock", "-o", str(tmp_path)]) == 0
    assert list(tmp_path.rglob("*.md"))


def test_repeated_saves_preserve_existing_content(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)
    args = ["go", "https://example.org/article", "--backend", "mock", "--yes", "--save-to", str(tmp_path)]
    assert main(args) == 0
    first = next(tmp_path.rglob("*.md"))
    first.write_text("user edited this", encoding="utf-8")
    assert main(args) == 0
    assert first.read_text(encoding="utf-8") == "user edited this"
    assert len(list(tmp_path.rglob("*.md"))) == 2


def test_go_rejects_bulk_before_running(monkeypatch):
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)
    assert main(["go", "https://example.org", "--all", "--yes", "--save-to", "ignored"]) == 2


def test_daily_start_enables_browser_without_prompt_when_route_needs_it(monkeypatch):
    from openmd.cli import build_parser

    args = build_parser().parse_args(["go", "https://www.youtube.com/watch?v=AIBfnG1it0s"])
    monkeypatch.setattr(interactive, "_ask", lambda prompt: (_ for _ in ()).throw(AssertionError("prompt")))
    assert interactive._authorize(args, args.url, tty=True)
    assert args.enable_browser_backends
    assert not args.enable_cloud_backends


def test_daily_douyin_save_enables_media_and_local_asr_without_prompt(monkeypatch):
    from openmd.cli import build_parser

    args = build_parser().parse_args(["go", "https://v.douyin.com/oAI5iHKX4JQ/", "-o", "D:/openmd-run"])
    monkeypatch.setattr(interactive, "_ask", lambda prompt: (_ for _ in ()).throw(AssertionError("prompt")))
    assert interactive._authorize(args, args.url, tty=False)
    assert args.enable_browser_backends and args.enable_media_download and args.enable_asr
    assert not args.enable_cloud_backends


def test_xiaohongshu_block_has_loaded_page_next_action():
    from openmd.cli_login import source_next_action

    action = source_next_action("xiaohongshu_request_blocked")
    assert action and "浏览器打开" in action and "扩展保存" in action
    assert source_next_action("xiaohongshu_loaded_page_mismatch") == action


def test_douyin_snapshot_cli_returns_structured_error_without_markdown(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(interactive, "is_interactive", lambda: False)
    page = tmp_path / "douyin.html"
    page.write_text("<html><body><main>页面文案，不是逐字稿</main></body></html>", encoding="utf-8")
    destination = tmp_path / "saved"
    code = main([
        "go", str(page), "--as-snapshot", "--url", "https://www.douyin.com/video/123456",
        "--yes", "--json", "--save-to", str(destination),
    ])
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert code == 1
    assert payload["error_code"] == "douyin_snapshot_no_transcript"
    assert payload["next_action"] == "抖音视频需走媒体转写：openmd go <链接>（扩展点击会走链接路线）"
    assert not list(destination.rglob("*.md")) if destination.exists() else True
