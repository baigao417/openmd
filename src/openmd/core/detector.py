"""Conservative URL platform detector for Openmd V0 categories."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote, urlparse

from openmd.core import plugins


PLATFORM_IDS: tuple[str, ...] = plugins.BUILTIN_PLATFORM_IDS + plugins.platform_ids()

MEDIA_EXTENSIONS: tuple[str, ...] = (
    ".mp4",
    ".m4v",
    ".mov",
    ".webm",
    ".mkv",
    ".mp3",
    ".m4a",
    ".wav",
    ".flac",
    ".m3u8",
)

# Keep the legacy `pdf` platform identity for the approved PDF/Office lane.
DOCUMENT_EXTENSIONS: tuple[str, ...] = (".pdf", ".docx", ".pptx", ".xlsx")
LOCAL_DOCUMENT_EXTENSIONS: tuple[str, ...] = (
    ".html", ".htm", ".epub", ".rtf", ".odt", ".odp", ".ods",
    ".doc", ".xls", ".ppt", ".csv", ".tsv", ".json", ".xml", ".ipynb", ".msg",
    ".mobi", ".azw3", ".fb2", ".zip",
)
DOCUMENT_EXTENSIONS += LOCAL_DOCUMENT_EXTENSIONS

IMAGE_TEXT_EXTENSIONS: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
)

QQ_EXPORT_EXTENSIONS: tuple[str, ...] = (".json", ".html", ".txt")

_BV_RE = re.compile(r"^BV[0-9A-Za-z]+$")
_URL_RE = re.compile(r"https?://[^\s，。]+")

_AI_CHAT_HOSTS = {
    "chatgpt.com": "chatgpt",
    "chat.openai.com": "chatgpt",
    "claude.ai": "claude",
    "chat.deepseek.com": "deepseek",
    "www.doubao.com": "doubao",
    "doubao.com": "doubao",
    "gemini.google.com": "gemini",
    "qianwen.aliyun.com": "qwen",
    "www.qianwen.com": "qwen",
    "tongyi.aliyun.com": "qwen",
    "yuanbao.tencent.com": "yuanbao",
    "grok.com": "grok",
    "x.com": "grok",
}


def extract_first_url(input_value: str) -> str:
    value = input_value.strip()
    match = _URL_RE.search(value)
    if not match:
        return value
    return match.group(0).rstrip(".,，。；;！!)）]")


def _host_and_path(input_value: str) -> tuple[str, str]:
    value = extract_first_url(input_value)
    parse_value = value if "://" in value else f"https://{value}"
    parsed = urlparse(parse_value)
    return parsed.netloc.lower(), parsed.path.lower()


def _host_endswith(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes)


def looks_like_media_url(input_value: str) -> bool:
    _, path = _host_and_path(input_value)
    return path.endswith(MEDIA_EXTENSIONS)


def _path_for_extension_check(input_value: str) -> str:
    value = extract_first_url(input_value)
    parsed = urlparse(value)
    if parsed.scheme == "file":
        return parsed.path.lower()
    if parsed.scheme in {"http", "https"}:
        return parsed.path.lower()
    return value.lower()


def detect_platform(input_value: str, *, explicit_media: bool = False) -> str:
    value = input_value.strip()
    if explicit_media:
        return "generic_media"
    directory_platform = _local_directory_platform(value)
    if directory_platform is not None:
        return directory_platform
    if _is_weixin_official_account_selector(value):
        return "weixin"
    plugin_platform = plugins.detect_platform(value)
    if plugin_platform is not None:
        return plugin_platform
    if _is_qq_group_selector(value) or _looks_like_qq_export_path(value):
        return "qq_group"
    if _BV_RE.match(value):
        return "bilibili"
    if re.fullmatch(r"\d{15,20}", value):
        return "douyin"

    extension_path = _path_for_extension_check(value)
    if extension_path.endswith(DOCUMENT_EXTENSIONS) and (
        not extension_path.endswith(LOCAL_DOCUMENT_EXTENSIONS) or _local_path_for_detection(value) is not None
    ):
        return "pdf"
    if extension_path.endswith(IMAGE_TEXT_EXTENSIONS):
        return "screenshot"
    if extension_path.endswith(MEDIA_EXTENSIONS):
        return "generic_media"

    host, path = _host_and_path(value)
    if _is_ai_chat_url(host, path):
        return "ai_chat"
    if _host_endswith(host, ("bilibili.com", "b23.tv")):
        return "bilibili"
    if _host_endswith(host, ("youtube.com", "youtu.be", "youtube-nocookie.com")):
        return "youtube"
    if host == "mp.weixin.qq.com":
        return "weixin"
    if _host_endswith(host, ("xiaoyuzhoufm.com",)):
        return "xiaoyuzhou"
    if _host_endswith(host, ("douyin.com", "iesdouyin.com")):
        return "douyin"
    if _host_endswith(host, ("tiktok.com",)):
        return "tiktok"
    if _host_endswith(host, ("xiaohongshu.com", "xhslink.com")):
        return "xiaohongshu"
    if _host_endswith(host, ("xet.citv.cn", "xet.pomoho.com", "xiaoeknow.com")) or "xiaoe" in host:
        return "xiaoe"
    if _host_endswith(host, ("x.com", "twitter.com")):
        return "x"
    if _host_endswith(host, ("feishu.cn", "larksuite.com")):
        return "feishu"
    if path.endswith(MEDIA_EXTENSIONS):
        return "generic_media"
    return "web_article"


def _is_ai_chat_url(host: str, path: str) -> bool:
    site = _AI_CHAT_HOSTS.get(host)
    if site == "chatgpt":
        return bool(re.search(r"/(?:c|share)/[^/]+/?$", path))
    if site == "claude":
        return bool(re.search(r"/chat/[^/]+/?$", path))
    if site == "deepseek":
        return bool(re.search(r"/a/chat/s/[^/]+/?$", path))
    if site == "doubao":
        return bool(re.search(r"/(?:chat|conversation)/[^/]+/?$", path))
    if site == "gemini":
        return bool(re.search(r"/(?:app|u/\d+/app)/[^/]+", path))
    if site == "qwen":
        return bool(re.search(r"/chat/[^/]+/?$", path))
    if site == "yuanbao":
        return bool(re.search(r"/chat/[^/]+/[^/]+/?$", path))
    if site == "grok":
        return bool(re.search(r"/(?:c|chat)/[^/]+", path))
    return False


def ai_chat_site(value: str) -> str | None:
    """Return the OpenCLI site id for a supported conversation URL."""
    host, path = _host_and_path(value)
    if not _is_ai_chat_url(host, path):
        return None
    return _AI_CHAT_HOSTS.get(host)


def _local_directory_platform(value: str) -> str | None:
    path = _local_path_for_detection(value)
    if path is None or not path.is_dir():
        return None

    seen_pdf = False
    seen_image = False
    seen_media = False
    checked = 0
    try:
        for candidate in path.rglob("*"):
            if not candidate.is_file():
                continue
            suffix = candidate.suffix.lower()
            seen_pdf = seen_pdf or (suffix in DOCUMENT_EXTENSIONS and not _looks_like_qq_export_path(str(candidate)))
            seen_image = seen_image or suffix in IMAGE_TEXT_EXTENSIONS
            seen_media = seen_media or suffix in MEDIA_EXTENSIONS
            checked += 1
            if checked >= 200 or seen_pdf:
                break
    except OSError:
        return None

    if seen_pdf:
        return "pdf"
    if seen_image:
        return "screenshot"
    if seen_media:
        return "generic_media"
    return None


def _local_path_for_detection(value: str) -> Path | None:
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        return None
    if len(parsed.scheme) == 1 and len(value) >= 3 and value[1] == ":" and value[2] in {"\\", "/"}:
        return Path(value)
    if parsed.scheme == "file":
        raw_path = unquote(parsed.path)
        if parsed.netloc and not raw_path:
            raw_path = parsed.netloc
        if raw_path.startswith("/") and len(raw_path) >= 4 and raw_path[2] == ":":
            raw_path = raw_path[1:]
        return Path(raw_path)
    if parsed.scheme:
        return None
    return Path(value)




def _is_weixin_official_account_selector(value: str) -> bool:
    parsed = urlparse(value.strip())
    if parsed.scheme in {"weixin", "wx-official", "wechat-official"}:
        return bool(parsed.netloc or parsed.path.strip("/"))
    if parsed.scheme == "wechat" and parsed.netloc.lower() in {
        "official",
        "official-account",
        "oa",
        "author",
        "account",
        "fakeid",
        "biz",
    }:
        return bool(parsed.path.strip("/") or parsed.query.strip())
    return False




def _is_qq_group_selector(value: str) -> bool:
    parsed = urlparse(value.strip())
    if parsed.scheme == "qq" and parsed.netloc.lower() == "group":
        return bool(parsed.path.strip("/"))
    if parsed.scheme in {"qq-group", "qq_export", "qq-export"}:
        return bool(parsed.netloc or parsed.path.strip("/"))
    return False


def _looks_like_qq_export_path(value: str) -> bool:
    extension_path = _path_for_extension_check(value)
    if not extension_path.endswith(QQ_EXPORT_EXTENSIONS):
        return False
    filename = extension_path.replace("\\", "/").rsplit("/", 1)[-1]
    return "qq" in filename or "group-chat" in filename
