"""Read-only CLI-backed platform adapters."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from openmd.core.detector import extract_first_url
from openmd.core.models import ContentItem, TranscriptResult
from openmd.core.secrets import sanitize_error_summary

from .base import PlatformAdapter


BVID_RE = re.compile(r"(BV[0-9A-Za-z]+)")
X_STATUS_RE = re.compile(r"/status/(\d+)")
PODWISE_EPISODE_RE = re.compile(r"https://podwise\.ai/dashboard/episodes/\d+")
XIAOYUZHOU_CREDENTIALS_PATH = Path.home() / ".opencli" / "xiaoyuzhou.json"
XIAOYUZHOU_ANDROID_DEVICE_PROPERTY_KEYS = frozenset(
    {"uuid", "android_id", "oaid", "vaid", "aaid"}
)
XIAOYUZHOU_IOS_DEVICE_PROPERTY_KEYS = frozenset({"idfa", "idfv"})
XIAOYUZHOU_IOS_REQUEST_HEADER_GROUPS = (
    ("user-agent",),
    ("os",),
    ("os-version",),
    ("manufacturer",),
    ("model",),
    ("market",),
    ("app-version",),
    ("app-buildno",),
    ("bundleid", "applicationid"),
)


def _source_type_for(platform: str) -> str:
    if platform in {"bilibili", "youtube", "douyin", "tiktok", "xiaoe"}:
        return "video"
    if platform == "xiaoyuzhou":
        return "audio"
    if platform == "feishu":
        return "document"
    if platform == "xiaohongshu":
        return "mixed"
    return "article"


def _run_command(
    command: list[str], *, timeout: int = 120, cwd: str | Path | None = None
) -> tuple[int, str, str]:
    resolved = shutil.which(command[0]) or command[0]
    completed = subprocess.run(
        [resolved, *command[1:]],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        encoding="utf-8",
        errors="replace",
    )
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _run_opencli(
    args: list[str], *, timeout: int = 120, cwd: str | Path | None = None
) -> tuple[int, str, str]:
    return _run_command(["opencli", *args], timeout=timeout, cwd=cwd)


def _run_podwise(
    args: list[str], *, timeout: int = 180, cwd: str | Path | None = None
) -> tuple[int, str, str]:
    return _run_command(["podwise", *args], timeout=timeout, cwd=cwd)


def _extract_bvid(url: str) -> str | None:
    match = BVID_RE.search(url)
    return match.group(1) if match else None


def _extract_episode_id(url: str) -> str:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "episode":
        return parts[1]
    return url.rstrip("/").split("/")[-1]


def _extract_x_post_id(url: str) -> str | None:
    parsed = urlparse(url)
    match = X_STATUS_RE.search(parsed.path)
    return match.group(1) if match else None


def _extract_x_username(url: str) -> str | None:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if not parts or parts[0] in {"i", "home", "explore", "search"}:
        return None
    return parts[0]


def _extract_markdown_title(markdown: str) -> str | None:
    for line in markdown.splitlines():
        text = line.strip()
        if text.startswith("# "):
            return text.removeprefix("# ").strip() or None
    return None


def _extract_table_title(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells:
            continue
        title = cells[0]
        if title and title.lower() != "title" and set(title) != {"-"}:
            return title
    return None


def _extract_first_markdown_title(directory: str | Path) -> str | None:
    for markdown_file in sorted(Path(directory).rglob("*.md")):
        title = _extract_markdown_title(
            markdown_file.read_text(encoding="utf-8", errors="replace")
        )
        if title:
            return title
    return None


def _extract_podwise_episode_url(text: str) -> str | None:
    match = PODWISE_EPISODE_RE.search(text)
    return match.group(0) if match else None


def _is_xiaoyuzhou_credentials_error(exit_code: int, stderr: str) -> bool:
    stderr_lower = stderr.lower()
    return exit_code == 78 and "xiaoyuzhou" in stderr_lower and "credential" in stderr_lower


def _header_lookup(headers: object, names: tuple[str, ...]) -> str | None:
    if not isinstance(headers, dict):
        return None
    normalized = {str(key).lower(): value for key, value in headers.items()}
    for name in names:
        value = normalized.get(name.lower())
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _check_xiaoyuzhou_credentials(
    path: Path = XIAOYUZHOU_CREDENTIALS_PATH,
) -> tuple[bool, str, str]:
    """Validate the local Xiaoyuzhou credential profile without exposing secrets."""
    if not path.exists():
        return (
            False,
            "xiaoyuzhou_credentials_missing",
            "Missing Xiaoyuzhou credential file at ~/.opencli/xiaoyuzhou.json.",
        )

    try:
        credentials = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (
            False,
            "xiaoyuzhou_credentials_invalid_json",
            "Xiaoyuzhou credential file is not valid JSON.",
        )

    for field in ("access_token", "refresh_token", "device_id"):
        value = credentials.get(field)
        if not isinstance(value, str) or not value.strip():
            return (
                False,
                "xiaoyuzhou_credentials_incomplete",
                f"Xiaoyuzhou credential file is missing required field: {field}.",
            )

    device_id = credentials["device_id"].strip()
    device_properties = credentials.get("device_properties")
    if not isinstance(device_properties, str) or not device_properties.strip():
        return (
            False,
            "xiaoyuzhou_device_properties_missing",
            (
                "Xiaoyuzhou device_properties is required; capture the real "
                "x-jike-device-properties value when possible, and ensure its "
                "uuid matches device_id."
            ),
        )

    try:
        parsed_properties = json.loads(device_properties)
    except json.JSONDecodeError:
        return (
            False,
            "xiaoyuzhou_device_properties_invalid_json",
            "Xiaoyuzhou device_properties must be a JSON string.",
        )

    if not isinstance(parsed_properties, dict):
        return (
            False,
            "xiaoyuzhou_device_properties_invalid_json",
            "Xiaoyuzhou device_properties must decode to a JSON object.",
        )

    property_keys = set(parsed_properties.keys())
    if XIAOYUZHOU_ANDROID_DEVICE_PROPERTY_KEYS <= property_keys:
        if parsed_properties.get("uuid") != device_id:
            return (
                False,
                "xiaoyuzhou_device_mismatch",
                "Xiaoyuzhou Android device_properties.uuid must match device_id.",
            )
        return True, "ok", "Xiaoyuzhou credential profile passed local shape checks."

    if XIAOYUZHOU_IOS_DEVICE_PROPERTY_KEYS <= property_keys:
        request_headers = credentials.get("request_headers") or credentials.get("headers")
        if not isinstance(request_headers, dict) or not request_headers:
            return (
                True,
                "ok",
                (
                    "Xiaoyuzhou credential profile passed local shape checks "
                    "for an OpenCLI-compatible iOS device profile."
                ),
            )

        missing_header_groups = [
            "/".join(group)
            for group in XIAOYUZHOU_IOS_REQUEST_HEADER_GROUPS
            if _header_lookup(request_headers, group) is None
        ]
        if missing_header_groups:
            return (
                False,
                "xiaoyuzhou_request_headers_incomplete",
                (
                    "Xiaoyuzhou iOS request_headers is missing fields: "
                    f"{', '.join(missing_header_groups)}."
                ),
            )

        header_device_id = _header_lookup(request_headers, ("x-jike-device-id",))
        if header_device_id is not None and header_device_id != device_id:
            return (
                False,
                "xiaoyuzhou_device_mismatch",
                "Xiaoyuzhou request_headers x-jike-device-id must match device_id.",
            )

        return True, "ok", "Xiaoyuzhou credential profile passed local shape checks."

    android_missing = sorted(XIAOYUZHOU_ANDROID_DEVICE_PROPERTY_KEYS - property_keys)
    ios_missing = sorted(XIAOYUZHOU_IOS_DEVICE_PROPERTY_KEYS - property_keys)
    if android_missing and ios_missing:
        return (
            False,
            "xiaoyuzhou_device_properties_incomplete",
            (
                "Xiaoyuzhou device_properties must look like either Android "
                f"({', '.join(sorted(XIAOYUZHOU_ANDROID_DEVICE_PROPERTY_KEYS))}) "
                f"or iOS ({', '.join(sorted(XIAOYUZHOU_IOS_DEVICE_PROPERTY_KEYS))})."
            ),
        )

    return True, "ok", "Xiaoyuzhou credential profile passed local shape checks."


class OpenCliPlatformAdapter(PlatformAdapter):
    """Minimal real adapter for smoke-proven read-only CLI backends."""

    def __init__(self, platform_id: str, default_backend: str) -> None:
        self.id = platform_id
        self.default_backend = default_backend

    def can_handle(self, input_value: str) -> bool:
        from openmd.core.detector import detect_platform

        return detect_platform(input_value) == self.id

    def resolve_items(
        self, input_value: str, *, limit: int | None = None, all: bool = False
    ) -> list[ContentItem]:
        input_type = "collection" if limit is not None or all else "single"
        return [
            ContentItem(
                platform=self.id,
                input_type=input_type,
                url=extract_first_url(input_value),
                source_type=_source_type_for(self.id),  # type: ignore[arg-type]
                raw_meta={"limit": limit},
            )
        ]

    def extract_text(
        self,
        item: ContentItem,
        *,
        backend: str | None = None,
        backend_chain: tuple[str, ...] | None = None,
        allow_fallback: bool = True,
    ) -> TranscriptResult:
        chain = backend_chain or (backend or self.default_backend,)
        if not allow_fallback:
            chain = chain[:1]
        selected_backend = chain[0] if chain else self.default_backend
        if selected_backend == "mock":
            return TranscriptResult(
                text="[00:00:00] Mock transcript for Openmd skeleton.\n",
                status="verified",
                source_level="third_party_extractor",
                backend="mock",
                source_notes=["local smoke check only"],
            )

        if not selected_backend.startswith("opencli.") and selected_backend != "podwise.transcript":
            return self._failed(
                selected_backend,
                "backend_not_implemented",
                "Primary backend is registered but not wired in the V0 skeleton.",
                notes=[f"backend_chain: {' -> '.join(chain)}"],
            )

        try:
            return self._extract_opencli_text(
                item,
                selected_backend,
                allow_fallback=allow_fallback,
            )
        except FileNotFoundError as exc:
            return self._failed(selected_backend, "backend_not_found", exc)
        except subprocess.TimeoutExpired as exc:
            return self._failed(selected_backend, "backend_timeout", exc)
        except OSError as exc:
            return self._failed(selected_backend, "backend_os_error", exc)

    def _extract_opencli_text(
        self,
        item: ContentItem,
        backend: str,
        *,
        allow_fallback: bool,
    ) -> TranscriptResult:
        if self.id == "bilibili":
            return self._bilibili(item, backend)
        if self.id == "youtube":
            return self._youtube(item, backend)
        if self.id == "weixin":
            return self._weixin(item, backend)
        if self.id == "x":
            return self._x(item, backend)
        if self.id == "xiaoyuzhou":
            return self._xiaoyuzhou(item, backend, allow_fallback=allow_fallback)
        if self.id == "xiaohongshu":
            return self._xiaohongshu(item, backend)
        if self.id == "xiaoe":
            return self._xiaoe(item, backend, allow_fallback=allow_fallback)
        return self._failed(
            backend,
            "backend_not_implemented",
            "OpenCLI adapter does not yet implement this platform.",
        )

    def _bilibili(self, item: ContentItem, backend: str) -> TranscriptResult:
        bvid = _extract_bvid(item.url)
        if not bvid:
            return self._failed(backend, "invalid_input", "Bilibili BVID not found.")
        exit_code, stdout, stderr = _run_opencli(["bilibili", "subtitle", bvid])
        return self._result_from_stdout(
            backend,
            exit_code,
            stdout,
            stderr,
            source_level="native_caption",
            notes=["OpenCLI bilibili subtitle output is preserved as backend text."],
        )

    def _youtube(self, item: ContentItem, backend: str) -> TranscriptResult:
        exit_code, stdout, stderr = _run_opencli(
            ["youtube", "transcript", item.url, "--mode", "grouped"]
        )
        return self._result_from_stdout(
            backend,
            exit_code,
            stdout,
            stderr,
            source_level="third_party_extractor",
            notes=["OpenCLI youtube transcript grouped output."],
        )

    def _weixin(self, item: ContentItem, backend: str) -> TranscriptResult:
        with tempfile.TemporaryDirectory(prefix="openmd-weixin-") as tmp_dir:
            exit_code, stdout, stderr = _run_opencli(
                [
                    "weixin",
                    "download",
                    "--url",
                    item.url,
                    "--output",
                    tmp_dir,
                    "--download-images",
                    "false",
                    "-f",
                    "json",
                ]
            )
            if exit_code != 0:
                return self._backend_failure(backend, exit_code, stderr)
            markdown_files = sorted(Path(tmp_dir).rglob("*.md"))
            if not markdown_files:
                return self._failed(
                    backend,
                    "empty_result",
                    "OpenCLI weixin download did not produce a Markdown file.",
                )
            text = markdown_files[0].read_text(encoding="utf-8", errors="replace")
            return TranscriptResult(
                text=text,
                status="verified" if text.strip() else "failed",
                source_level="native_text" if text.strip() else "partial_extraction",
                backend=backend,
                source_notes=["OpenCLI weixin download with --download-images false."],
                error_code=None if text.strip() else "empty_result",
                error_summary=None if text.strip() else "Downloaded Markdown was empty.",
            )

    def _xiaoe(
        self,
        item: ContentItem,
        backend: str,
        *,
        allow_fallback: bool,
    ) -> TranscriptResult:
        if backend == "opencli.xiaoe.content":
            return self._xiaoe_content(item, backend)

        result = self._xiaoe_transcript(item, backend)
        if result.has_usable_text or not allow_fallback:
            return result

        fallback = self._xiaoe_content(item, "opencli.xiaoe.content")
        if fallback.source_notes:
            fallback.source_notes.insert(
                0,
                "OpenCLI xiaoe transcript failed; content extraction fallback was attempted.",
            )
        return fallback

    def _xiaoe_transcript(self, item: ContentItem, backend: str) -> TranscriptResult:
        with tempfile.TemporaryDirectory(prefix="openmd-xiaoe-") as tmp_dir:
            exit_code, _stdout, stderr = _run_opencli(
                [
                    "xiaoe",
                    "transcript",
                    item.url,
                    "--out",
                    tmp_dir,
                    "--keep-artifacts",
                    "false",
                    "-f",
                    "json",
                ],
                timeout=60 * 60,
            )
            if exit_code != 0:
                return self._backend_failure(backend, exit_code, stderr)

            markdown_files = sorted(Path(tmp_dir).rglob("*.md"))
            if not markdown_files:
                return self._failed(
                    backend,
                    "empty_result",
                    "OpenCLI xiaoe transcript did not produce a Markdown file.",
                )

            text = markdown_files[0].read_text(encoding="utf-8", errors="replace")
            return TranscriptResult(
                text=text,
                status="verified" if text.strip() else "failed",
                source_level="media_asr" if text.strip() else "partial_extraction",
                backend=backend,
                source_notes=[
                    "OpenCLI xiaoe transcript with --keep-artifacts false.",
                    "Generated Markdown was read from a temporary output directory.",
                ],
                error_code=None if text.strip() else "empty_result",
                error_summary=None if text.strip() else "Generated Markdown was empty.",
            )

    def _xiaoe_content(self, item: ContentItem, backend: str) -> TranscriptResult:
        exit_code, stdout, stderr = _run_opencli(
            [
                "xiaoe",
                "content",
                item.url,
                "-f",
                "json",
                "--window",
                "background",
                "--site-session",
                "persistent",
                "--keep-tab",
                "false",
            ],
            timeout=300,
        )
        return self._result_from_stdout(
            backend,
            exit_code,
            stdout,
            stderr,
            source_level="native_text",
            notes=["OpenCLI xiaoe content extraction output."],
        )

    def _x(self, item: ContentItem, backend: str) -> TranscriptResult:
        post_id = _extract_x_post_id(item.url)
        if post_id:
            exit_code, stdout, stderr = _run_opencli(["twitter", "thread", post_id])
            notes = ["OpenCLI twitter namespace is used for X thread output."]
        else:
            username = _extract_x_username(item.url)
            if not username:
                return self._failed(backend, "invalid_input", "X post ID or username not found.")
            limit = str(item.raw_meta.get("limit") or 20)
            exit_code, stdout, stderr = _run_opencli(["twitter", "tweets", username, "--limit", limit])
            notes = ["OpenCLI twitter namespace is used for X user posts output."]
        return self._result_from_stdout(
            backend,
            exit_code,
            stdout,
            stderr,
            source_level="native_text",
            notes=notes,
        )

    def _xiaoyuzhou(
        self,
        item: ContentItem,
        backend: str,
        *,
        allow_fallback: bool,
    ) -> TranscriptResult:
        if backend == "podwise.transcript":
            return self._xiaoyuzhou_podwise(item)

        credentials_ok, credentials_code, credentials_summary = _check_xiaoyuzhou_credentials()
        if not credentials_ok:
            if allow_fallback:
                return self._xiaoyuzhou_podwise(
                    item,
                    extra_notes=[
                        credentials_summary,
                        "Skipped OpenCLI xiaoyuzhou transcript before making an API request.",
                    ],
                )
            return self._failed(backend, credentials_code, credentials_summary)

        episode_id = _extract_episode_id(item.url)
        with tempfile.TemporaryDirectory(prefix="openmd-xiaoyuzhou-") as tmp_dir:
            exit_code, stdout, stderr = _run_opencli(
                [
                    "xiaoyuzhou",
                    "transcript",
                    episode_id,
                    "--output",
                    tmp_dir,
                    "--json",
                    "false",
                    "--text",
                    "true",
                    "-f",
                    "json",
                ]
            )
            if exit_code != 0:
                if allow_fallback and _is_xiaoyuzhou_credentials_error(exit_code, stderr):
                    return self._xiaoyuzhou_podwise(
                        item,
                        extra_notes=[
                            "OpenCLI xiaoyuzhou transcript credentials were unavailable.",
                        ],
                    )
                return self._backend_failure(backend, exit_code, stderr)
            text_files = sorted(Path(tmp_dir).rglob("transcript.txt"))
            text = text_files[0].read_text(encoding="utf-8", errors="replace") if text_files else stdout
            return TranscriptResult(
                text=text,
                status="verified" if text.strip() else "failed",
                source_level="native_caption" if text.strip() else "partial_extraction",
                backend=backend,
                source_notes=["OpenCLI xiaoyuzhou transcript text output."],
                error_code=None if text.strip() else "empty_result",
                error_summary=None if text.strip() else "Xiaoyuzhou transcript output was empty.",
            )

    def _xiaoyuzhou_podwise(
        self,
        item: ContentItem,
        *,
        extra_notes: list[str] | None = None,
    ) -> TranscriptResult:
        podwise_url = _extract_podwise_episode_url(item.url)
        notes = list(extra_notes or [])
        if podwise_url is None:
            podwise_url_result = self._find_podwise_episode_url(item.url)
            if isinstance(podwise_url_result, TranscriptResult):
                return podwise_url_result
            podwise_url = podwise_url_result

        exit_code, stdout, stderr = _run_podwise(
            ["get", "transcript", podwise_url],
            timeout=180,
        )
        if exit_code != 0:
            if "not been processed yet" in stderr.lower():
                return self._failed(
                    "podwise.transcript",
                    "podwise_episode_not_processed",
                    (
                        "Podwise has not processed this episode yet. "
                        "Openmd does not run `podwise process` automatically because it consumes account credits."
                    ),
                )
            return self._backend_failure("podwise.transcript", exit_code, stderr)
        if not stdout.strip():
            return self._failed(
                "podwise.transcript",
                "empty_result",
                "Podwise transcript command returned empty stdout.",
            )
        notes.extend(
            [
                "Podwise transcript fallback was used for Xiaoyuzhou.",
                f"podwise_episode: {podwise_url}",
            ]
        )
        return TranscriptResult(
            text=stdout,
            status="verified",
            source_level="third_party_extractor",
            backend="podwise.transcript",
            source_notes=notes,
        )

    def _find_podwise_episode_url(self, xiaoyuzhou_url: str) -> str | TranscriptResult:
        with tempfile.TemporaryDirectory(prefix="openmd-xiaoyuzhou-web-") as tmp_dir:
            exit_code, stdout, stderr = _run_opencli(
                ["web", "read", "--url", xiaoyuzhou_url, "--stdout"],
                cwd=tmp_dir,
            )
            if exit_code != 0:
                return self._backend_failure("opencli.web.read", exit_code, stderr)
            title = (
                _extract_markdown_title(stdout)
                or _extract_table_title(stdout)
                or _extract_first_markdown_title(tmp_dir)
            )

        if not title:
            return self._failed(
                "podwise.transcript",
                "podwise_lookup_failed",
                "Could not derive a Podwise search title from the Xiaoyuzhou page.",
            )

        exit_code, stdout, stderr = _run_podwise(
            ["search", "episode", title, "--limit", "5"],
            timeout=120,
        )
        if exit_code != 0:
            return self._backend_failure("podwise.search", exit_code, stderr)
        podwise_url = _extract_podwise_episode_url(stdout)
        if not podwise_url:
            return self._failed(
                "podwise.transcript",
                "podwise_episode_not_found",
                f"Podwise search did not return an episode URL for title: {title}",
            )
        return podwise_url

    def _xiaohongshu(self, item: ContentItem, backend: str) -> TranscriptResult:
        parsed = urlparse(item.url)
        if "/user/profile/" in parsed.path:
            limit = str(item.raw_meta.get("limit") or 20)
            args = ["xiaohongshu", "user", item.url, "--limit", limit, "-f", "json"]
            notes = ["OpenCLI xiaohongshu user output with explicit limit."]
        else:
            args = ["xiaohongshu", "note", item.url, "-f", "json"]
            notes = ["OpenCLI xiaohongshu note output."]
        exit_code, stdout, stderr = _run_opencli(args)
        return self._result_from_stdout(
            backend,
            exit_code,
            stdout,
            stderr,
            source_level="native_text",
            notes=notes,
        )

    def _result_from_stdout(
        self,
        backend: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        *,
        source_level: str,
        notes: list[str],
    ) -> TranscriptResult:
        if exit_code != 0:
            return self._backend_failure(backend, exit_code, stderr)
        if not stdout.strip():
            return self._failed(backend, "empty_result", "OpenCLI command returned empty stdout.")
        return TranscriptResult(
            text=stdout,
            status="verified",
            source_level=source_level,  # type: ignore[arg-type]
            backend=backend,
            source_notes=notes,
        )

    def _backend_failure(self, backend: str, exit_code: int, stderr: str) -> TranscriptResult:
        return TranscriptResult(
            text="",
            status="failed",
            source_level="partial_extraction",
            backend=backend,
            source_notes=["backend command failed without persisting raw stderr."],
            error_code=f"backend_exit_{exit_code}",
            error_summary=sanitize_error_summary(stderr) or "OpenCLI command failed.",
        )

    def _failed(
        self,
        backend: str,
        code: str,
        error: object,
        *,
        notes: list[str] | None = None,
    ) -> TranscriptResult:
        return TranscriptResult(
            text="",
            status="failed",
            source_level="partial_extraction",
            backend=backend,
            source_notes=["adapter returned structured failure", *(notes or [])],
            error_code=code,
            error_summary=sanitize_error_summary(error),
        )
