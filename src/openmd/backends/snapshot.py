"""Current page snapshot backend.

Snapshots are local HTML captured by the extension. Defuddle is used when
available; a small stdlib fallback keeps offline CLI fixtures testable without
silently claiming broader page coverage.
"""

from __future__ import annotations

import json
import os
import re
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
        return BackendHealth(ok=True, dependencies={"defuddle": "managed" if _managed_defuddle_available() else "fallback"})

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
        markdown, used_managed = self._html_to_markdown(path, html, item.url)
        text = (markdown or "").strip()
        if text.startswith("# ") and text.splitlines()[0].strip().removeprefix("# ").strip() == title.strip():
            text = "\n".join(text.splitlines()[1:]).lstrip()
        if not text:
            return _failed("snapshot_empty", "当前页快照没有可保存的正文。")
        notes = ["当前页快照仅覆盖浏览器捕获的可见 DOM，不包含 cookie 或 storage。"]
        if not used_managed:
            notes.append("未使用 Defuddle，正文抽取为简易回退")
        hint = snapshot_acceleration_hint(item.url)
        if hint:
            notes.append(hint)
        return TranscriptResult(
            text=text.rstrip() + "\n",
            status="verified",
            source_level="third_party_extractor" if used_managed else "partial_extraction",
            backend=self.name,
            title=title,
            source_notes=notes,
            metadata={"captured_from": "browser_snapshot", "coverage": "visible_branch", "snapshot_path": str(path)},
        )

    def _html_to_markdown(self, path: Path, html: str, url: str) -> tuple[str, bool]:
        if self.runner is not None:
            result = self.runner(path, html)
            if isinstance(result, dict):
                return str(result.get("contentMarkdown") or result.get("content") or ""), True
            return str(result or ""), True
        managed = _run_managed_defuddle(path, html, url)
        if managed:
            return managed, True
        try:
            return html_to_markdown(html), False
        except Exception:
            return re.sub(r"<[^>]+>", "\n", html), False


def _defuddle_card_path() -> Path:
    return Path(__file__).resolve().parents[1] / "routing" / "cards" / "defuddle.json"


def _local_config_candidates() -> list[Path]:
    values: list[Path] = []
    configured = os.environ.get("OPENMD_CONFIG")
    if configured:
        values.append(Path(configured))
    here = Path(__file__).resolve()
    values.extend([Path.cwd() / "out" / "local-config.json", here.parents[5] / "out" / "local-config.json"])
    return values


def _managed_defuddle_spec() -> tuple[str, str, str, dict[str, Any]] | None:
    try:
        card = json.loads(_defuddle_card_path().read_text(encoding="utf-8"))
        api = card["launcher"]["node_api"]
        module = str(api["module"])
        options = dict(api.get("options") or {})
        config = next((p for p in _local_config_candidates() if p.is_file()), None)
        if config is None:
            return None
        tools = json.loads(config.read_text(encoding="utf-8")).get("tools", {})
        spec = tools.get("defuddle") or {}
        node = str(spec.get("node") or "")
        root = str(spec.get("root") or "")
        if not node or not root or not Path(node).is_file() or not Path(root).is_dir():
            return None
        return node, root, module, options
    except (OSError, TypeError, ValueError, KeyError):
        return None


def _managed_defuddle_available() -> bool:
    return _managed_defuddle_spec() is not None


def _run_managed_defuddle(path: Path, html: str, url: str) -> str | None:
    spec = _managed_defuddle_spec()
    if spec is None:
        return None
    node, root, module, options = spec
    script = r"""
const path = require('path');
const fs = require('fs');
const { Defuddle } = require(path.join(process.argv[1], process.argv[2]));
const { parseHTML } = require(path.join(process.argv[1], 'linkedom'));
(async () => {
  const html = fs.readFileSync(process.argv[3], 'utf8');
  const { document } = parseHTML(html);
  const result = await Defuddle(document, process.argv[4], { ...JSON.parse(process.argv[5]), markdown: true });
  process.stdout.write(JSON.stringify({ contentMarkdown: result.contentMarkdown || '', content: result.content || '' }));
})().catch((error) => { process.stderr.write(String(error && error.stack || error)); process.exit(1); });
"""
    try:
        completed = run_interruptibly(
            [node, "-e", script, root, module, str(path), url, json.dumps(options)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            return None
        payload = json.loads(completed.stdout or "{}")
        if isinstance(payload, dict):
            return str(payload.get("contentMarkdown") or payload.get("content") or "") or None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return None


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
