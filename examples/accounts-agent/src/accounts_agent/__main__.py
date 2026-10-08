"""Command-line entry point: ask the agent one question."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from accounts_agent.graph import APPLICATION, ask
from ai_agent_lib_core import ServiceConfig, ServiceContainer
from ai_agent_lib_core.config import load_service_config
from ai_agent_lib_core.observability import report_error, shows_details

__all__ = ["main"]


async def _run(config: ServiceConfig, question: str, thread_id: str) -> str:
    async with ServiceContainer(config) as services:
        await services.validate()
        # A deployed agent passes the bearer token it was sent, and the identity
        # provider's signature decides who the caller is. On a developer's
        # machine there is no token: the static identity stands in.
        context = await services.authenticate(None, application=APPLICATION, thread_id=thread_id)
        return await ask(services, context, question)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the agent once and print its answer."""
    parser = argparse.ArgumentParser(prog="accounts-agent", description=__doc__)
    parser.add_argument("question", help="the question to ask")
    parser.add_argument("--thread", default="default", help="conversation to continue")
    arguments = parser.parse_args(argv)
    config: ServiceConfig | None = None
    try:
        config = load_service_config()
        answer = asyncio.run(_run(config, arguments.question, arguments.thread))
    except Exception as error:  # noqa: BLE001 - every failure is explained, then the exit code
        return report_error("accounts-agent", error, details=shows_details(config))
    sys.stdout.write(answer + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
