"""Runtime registration for WeChat group discovery providers."""

from __future__ import annotations

from openmd.adapters.wechat_group.wx_cli import (
    OPENCLI_WX_HISTORY_DISCOVERY_BACKEND,
    OpenCliWechatGroupHistoryProvider,
)
from openmd.backends.executor import BackendProviderRegistry, DiscoveryProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """WeChat group extraction is intentionally not registered in mock/gated V1."""
    return None


def register_discovery_providers(
    registry: DiscoveryProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register OpenCLI WeChat group history only behind explicit browser gate."""
    if not options.enable_browser_backends:
        return
    requested = options.requested_discovery_provider
    if requested in {None, OPENCLI_WX_HISTORY_DISCOVERY_BACKEND}:
        registry.register(
            OPENCLI_WX_HISTORY_DISCOVERY_BACKEND,
            OpenCliWechatGroupHistoryProvider(),
            platforms=("wechat_group",),
            default_for_platforms=requested is None,
        )
