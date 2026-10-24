"""Initialize, validate and serve the optional local agent frontend."""

import argparse
import asyncio
import json
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from agentcraft2_ui.config.home import config_file

if TYPE_CHECKING:
    from agentcraft2_ui.contracts.catalog import AgentCoreRuntime


def arguments() -> argparse.Namespace:
    """Parse commands without importing optional runtime dependencies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="also print the error's type and traceback to stderr when a command fails",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "check", "serve"):
        command = commands.add_parser(name)
        command.add_argument(
            "--config",
            type=Path,
            default=None,
            help="the Agent Lab config file (default: ui/config.json in the state folder)",
        )
        if name == "init":
            command.add_argument("--env-file", type=Path, required=True)
            command.add_argument(
                "--agent",
                action="append",
                required=True,
                help="registry ID supporting question-answer/v1; repeat for more agents",
            )
            command.add_argument(
                "--allow-url",
                action="append",
                default=None,
                help="exact loopback registry URL; repeat for more endpoints",
            )
            command.add_argument(
                "--agentcore-runtime",
                action="append",
                default=None,
                metavar="AGENT=ARN",
                help="reach a selected agent through AgentCore Runtime at this ARN; repeatable",
            )
            command.add_argument(
                "--agentcore-auth",
                choices=("iam", "bearer"),
                default="iam",
                help="how every runtime request is authenticated: SigV4 or a bearer token file",
            )
            command.add_argument(
                "--agentcore-token-file",
                type=Path,
                default=None,
                help="file whose first line is the bearer token; needed for bearer auth",
            )
            command.add_argument("--port", type=int, default=8765)
            command.add_argument(
                "--artifacts-agent",
                action="append",
                default=None,
                help="selected agent that returns versioned inline documents; repeat as needed",
            )
            command.add_argument(
                "--overwrite",
                action="store_true",
                help="replace configuration pins; keep conversations and secrets",
            )
    return parser.parse_args()


def main() -> int:
    """Initialize or run a configured local server with safe command-line errors."""
    args = arguments()
    try:
        from agentcraft2_ui.adapters.catalog import (
            FileCatalog,
            LocalAccess,
            pin_bindings,
            read_registries,
        )
        from agentcraft2_ui.config.settings import Settings, agent_environment
        from agentcraft2_ui.config.shell import prepare_shell
        from agentcraft2_ui.contracts.queries import PageRequest
        from agentcraft2_ui.integrations.runtime import compose

        path = config_file(args.config)
        if args.command == "init":
            if path.exists() and not args.overwrite:
                raise ValueError(
                    "config already exists; use --overwrite to explicitly refresh registry pins"
                )
            agents_path, tools_path, identity = agent_environment(args.env_file.resolve())
            agents, _ = read_registries(agents_path, tools_path)
            runtimes = _runtimes(args)
            # Pinning an ARN is the explicit approval of its derived service URL.
            allowed = frozenset(args.allow_url or ()) | {
                runtime.invocation_url for runtime in runtimes.values()
            }
            if not allowed:
                raise ValueError("give --allow-url for each agent URL or --agentcore-runtime")
            artifact_agents = args.artifacts_agent or []
            if not set(artifact_agents) <= set(args.agent):
                raise ValueError("--artifacts-agent must also be selected with --agent")
            settings = Settings(
                agents_path=agents_path,
                tools_path=tools_path,
                identity=identity,
                bindings=pin_bindings(
                    agents,
                    tuple(args.agent),
                    allowed,
                    artifact_agent_ids=frozenset(artifact_agents),
                    runtimes=runtimes,
                ),
                allowed_urls=allowed,
                port=args.port,
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(settings.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8"
            )
            prepare_shell(path.parent, settings)
            print(f"Created {path}\nRead the local access code from {path.parent / 'access-code'}")
            print(
                "Confirm that every selected agent uses the same static tenant, subject and roles."
            )
            return 0
        settings = Settings.read(path)
        catalog = FileCatalog(settings, LocalAccess(settings))
        asyncio.run(catalog.agents(settings.identity.principal(), PageRequest(limit=200)))
        if args.command == "check":
            print(f"Configuration valid: {len(settings.bindings)} agent(s), {settings.origin}")
            print("Registry pins and local settings match. Start the agent services separately.")
            return 0
        import uvicorn
        from filelock import FileLock, Timeout

        try:
            with FileLock(str(path.parent / "server.lock"), timeout=0):
                resources = compose(settings, path.parent)
                from agentcraft2_ui.integrations.server import create_app

                app = create_app(resources)
                print(f"Open {settings.origin}\nAccess code file: {path.parent / 'access-code'}")
                uvicorn.run(
                    app, host="127.0.0.1", port=settings.port, access_log=False, ws="wsproto"
                )
        except Timeout:
            raise ValueError(
                "another UI server owns this state folder; stop it before restarting"
            ) from None
        return 0
    except ImportError as error:
        # Name the module that is missing: the hint is wrong when the UI is
        # installed and some other import failed.
        missing = f" (missing: {error.name})" if error.name else ""
        print(
            "Install the UI in its own environment: "
            f'python -m pip install "agentcraft2-ui[chat]"{missing}',
            file=sys.stderr,
        )
        if args.verbose:
            traceback.print_exc()
    except Exception as error:  # noqa: BLE001 - CLI boundary must not print secrets
        print(f"UI error: {_safe_message(error)} ({type(error).__name__})", file=sys.stderr)
        if args.verbose:
            # Opt-in only: a traceback can repeat configuration values.
            traceback.print_exc()
    return 2


def _runtimes(args: argparse.Namespace) -> "dict[str, AgentCoreRuntime]":
    """Map each ``--agentcore-runtime AGENT=ARN`` to a validated runtime description."""
    from agentcraft2_ui.contracts.catalog import AgentCoreRuntime

    result: dict[str, AgentCoreRuntime] = {}
    for item in args.agentcore_runtime or ():
        agent_id, separator, arn = item.partition("=")
        if not separator or not agent_id or agent_id not in args.agent:
            raise ValueError("--agentcore-runtime takes AGENT=ARN for an agent given with --agent")
        if agent_id in result:
            raise ValueError(f"--agentcore-runtime names agent {agent_id!r} twice")
        token_file = args.agentcore_token_file
        if (args.agentcore_auth == "bearer") != (token_file is not None):
            raise ValueError("--agentcore-token-file goes with --agentcore-auth bearer only")
        result[agent_id] = AgentCoreRuntime(
            arn=arn,
            auth=args.agentcore_auth,
            token_file=token_file.resolve() if token_file is not None else None,
        )
    return result


def _safe_message(error: Exception) -> str:
    """Describe a startup failure without its text, which may repeat .env content.

    Pydantic and provider errors quote the values they rejected, so only the
    kind of failure and where to look are printed. A ValueError is one of this
    module's own messages, written to be shown.
    """
    from pydantic import ValidationError

    from agentcraft2_core.contracts import ConfigurationError
    from agentcraft2_ui.contracts.queries import ContractError

    if isinstance(error, ContractError):
        return error.code.value + "; check registry IDs, urls and configuration pins"
    if isinstance(error, (ValidationError, ConfigurationError, json.JSONDecodeError)):
        return "invalid settings or registry; check the documented field names and values"
    if isinstance(error, OSError):
        return "a required file could not be read or written; check paths and folder permissions"
    if isinstance(error, ValueError):
        return str(error)
    return "UI startup failed; verify the separate UI environment and configuration"


if __name__ == "__main__":
    raise SystemExit(main())
