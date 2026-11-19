"""Compatibility adapter for the single audited OpenCLI patch registry."""
from __future__ import annotations

from .opencli_patches import loader_environment


def detail_loading_environment(site: str, operation: str = "detail") -> dict[str, str]:
    return loader_environment(site, operation)
