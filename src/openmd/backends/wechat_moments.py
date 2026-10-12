"""Runtime registration for WeChat Moments discovery providers."""

from __future__ import annotations

from openmd.adapters.wechat_moments.wx_cli import (
    OPENCLI_WX_SNS_FEED_DISCOVERY_BACKEND,
    OpenCliWechatMomentsProvider,
)
from openmd.backends.executor import BackendProviderRegistry, DiscoveryProviderRegistry
from openmd.backends.registry import BackendRuntimeOptions


def register_backend_providers(
    registry: BackendProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """WeChat Moments extraction is intentionally not registered in mock/gated V1."""
    return None


def register_discovery_providers(
    registry: DiscoveryProviderRegistry,
    options: BackendRuntimeOptions,
) -> None:
    """Register OpenCLI Moments discovery only behind explicit browser gate."""
    if not options.enable_browser_backends:
        return
    requested = options.requested_discovery_provider
    if requested in {None, OPENCLI_WX_SNS_FEED_DISCOVERY_BACKEND}:
        registry.register(
            OPENCLI_WX_SNS_FEED_DISCOVERY_BACKEND,
            OpenCliWechatMomentsProvider(),
            platforms=("wechat_moments",),
            default_for_platforms=requested is None,
        )
