"""Read-only Zhihu extraction through the managed OpenCLI adapter."""
from __future__ import annotations

import json
import re
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

from openmd.backends.base import BackendCapability, BackendDescriptor, BackendExtractRequest, BackendHealth
from openmd.backends.executor import BackendProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions
from openmd.core.models import TranscriptResult
from openmd.core.opencli_output import load_opencli_json
from openmd.core.secrets import sanitize_error_summary
from openmd.runtime.managed_tools import managed_package_root
from openmd.runtime.interpreters import run_interruptibly
from openmd.runtime.managed_tools import command_environment, managed_command

ZHIHU_BACKEND = "opencli.zhihu.read"
MAX_ANSWERS = 1000
TIME_LIMIT_SECONDS = 300


def _question_coverage_partial(answer_count: int, declared_total: int | None) -> bool:
    """Never claim a question is complete without a reliable total receipt."""
    return declared_total is None or answer_count >= MAX_ANSWERS or answer_count < declared_total


class _Markdown(HTMLParser):
    """Small HTML-to-Markdown converter preserving common rich-text structure."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.stack = [], []
        self.pre_depth = 0
        self.code_depth = 0
        self.pre_code_seen = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"p", "div", "section"}: self.out.append("\n\n")
        elif tag in {"br"}: self.out.append("\n")
        elif tag in {"h1", "h2", "h3", "h4"}: self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in {"blockquote"}: self.out.append("\n\n> ")
        elif tag == "pre":
            self.pre_depth += 1
            self.pre_code_seen = False
            self.out.append("\n\n")
        elif tag in {"strong", "b"}: self.out.append("**")
        elif tag in {"em", "i"}: self.out.append("*")
        elif tag == "code":
            self.code_depth += 1
            if self.pre_depth:
                self.pre_code_seen = True
                classes = str(attrs.get("class") or "").split()
                language = next((item.removeprefix("language-") for item in classes if item.startswith("language-")), "")
                self.out.append("```" + language + "\n")
            else:
                self.out.append("`")
        elif tag == "a": self.stack.append((tag, attrs.get("href", ""))); self.out.append("[")
        elif tag == "img":
            src = attrs.get("data-original") or attrs.get("data-actualsrc") or attrs.get("src") or ""
            alt = attrs.get("alt") or "image"
            formula = attrs.get("data-tex") or attrs.get("data-formula") or alt
            if "eeimg" in src.lower() and formula: self.out.append(f"${formula}$")
            elif src.startswith(("https://", "http://")): self.out.append(f"![{alt}]({src})")
        elif tag == "figure": self.out.append("\n\n")
        elif tag == "hr": self.out.append("\n\n---\n\n")

    def handle_endtag(self, tag):
        if tag in {"p", "div", "section", "figure"}: self.out.append("\n\n")
        elif tag in {"pre"}:
            self.pre_depth = max(0, self.pre_depth - 1)
            if not self.pre_code_seen:
                self.out.append("\n```\n\n")
        elif tag in {"strong", "b"}: self.out.append("**")
        elif tag in {"em", "i"}: self.out.append("*")
        elif tag == "code":
            self.code_depth = max(0, self.code_depth - 1)
            self.out.append("```\n" if self.pre_depth else "`")
        elif tag == "a" and self.stack:
            _, href = self.stack.pop()
            if href.startswith(("https://", "http://")): self.out.append(f"]({href})")
            else: self.out.append("]")
        elif tag == "blockquote": self.out.append("\n\n")

    def handle_data(self, data): self.out.append(data)

    def text(self):
        return re.sub(r"\n{3,}", "\n\n", "".join(self.out)).strip()


def html_to_markdown(value: str) -> str:
    parser = _Markdown(); parser.feed(value or ""); return parser.text()


def register_backend_providers(registry: BackendProviderRegistry, options: BackendRuntimeOptions) -> None:
    if options.backend_override in {None, ZHIHU_BACKEND} and options.enable_browser_backends:
        registry.register(ZHIHU_BACKEND, ZhihuOpenCliProvider())
        from openmd.backends.kaze_snapshot import KazeSnapshotProvider
        registry.register("browser.kaze.snapshot", KazeSnapshotProvider())


class ZhihuOpenCliProvider:
    name = ZHIHU_BACKEND

    def __init__(self, *, runner=None, timeout: int = TIME_LIMIT_SECONDS):
        self.runner, self.timeout = runner or _run_zhihu, timeout

    def descriptor(self):
        return BackendDescriptor(name=self.name, layer="managed OpenCLI browser adapter",
            capabilities=(BackendCapability(platforms=("zhihu",), source_types=("article",), input_types=("single",),
                source_levels=("third_party_extractor", "partial_extraction"), requires_network=True, requires_credentials=True),),
            description="Read Zhihu articles and answers through the managed OpenCLI adapter; never invokes write commands.", provider=self.name)

    def health(self):
        try:
            package = json.loads((managed_package_root() / "package.json").read_text(encoding="utf-8"))
            return BackendHealth(ok=package.get("version") == "2.0.3", dependencies={"opencli": str(package.get("version"))})
        except (OSError, ValueError):
            return BackendHealth(ok=False, error_code="tool_not_installed", error_summary="The managed OpenCLI package is unavailable.")

    def extract(self, request: BackendExtractRequest) -> TranscriptResult:
        item = request.item
        parsed = urlparse(item.url); parts = [p for p in parsed.path.split("/") if p]
        kind = str(item.raw_meta.get("zhihu_kind") or ("answer" if "answer" in parts else parts[0] if parts else ""))
        with tempfile.TemporaryDirectory(prefix="openmd-zhihu-") as temp:
            workdir = Path(temp)
            args = self._args(kind, parts, item.url, workdir)
            if args is None: return self._failed("zhihu_target_unsupported", "This Zhihu target is not supported by the read-only OpenCLI route.")
            started = time.monotonic()
            try:
                code, stdout, stderr = self.runner(args, timeout=max(1, min(self.timeout, TIME_LIMIT_SECONDS)))
            except Exception as exc:
                return self._failed("zhihu_opencli_failed", sanitize_error_summary(exc) or "OpenCLI read failed.")
            elapsed = time.monotonic() - started
            if code:
                summary = "Zhihu requires login or rejected this request." if code == 77 else "The managed OpenCLI Zhihu read command failed."
                return self._failed("login_required" if code == 77 else f"backend_exit_{code}", summary)
            try:
                payload = load_opencli_json(stdout)
                if kind == "collection":
                    text, title, author, note, collection_partial, declared_total = self._read_collection(payload, workdir, started)
                else:
                    text, title, author, note = self._read_payload(kind, payload, item.url, workdir)
                    collection_partial = False
            except (OSError, ValueError, TypeError, KeyError) as exc:
                return self._failed("zhihu_output_invalid", sanitize_error_summary(exc) or "OpenCLI returned no validated source content.")
        if not text.strip(): return self._failed("zhihu_content_empty", "The Zhihu page returned no exportable content.")
        declared_total = None
        if kind == "question" and isinstance(payload, list):
            declared_total = next((row.get("declared_total") for row in payload if isinstance(row, dict) and row.get("declared_total") is not None), None)
            try:
                declared_total = int(declared_total) if declared_total is not None else None
            except (TypeError, ValueError):
                declared_total = None
        partial = collection_partial or (
            kind == "question" and isinstance(payload, list)
            and _question_coverage_partial(len(payload), declared_total)
        )
        status = "partial" if partial or elapsed >= self.timeout else "verified"
        if partial and kind == "question":
            observed = f"取得 {len(payload)} 条回答"
            expected = f"，页面声明约 {declared_total} 条" if declared_total is not None else "，总数未知"
            note = note + f"；覆盖为 partial：{observed}{expected}，受数量上限 {MAX_ANSWERS} 或分页结果限制"
        return TranscriptResult(text=text.rstrip() + "\n", status=status,
            source_level="partial_extraction" if status == "partial" else "third_party_extractor", backend=self.name,
            title=title or item.title, author=author, source_notes=[note, "Original source content; no LLM rewriting."],
            metadata={"zhihu_kind": kind, "answer_count": len(payload) if kind == "question" and isinstance(payload, list) else None,
                      "declared_answer_count": declared_total,
                      "elapsed_seconds": round(elapsed, 3), "quantity_limit": MAX_ANSWERS if kind == "question" else None,
                      "time_limit_seconds": self.timeout})

    @staticmethod
    def _args(kind, parts, url, workdir):
        flags = ["-f", "json", "--window", "background", "--site-session", "ephemeral", "--keep-tab", "false"]
        if kind == "question" and len(parts) >= 2: return ["opencli", "zhihu", "question", parts[1], "--limit", str(MAX_ANSWERS), *flags]
        if kind == "answer" and len(parts) >= 4: return ["opencli", "zhihu", "download", "--url", url, "--output", str(workdir), "--download-images", "false", *flags]
        if kind in {"p", "article"} and len(parts) >= 2: return ["opencli", "zhihu", "download", "--url", url, "--output", str(workdir), "--download-images", "false", *flags]
        if kind == "collection" and len(parts) >= 2: return ["opencli", "zhihu", "collection", parts[1], "--offset", "0", "--limit", str(MAX_ANSWERS), *flags]
        return None

    @staticmethod
    def _read_payload(kind, payload, source_url, workdir):
        if kind == "question":
            if not isinstance(payload, list): raise ValueError("Question output was not a list.")
            title = next((str(row.get("question_title")) for row in payload if isinstance(row, dict) and row.get("question_title")), "") or "知乎问题"
            detail = next((html_to_markdown(str(row.get("question_detail") or "")) for row in payload if isinstance(row, dict) and row.get("question_detail")), "")
            blocks = [f"# {title}"]
            if detail:
                blocks.append(f"问题描述\n\n{detail}")
            for row in payload:
                if not isinstance(row, dict) or not row.get("id"): raise ValueError("Question answer lacked stable identity.")
                body = html_to_markdown(str(row.get("content") or ""))
                if not body: raise ValueError("Question answer content was empty.")
                published = row.get("published_at") or row.get("created_time") or row.get("updated_time")
                timing = f"\n\n时间：{published}" if published else ""
                answer_rank = row.get("rank") or sum(1 for block in blocks if block.startswith("## 回答 ")) + 1
                blocks.append(f"## 回答 {answer_rank}\n\n作者：{row.get('author') or '匿名用户'}\n\n赞同：{row.get('votes', 0)}{timing}\n\n来源：{row.get('url') or ''}\n\n{body}")
            answer_count = sum(1 for block in blocks if block.startswith("## 回答 "))
            return "\n\n---\n\n".join(blocks), title, None, f"取得 {answer_count} 条回答；问题页 answers command 返回条数作为覆盖观察值"
        if kind in {"answer", "p", "article"}:
            rows = _first(payload); candidates = list(workdir.glob("**/*.md"))
            if candidates:
                text = candidates[0].read_text(encoding="utf-8")
                heading = re.search(r"(?m)^#\s+(.+)$", text)
                title = str(rows.get("title") or (heading.group(1).strip() if heading else "知乎内容"))
                return text, title, str(rows.get("author") or ""), "由 OpenCLI download 读取 Markdown 产物"
            raise ValueError("OpenCLI download did not return a Markdown artifact.")
        raise ValueError("Collection export needs a content expansion route.")

    def _read_collection(self, payload, workdir, started):
        if not isinstance(payload, list): raise ValueError("Collection output was not a list.")
        blocks, partial = [], False
        title = "知乎收藏夹"
        declared_total = next((row.get("declared_total") for row in payload if isinstance(row, dict) and row.get("declared_total") is not None), None)
        try:
            declared_total = int(declared_total) if declared_total is not None else None
        except (TypeError, ValueError):
            declared_total = None
        for row in payload[:MAX_ANSWERS]:
            if not isinstance(row, dict) or not row.get("url"):
                partial = True; continue
            remaining = self.timeout - (time.monotonic() - started)
            if remaining <= 1:
                partial = True; break
            kind = {"answer": "answer", "article": "article", "pin": "pin"}.get(str(row.get("type")))
            if kind not in {"answer", "article", "pin"}:
                blocks.append(f"## {row.get('title') or '想法'}\n\n作者：{row.get('author') or '匿名用户'}\n\n赞同：{row.get('votes', 0)}\n\n{row.get('excerpt') or ''}\n\n来源：{row['url']}")
                partial = True
                continue
            parts = [p for p in urlparse(row["url"]).path.split("/") if p]
            item_workdir = workdir / f"item-{len(blocks)}"
            item_workdir.mkdir(parents=True, exist_ok=True)
            args = self._args(kind, parts, row["url"], item_workdir)
            if not args:
                partial = True; continue
            code, output, _ = self.runner(args, timeout=max(1, min(30, int(remaining))))
            if code:
                partial = True; continue
            try:
                body, item_title, author, _ = self._read_payload(kind, load_opencli_json(output), row["url"], item_workdir)
            except (OSError, ValueError, TypeError, KeyError):
                partial = True; continue
            blocks.append(f"## {item_title or row.get('title') or '收藏内容'}\n\n作者：{author or row.get('author') or '匿名用户'}\n\n赞同：{row.get('votes', 0)}\n\n{body}\n\n来源：{row['url']}")
        if len(payload) >= MAX_ANSWERS or (declared_total is not None and len(payload) < declared_total): partial = True
        total_note = f"，页面声明约 {declared_total} 条" if declared_total is not None else "，总数未知"
        note = f"收藏夹条目 {len(payload)} 条{total_note}；已展开全文 {len(blocks)} 条" + ("；部分条目因内容类型或时间预算保留为摘要/未能展开" if partial else "")
        return "\n\n---\n\n".join(blocks), title, None, note, partial, declared_total

    def _failed(self, code, summary):
        return TranscriptResult(text="", status="failed", source_level="partial_extraction", backend=self.name,
                                error_code=code, error_summary=summary, source_notes=["No unvalidated page or status text was saved."])


def _first(payload):
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict): raise ValueError("OpenCLI output was not a nonempty object list.")
    return payload[0]


def _run_zhihu(args, *, timeout=TIME_LIMIT_SECONDS):
    """Run only enumerated read commands from the managed OpenCLI install."""
    allowed = {"answer-detail", "question", "download", "collection"}
    if len(args) < 3 or args[:2] != ["opencli", "zhihu"] or args[2] not in allowed:
        return 2, "", "unsupported_zhihu_command"
    env = command_environment()
    try:
        done = run_interruptibly(managed_command(args[1:]), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env, check=False)
        return done.returncode, done.stdout, done.stderr
    except FileNotFoundError:
        return 127, "", "managed_opencli_missing"
    except TimeoutError:
        return 124, "", "zhihu_read_timeout"
