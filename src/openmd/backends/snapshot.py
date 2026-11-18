"""Current page snapshot backend.

Snapshots are local HTML captured by the extension. Defuddle is used when
available; a small stdlib fallback keeps offline CLI fixtures testable without
silently claiming broader page coverage.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from openmd.backends.base import BackendCapability, BackendDescriptor, BackendExtractRequest, BackendHealth
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.content_validation import html_to_markdown
from openmd.core.models import TranscriptResult
from openmd.runtime.interpreters import run_interruptibly


SNAPSHOT_BACKEND = "page_snapshot.defuddle"
MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024


def register_backend_providers(registry: BackendProviderRegistry, options: BackendRuntimeOptions) -> None:
    if options.backend_override in {None, SNAPSHOT_BACKEND}:
        registry.register(SNAPSHOT_BACKEND, SnapshotProvider())


class SnapshotProvider:
    name = SNAPSHOT_BACKEND

    def __init__(self, *, runner=None, max_bytes: int = MAX_SNAPSHOT_BYTES) -> None:
        self.runner = runner
        self.max_bytes = max_bytes

    def descriptor(self) -> BackendDescriptor:
        return BackendDescriptor(
            name=self.name,
            layer="L1 local snapshot extractor",
            capabilities=(BackendCapability(
                platforms=("page_snapshot",), source_types=("article",), input_types=("single",),
                source_levels=("browser_rendered", "third_party_extractor", "partial_extraction"),
                requires_network=False,
            ),),
            description="Extract a browser captured HTML snapshot through Defuddle.",
            provider="openmd.backends.snapshot",
        )

    def health(self) -> BackendHealth:
        if self.runner is not None:
            return BackendHealth(ok=True, dependencies={"defuddle": "injected"})
        return BackendHealth(ok=True, dependencies={"defuddle": "available" if shutil.which("defuddle") else "fallback"})

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        path_value = item.raw_meta.get("snapshot_path")
        if not isinstance(path_value, str) or not path_value:
            return _failed("snapshot_missing_path", "page_snapshot 输入缺少本地快照路径。")
        path = Path(path_value)
        try:
            if not path.is_file():
                return _failed("snapshot_missing_file", "当前页快照文件不存在。")
            if path.stat().st_size > self.max_bytes:
                return _failed("snapshot_too_large", "当前页快照超过 50 MB 上限。")
            html = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return _failed("snapshot_read_failed", "当前页快照无法读取。")
        title = item.title or _html_title(html) or "当前页快照"
        markdown = self._defuddle(path, html)
        text = (markdown or "").strip()
        if text.startswith("# ") and text.splitlines()[0].strip().removeprefix("# ").strip() == title.strip():
            text = "\n".join(text.splitlines()[1:]).lstrip()
        if not text:
            return _failed("snapshot_empty", "当前页快照没有可保存的正文。")
        notes = ["当前页快照仅覆盖浏览器捕获的可见 DOM，不包含 cookie 或 storage。"]
        hint = snapshot_acceleration_hint(item.url)
        if hint:
            notes.append(hint)
        return TranscriptResult(
            text=text.rstrip() + "\n",
            status="verified",
            source_level="browser_rendered",
            backend=self.name,
            title=title,
            source_notes=notes,
            metadata={"captured_from": "browser_snapshot", "coverage": "visible_only", "snapshot_path": str(path)},
        )

    def _defuddle(self, path: Path, html: str) -> str:
        if self.runner is not None:
            result = self.runner(path, html)
            if isinstance(result, dict):
                return str(result.get("contentMarkdown") or result.get("content") or "")
            return str(result or "")
        executable = shutil.which("defuddle")
        if executable:
            with tempfile.TemporaryDirectory(prefix="openmd-defuddle-") as tmp:
                output = Path(tmp) / "content.json"
                command = [executable, "parse", str(path), "--markdown", "--json", "-o", str(output)]
                try:
                    completed = run_interruptibly(command, capture_output=True, text=True, timeout=120, check=False, encoding="utf-8", errors="replace")
                    if completed.returncode == 0 and output.is_file():
                        payload = json.loads(output.read_text(encoding="utf-8"))
                        return str(payload.get("contentMarkdown") or payload.get("content") or "") if isinstance(payload, dict) else ""
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
        try:
            return html_to_markdown(html)
        except Exception:
            return re.sub(r"<[^>]+>", "\n", html)


def snapshot_acceleration_hint(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    if host == "mp.weixin.qq.com" or host.endswith(".weixin.qq.com"):
        return "该 URL 有公众号平台路线，可以用平台路线完整抓取。"
    if host.endswith("xiaohongshu.com") or host.endswith("xhslink.com"):
        return "该 URL 有小红书平台路线，可以用平台路线完整抓取。"
    if host.endswith("chatgpt.com") or host.endswith("claude.ai") or host.endswith("deepseek.com"):
        return "该 URL 有 AI 对话平台路线，可以用对话导出获取完整分支。"
    return None


def _html_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not match:
        return None
    value = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", match.group(1))).strip()
    return value or None


def _failed(code: str, summary: str) -> TranscriptResult:
    return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=SNAPSHOT_BACKEND, error_code=code, error_summary=summary, source_notes=["browser snapshot provider returned a structured failure."])
