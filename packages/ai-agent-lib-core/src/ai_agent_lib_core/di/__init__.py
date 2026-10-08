"""Dependency injection: the provider registry and the service container."""

from ai_agent_lib_core.di.access import AccessPlan, AdapterAccess, access_plan
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
from ai_agent_lib_core.di.sync import SyncServices

__all__ = [
    "DATA_PORT",
    "ENTRY_POINT_GROUP",
    "FIRST_PARTY_PACKS",
    "MODEL_PORT",
    "AccessPlan",
    "AdapterAccess",
    "BuildContext",
    "Factory",
    "ProviderSpec",
    "ServiceContainer",
    "ServiceProviders",
    "SyncServices",
    "access_plan",
    "diagnose",
    "fix_for",
]
