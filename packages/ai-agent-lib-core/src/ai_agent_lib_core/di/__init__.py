"""Dependency injection: the provider registry and the service container."""

from ai_agent_lib_core.di.container import ServiceContainer, fix_for
from ai_agent_lib_core.di.doctor import diagnose
from ai_agent_lib_core.di.providers import (
    DATA_PORT,
    ENTRY_POINT_GROUP,
    FIRST_PARTY_PACKS,
    MODEL_PORT,
    BuildContext,
    Factory,
    ProviderSpec,
    ServiceProviders,
)

__all__ = [
    "DATA_PORT",
    "ENTRY_POINT_GROUP",
    "FIRST_PARTY_PACKS",
    "MODEL_PORT",
    "BuildContext",
    "Factory",
    "ProviderSpec",
    "ServiceContainer",
    "ServiceProviders",
    "diagnose",
    "fix_for",
]
