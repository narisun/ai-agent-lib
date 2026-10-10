"""Select implementations at the composition boundary and manage their resources.

``ServiceProviders`` holds factory definitions; ``ServiceContainer`` owns the
built adapters. ``BuildContext`` supplies resolved settings and declared
dependencies to factories. Typed port constants such as ``AUDIT`` preserve
types at registration and lookup without changing configuration-facing names.
"""

from ai_agent_lib_core.di.access import AccessPlan, AdapterAccess, access_plan
from ai_agent_lib_core.di.container import ServiceContainer, fix_for
from ai_agent_lib_core.di.doctor import diagnose
from ai_agent_lib_core.di.ports import (
    AUDIT,
    CHECKPOINT,
    DATA,
    GUARDRAILS,
    IDENTITY,
    MODEL,
    POLICY,
    REGISTRY,
    SECRETS,
)
from ai_agent_lib_core.di.providers import (
    DATA_PORT,
    ENTRY_POINT_GROUP,
    FIRST_PARTY_PACKS,
    MODEL_PORT,
    BuildContext,
    BuildResources,
    Factory,
    ProviderSpec,
    ServicePort,
    ServiceProviders,
)
from ai_agent_lib_core.di.sync import SyncServices

__all__ = [
    "AUDIT",
    "CHECKPOINT",
    "DATA",
    "DATA_PORT",
    "ENTRY_POINT_GROUP",
    "FIRST_PARTY_PACKS",
    "GUARDRAILS",
    "IDENTITY",
    "MODEL",
    "MODEL_PORT",
    "POLICY",
    "REGISTRY",
    "SECRETS",
    "AccessPlan",
    "AdapterAccess",
    "BuildContext",
    "BuildResources",
    "Factory",
    "ProviderSpec",
    "ServiceContainer",
    "ServicePort",
    "ServiceProviders",
    "SyncServices",
    "access_plan",
    "diagnose",
    "fix_for",
]
