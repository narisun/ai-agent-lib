"""Command-line entry point: ask the agent one question."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from agentcraft2_core import ServiceConfig, ServiceContainer
from agentcraft2_core.config import load_service_config
from agentcraft2_core.observability import report_error, shows_details

from rmprep.context import ContextBuilder
from rmprep.graph import APPLICATION, ask
from rmprep.settings import ENV_FILE, load_context_budget

__all__ = ["main"]


async def _run(
    config: ServiceConfig, question: str, thread_id: str, context_builder: ContextBuilder
) -> str:
    async with ServiceContainer(config) as services:
        await services.validate()
        # A deployed agent passes the bearer token it was sent. On a developer's
        # machine there is no token: the development identity stands in.
        context = await services.authenticate(None, application=APPLICATION, thread_id=thread_id)
        return await ask(services, context, question, context_builder=context_builder)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the agent once and print its answer."""
    parser = argparse.ArgumentParser(prog="rmprep", description=__doc__)
    parser.add_argument("question", help="the question to ask")
    parser.add_argument("--thread", default="default", help="conversation to continue")
    arguments = parser.parse_args(argv)
    config: ServiceConfig | None = None
    try:
        config = load_service_config(ENV_FILE)
        contexts = ContextBuilder(load_context_budget(ENV_FILE))
        reply = asyncio.run(_run(config, arguments.question, arguments.thread, contexts))
    except Exception as error:
        # What went wrong, what was expected, and the line of this code to look at.
        return report_error("rmprep", error, details=shows_details(config))
    sys.stdout.write(reply + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
