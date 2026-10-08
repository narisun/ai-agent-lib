"""What an adapter needs from the cloud it runs in.

A deployment grants a service only what its adapters need. Each adapter says
what that is, from its own options, so the permissions are derived from the
configuration the service runs with and never written by hand.

Resources are written with two placeholders, :data:`REGION` and
:data:`ACCOUNT`, that the deployment fills in. Nothing here names a cloud
provider's SDK: an action is a plain string such as ``"firehose:PutRecord"``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ai_agent_lib_core.contracts.options import ProviderSelection

__all__ = [
    "ACCOUNT",
    "NO_ACCESS",
    "REGION",
    "Access",
    "AccessQuery",
    "AccessRule",
    "no_access",
]

REGION = "{region}"
"""Placeholder for the region the service runs in."""

ACCOUNT = "{account}"
"""Placeholder for the account the service runs in."""


@dataclass(frozen=True, slots=True)
class Access:
    """One permission an adapter needs.

    Attributes:
        actions: The actions it calls, such as ``"firehose:PutRecord"``.
        resources: What it calls them on, narrowed as far as its options allow.
        why: One line a reviewer reads to see why the permission is there.
        note: What the deployment should check by hand, when a resource could
            not be narrowed from the options alone. Empty otherwise.
    """

    actions: tuple[str, ...]
    resources: tuple[str, ...]
    why: str
    note: str = ""

    def __post_init__(self) -> None:
        if not self.actions or not self.resources:
            raise ValueError("an access needs at least one action and one resource")


@dataclass(frozen=True, slots=True)
class AccessQuery:
    """What an adapter is told when asked what it needs.

    Attributes:
        selection: The adapter's own name and options, as the service runs with them.
        model_ids: For a model provider, every model the service uses through it.
        instance: For a data source, its name. Empty otherwise.
    """

    selection: ProviderSelection
    model_ids: tuple[str, ...] = field(default=())
    instance: str = ""


AccessRule = Callable[[AccessQuery], Sequence[Access]]
"""Returns what one adapter needs, given how it is configured."""


def no_access(query: AccessQuery) -> Sequence[Access]:  # noqa: ARG001 - an AccessRule
    """The rule of an adapter that needs nothing from the cloud."""
    return ()


NO_ACCESS: AccessRule = no_access
"""The rule of an adapter that needs nothing from the cloud."""
