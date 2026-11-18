"""Markdown artifact output writer."""

from __future__ import annotations

from dataclasses import replace
import re
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from .filename import filename_base_for_item, safe_filename, unique_markdown_path
from .models import ContentItem, MarkdownDocument, TranscriptResult
from .renderer import render_document, render_markdown_document
from .secrets import sanitize_error_summary, sanitize_url_text


AssetFetcher = Callable[[str, dict[str, str], int], tuple[bytes, str | None]]

_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^\s)]+)\)")
_SAFE_EXTENSION_RE = re.compile(r"^[A-Za-z0-9]{2,5}$")




def write_item_result(
    item: ContentItem,
    result: TranscriptResult,
    out_dir: str | Path,
    *,
    overwrite: bool = False,
    captured_at: str | None = None,
    download_assets: bool = False,
    asset_fetcher: AssetFetcher | None = None,
    raw: bool = False,
    sync_receipt: dict | None = None,
) -> Path:
    document = render_markdown_document(item, result, captured_at=captured_at)
    platform_dir = Path(out_dir) / safe_filename(item.platform, fallback="unknown", max_length=80)
    filename_base = filename_base_for_item(document.title, item_id=item.id, url=item.url)
    from .sync_manifest import SyncManifest, is_syncable, source_id_for

    syncable = is_syncable(item)
    output_path = platform_dir / f"{filename_base}.md" if syncable else unique_markdown_path(
        platform_dir,
        filename_base,
        collision_key=item.url or item.id or document.title,
        overwrite=overwrite,
    )
    if download_assets:
        localized_content, asset_notes = localize_markdown_images(
            document.content,
            output_path.parent,
            fetcher=asset_fetcher,
        )
        document = replace(
            document,
            content=localized_content,
            source_notes=[*document.source_notes, *asset_notes],
        )
    content = render_document(document, raw=raw)
    if syncable:
        append = bool(result.metadata.get("sync_append"))
        identity = None
        if item.source_type == "conversation" and not append:
            import hashlib

            stable = render_document(render_markdown_document(item, result, captured_at="export-time"), raw=raw)
            identity = hashlib.sha256(stable.encode("utf-8")).hexdigest()
        manifest_source_id = result.metadata.get("source_id") or source_id_for(item)
        synced = SyncManifest(out_dir).write(
            str(manifest_source_id),
            output_path.relative_to(Path(out_dir)).as_posix(),
            content,
            append=append,
            cursor=result.metadata.get("cursor"),
            aliases=(source_id_for(item),),
            no_delta=bool(result.metadata.get("sync_no_delta")),
            content_identity=identity,
        )
        if sync_receipt is not None:
            sync_receipt["operation"] = synced.operation
            sync_receipt.pop("renamed_from", None)
            if synced.renamed_from is not None:
                sync_receipt["renamed_from"] = synced.renamed_from
        return synced.path
    output_path.write_text(content, encoding="utf-8", newline="\n")
    return output_path


def localize_markdown_images(
    markdown: str,
    article_dir: str | Path,
    *,
    fetcher: AssetFetcher | None = None,
    timeout: int = 30,
) -> tuple[str, list[str]]:
    """Download Markdown image URLs under ``assets/images`` and rewrite links.

    Asset failures are warnings, not extraction failures: the original remote
    URL stays in the Markdown so the raw article remains usable.
    """
    image_urls = _markdown_image_urls(markdown)
    if not image_urls:
        return markdown, ["asset_download: no Markdown image links found"]

    fetch = fetcher or _default_asset_fetcher
    image_dir = Path(article_dir) / "assets" / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    replacements: dict[str, str] = {}
    notes = [f"asset_download: attempted {len(image_urls)} image(s)"]
    success_count = 0

    for index, url in enumerate(image_urls, start=1):
        try:
            content, content_type = fetch(_absolute_url(url), _asset_headers(url), timeout)
            extension = _image_extension(url, content_type)
            filename = f"img_{index:03d}.{extension}"
            output_path = image_dir / filename
            output_path.write_bytes(content)
            replacements[url] = f"assets/images/{filename}"
            success_count += 1
        except Exception as exc:  # pragma: no cover - safety net covered via injected fetchers
            summary = sanitize_error_summary(exc) or "unknown error"
            notes.append(f"asset_download_failed: {sanitize_url_text(url)} ({summary})")

    def replace_image(match: re.Match[str]) -> str:
        alt_text = match.group(1)
        remote_url = match.group(2).strip().strip("<>")
        local_path = replacements.get(remote_url)
        if local_path is None:
            return match.group(0)
        return f"![{alt_text}]({local_path})"

    markdown = _MARKDOWN_IMAGE_RE.sub(replace_image, markdown)

    notes.append(f"asset_download: downloaded {success_count}/{len(image_urls)} image(s)")
    return markdown, notes


def _markdown_image_urls(markdown: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _MARKDOWN_IMAGE_RE.finditer(markdown):
        url = match.group(2).strip().strip("<>")
        if not _is_remote_url(url) or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _is_remote_url(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://") or url.startswith("//")


def _absolute_url(url: str) -> str:
    if url.startswith("//"):
        return f"https:{url}"
    return url


def _asset_headers(url: str) -> dict[str, str]:
    parsed = urlparse(_absolute_url(url))
    headers = {"User-Agent": "Mozilla/5.0 Openmd/0.0"}
    if parsed.netloc.endswith("qpic.cn") or parsed.netloc.endswith("qq.com"):
        headers["Referer"] = "https://mp.weixin.qq.com/"
    return headers


def _default_asset_fetcher(
    url: str,
    headers: dict[str, str],
    timeout: int,
) -> tuple[bytes, str | None]:
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        return response.read(), response.headers.get("Content-Type")


def _image_extension(url: str, content_type: str | None) -> str:
    parsed = urlparse(_absolute_url(url))
    wx_fmt = parse_qs(parsed.query).get("wx_fmt")
    if wx_fmt:
        return _safe_extension(wx_fmt[0])
    suffix = Path(parsed.path).suffix.lstrip(".")
    if suffix:
        return _safe_extension(suffix)
    if content_type and "/" in content_type:
        subtype = content_type.split("/", 1)[1].split(";", 1)[0].strip()
        if subtype == "jpeg":
            return "jpg"
        return _safe_extension(subtype)
    return "png"


def _safe_extension(value: str) -> str:
    extension = value.lower().strip().lstrip(".")
    if extension == "jpeg":
        extension = "jpg"
    if not _SAFE_EXTENSION_RE.match(extension):
        return "png"
    return extension
