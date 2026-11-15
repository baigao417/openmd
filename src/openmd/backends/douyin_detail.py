"""Bounded, transient Douyin single-work detail acquisition."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from openmd.backends.youtube_discovery import _run_opencli
from openmd.core.detector import extract_first_url
from openmd.core.models import ContentItem
from openmd.core.opencli_output import load_opencli_json


class DouyinDetailError(ValueError):
    def __init__(self, code: str, summary: str):
        super().__init__(summary)
        self.code = code


def douyin_author(payload: dict) -> str | None:
    """Read nickname-shaped fields without stringifying API objects or IDs."""
    fields = {str(key).lower().replace("_", ""): value for key, value in payload.items()}
    for key in ("author", "nickname", "authorname", "user", "username", "userinfo"):
        value = fields.get(key)
        if isinstance(value, dict):
            value = douyin_author(value) or value.get("name")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def douyin_published_at(payload: dict) -> str | None:
    """Keep source calendar times; normalize Unix seconds/milliseconds in UTC."""
    fields = {str(key).lower().replace("_", ""): value for key, value in payload.items()}
    for key in ("publishedat", "createtime", "created", "createdat", "publishtime", "date"):
        value = fields.get(key)
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            continue
        text = str(value).strip()
        if not text:
            continue
        try:
            if re.fullmatch(r"\d{8}", text):
                return datetime.strptime(text, "%Y%m%d").date().isoformat()
            if re.fullmatch(r"\d{10}(?:\.\d+)?|\d{13}(?:\.\d+)?", text):
                seconds = float(text) / (1000 if len(text.split(".")[0]) == 13 else 1)
                return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
            datetime.fromisoformat(text.replace("Z", "+00:00"))
            return text
        except (ValueError, OverflowError, OSError):
            continue
    return None


def douyin_video_id(value: str) -> str | None:
    target = extract_first_url(value)
    if re.fullmatch(r"\d{6,20}", target):
        return target
    parsed = urlparse(target if "://" in target else "https://" + target)
    host = (parsed.hostname or "").lower()
    if host != "douyin.com" and not host.endswith(".douyin.com") and host != "iesdouyin.com" and not host.endswith(".iesdouyin.com"):
        return None
    for key in ("modal_id", "aweme_id", "item_id"):
        candidate = parse_qs(parsed.query).get(key, [""])[0]
        if re.fullmatch(r"\d{6,20}", candidate):
            return candidate
    match = re.search(r"/(?:video|note)/(\d{6,20})(?:/|$)", parsed.path)
    return match.group(1) if match else None


class DouyinDetailProvider:
    name = "opencli.douyin.detail"

    def __init__(self, *, runner=None, timeout: int = 90):
        self.runner = runner or _run_opencli
        self.timeout = timeout

    def resolve(self, item: ContentItem) -> ContentItem:
        target = extract_first_url(item.url)
        expected = douyin_video_id(target) or (item.id if item.id and item.id.isdigit() else None)
        parsed = urlparse(target)
        if not expected and (parsed.hostname or "").lower() not in {"v.douyin.com", "www.iesdouyin.com"}:
            raise DouyinDetailError("douyin_invalid_video_target", "A video ID, video URL, modal URL, or video share link is required.")
        command = ["opencli", "douyin", "detail", expected or target, "--window", "background", "--keep-tab", "false", "-f", "json"]
        try:
            code, stdout, _stderr = self.runner(command, timeout=self.timeout)
        except Exception as exc:
            raise DouyinDetailError("douyin_detail_read_failed", "The bounded video detail command could not complete.") from exc
        if code:
            raise DouyinDetailError("douyin_detail_read_failed", "The bounded video detail command failed; check the browser session or access permissions.")
        try:
            payload = load_opencli_json(stdout)
            if isinstance(payload, list) and len(payload) == 1:
                payload = payload[0]
            if not isinstance(payload, dict) or payload.get("ok") is False:
                raise ValueError("invalid detail")
            video_id = str(payload.get("aweme_id") or payload.get("id") or "")
            if not re.fullmatch(r"\d{6,20}", video_id):
                raise ValueError("missing identity")
        except (ValueError, TypeError) as exc:
            raise DouyinDetailError("douyin_detail_invalid", "Video detail returned no valid single-work identity.") from exc
        if expected and video_id != expected:
            raise DouyinDetailError("douyin_detail_id_mismatch", "Video detail did not match the requested work ID.")
        images = payload.get("images")
        is_image = bool(images) or payload.get("media_kind") == "image_text" or payload.get("aweme_type") in {2, 68, 150}
        raw_meta = {**item.raw_meta, "detail_resolved": True, "identity_unresolved": False, "content_id": video_id}
        raw_meta.update({"duration": payload.get("duration_s", payload.get("duration", 0)), "media_kind": "image_text" if is_image else "video"})
        if is_image:
            # Image-post descriptions are author text, distinct from video
            # captions. Keep an empty value too: a missing description must not
            # fall back to an ID or a separately supplied display title.
            raw_meta["author_text"] = payload.get("desc") if isinstance(payload.get("desc"), str) else ""
            raw_meta["author_text_source"] = "detail_desc"
        for key in ("play_url", "transcript_candidates", "subtitles", "caption_info", "captionInfo"):
            if payload.get(key):
                raw_meta[key] = payload[key]
        # A video's description remains title metadata, never a transcript.
        return replace(item, url=f"https://www.douyin.com/video/{video_id}", id=video_id,
                       title=item.title if item.title and item.title != video_id else str(payload.get("desc") or payload.get("title") or video_id),
                       author=item.author or douyin_author(payload),
                       published_at=item.published_at or douyin_published_at(payload),
                       source_type="image_text" if is_image else "video", raw_meta=raw_meta)
