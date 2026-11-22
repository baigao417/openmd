"""Optional provider registrations for generic web articles."""

from openmd.backends.registry import BackendRuntimeOptions
from openmd.backends.executor import BackendProviderRegistry


def register_backend_providers(registry: BackendProviderRegistry, options: BackendRuntimeOptions) -> None:
    from openmd.backends.kaze_snapshot import register_backend_providers as register_kaze

    register_kaze(registry, options)
