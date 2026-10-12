"""Read-only safety envelope for an optional Douyin local sidecar."""

from __future__ import annotations

from urllib.parse import urljoin


ALLOWED_SIDECAR_ENDPOINTS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/monitors"),
        ("GET", "/api/v1/contents"),
        ("GET", "/api/v1/contents/:id"),
        ("GET", "/api/v1/contents/:id/comments"),
    }
)

FORBIDDEN_SIDECAR_PATH_PREFIXES: tuple[str, ...] = (
    "/auth/",
    "/api/v1/accounts",
    "/api/v1/admin",
    "/api/v1/creator",
)

FORBIDDEN_SIDECAR_PATH_PARTS: tuple[str, ...] = (
    "/upload",
    "/cancel-upload",
    "/retry-download",
    "/publish",
    "/draft",
    "/delete",
    "/reply",
    "/like",
    "/approve",
)


def is_allowed_sidecar_endpoint(method: str, path: str) -> bool:
    normalized_method = method.upper().strip()
    normalized_path = _normalize_path(path)
    if normalized_method != "GET":
        return False
    if any(normalized_path.startswith(prefix) for prefix in FORBIDDEN_SIDECAR_PATH_PREFIXES):
        return False
    if any(part in normalized_path for part in FORBIDDEN_SIDECAR_PATH_PARTS):
        return False
    if (normalized_method, normalized_path) in ALLOWED_SIDECAR_ENDPOINTS:
        return True
    return _is_content_detail_path(normalized_method, normalized_path) or _is_content_comments_path(
        normalized_method, normalized_path
    )


def build_sidecar_url(base_url: str, path: str) -> str:
    normalized_base = base_url.rstrip("/") + "/"
    normalized_path = _normalize_path(path).lstrip("/")
    return urljoin(normalized_base, normalized_path)


def _is_content_detail_path(method: str, path: str) -> bool:
    if method != "GET":
        return False
    parts = [part for part in path.split("/") if part]
    return len(parts) == 4 and parts[:3] == ["api", "v1", "contents"] and parts[3].isdigit()


def _is_content_comments_path(method: str, path: str) -> bool:
    if method != "GET":
        return False
    parts = [part for part in path.split("/") if part]
    return len(parts) == 5 and parts[:3] == ["api", "v1", "contents"] and parts[3].isdigit() and parts[4] == "comments"


def _normalize_path(path: str) -> str:
    value = "/" + path.strip().split("?", 1)[0].lstrip("/")
    while "//" in value:
        value = value.replace("//", "/")
    return value
