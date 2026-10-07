"""AWS adapters for the Enterprise Agentic Platform library.

Installing this package is all a service needs to do to use them: core loads
this provider pack whenever it is installed, and configuration then selects an
adapter by name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ai_agent_lib_core.kit import ServiceProviders

__all__ = ["register"]


def register(providers: ServiceProviders) -> None:
    """Add the AWS adapters to a provider registry.

    Core calls this through the ``ai_agent_lib.providers`` entry point. The
    adapters are imported only now, so importing this package costs nothing.
    """
    from ai_agent_lib_aws.pack import register_aws_adapters

    register_aws_adapters(providers)
