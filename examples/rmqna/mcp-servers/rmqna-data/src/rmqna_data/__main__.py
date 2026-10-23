"""Command-line entry point: serve the MCP server over Streamable HTTP."""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import Sequence

from ai_agent_lib_core import ServiceConfig, ServiceContainer
from ai_agent_lib_core.config import log_resolution_warnings, service_resolver
from ai_agent_lib_core.contracts import DeploymentEnv
from ai_agent_lib_core.integrations.http import ServiceLifecycle, add_health_routes, serve
from ai_agent_lib_core.integrations.mcp import verify_registration
from ai_agent_lib_core.observability import (
    configure_logging,
    report_error,
    shows_details,
    start_telemetry,
)

from rmqna_data.server import APPLICATION, SERVER_ID, build_server
from rmqna_data.settings import ENV_FILE

__all__ = ["main"]


async def _serve(config: ServiceConfig, host: str, port: int) -> None:
    async with ServiceContainer(config) as services:
        server = build_server(services)
        # Health and readiness answer without a token; a load balancer has none.
        lifecycle = ServiceLifecycle(services.validate)
        add_health_routes(server, lifecycle)
        entry = services.registry.tools.get(SERVER_ID)
        if entry is not None:
            # Fail at startup, not on the first call, if the tools and the registry disagree.
            local = services.config.deployment_env is DeploymentEnv.LOCAL
            await verify_registration(server, entry, require_pins=not local)
        app = server.streamable_http_app(stateless_http=True, json_response=True, host=host)
        # serve() runs services.validate() once it is listening, and on SIGTERM
        # lets the calls in flight finish before it returns.
        await serve(app, lifecycle, host=host, port=port)


def main(argv: Sequence[str] | None = None) -> int:
    """Serve until stopped. SIGTERM lets calls in flight finish first."""
    parser = argparse.ArgumentParser(prog="rmqna-data", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on")
    parser.add_argument("--port", type=int, default=8300, help="port to listen on")
    arguments = parser.parse_args(argv)
    config: ServiceConfig | None = None
    try:
        # explain() rather than resolve(): the same configuration, plus the deprecation
        # warnings, which the service's log shows once logging is set up.
        resolution = service_resolver(ENV_FILE).explain()
        config = resolution.config
        # One line of JSON per event on standard output, set up before the server
        # is built so that nothing else sets up logging first. On a developer's
        # machine a line about an error also says what the error said.
        configure_logging(APPLICATION, details=shows_details(config))
        # Loggers outside the library are quiet below WARNING. This server's one line per call
        # (counts, duration and error kind; never SQL) is INFO, so it asks to be heard.
        logging.getLogger("rmqna_data").setLevel(logging.INFO)
        log_resolution_warnings(resolution)
        # Traces and metrics go to the collector when the settings turn them on.
        stop_telemetry = start_telemetry(APPLICATION, config)
        try:
            asyncio.run(_serve(config, arguments.host, arguments.port))
        finally:
            stop_telemetry()  # the last spans and metrics are flushed
    except Exception as error:
        # What went wrong, what was expected, and the line of this code to look at.
        return report_error("rmqna-data", error, details=shows_details(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
