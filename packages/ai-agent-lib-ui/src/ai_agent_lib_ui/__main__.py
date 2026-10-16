"""Initialize, validate and serve the optional local agent frontend."""

import argparse
import asyncio
import json
import sys
from pathlib import Path


def arguments() -> argparse.Namespace:
    """Parse commands without importing optional runtime dependencies."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "check", "serve"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, default=Path(".agentlib/ui/config.json"))
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
                required=True,
                help="exact loopback registry URL; repeat for more endpoints",
            )
            command.add_argument("--port", type=int, default=8765)
            command.add_argument(
                "--artifacts-agent",
                action="append",
                default=[],
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
        from ai_agent_lib_ui.adapters.catalog import (
            FileCatalog,
            LocalAccess,
            pin_bindings,
            read_registries,
        )
        from ai_agent_lib_ui.config.settings import Settings, agent_environment
        from ai_agent_lib_ui.config.shell import prepare_shell
        from ai_agent_lib_ui.contracts.queries import ContractError, PageRequest
        from ai_agent_lib_ui.integrations.runtime import compose

        path = args.config.resolve()
        if args.command == "init":
            if path.exists() and not args.overwrite:
                raise ValueError(
                    "config already exists; use --overwrite to explicitly refresh registry pins"
                )
            agents_path, tools_path, identity = agent_environment(args.env_file.resolve())
            agents, _ = read_registries(agents_path, tools_path)
            allowed = frozenset(args.allow_url)
            if not set(args.artifacts_agent) <= set(args.agent):
                raise ValueError("--artifacts-agent must also be selected with --agent")
            settings = Settings(
                agents_path=agents_path,
                tools_path=tools_path,
                identity=identity,
                bindings=pin_bindings(
                    agents,
                    tuple(args.agent),
                    allowed,
                    artifact_agent_ids=frozenset(args.artifacts_agent),
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
                from ai_agent_lib_ui.integrations.server import create_app

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
    except ImportError:
        print(
            'Install the UI in its own environment: python -m pip install "ai-agent-lib-ui[chat]"',
            file=sys.stderr,
        )
    except Exception as error:  # noqa: BLE001 - CLI boundary must not print secrets
        # Pydantic/provider errors may contain configuration inputs. Do not print
        # their repr or a traceback, which can accidentally disclose .env content.
        from pydantic import ValidationError

        from ai_agent_lib_core.contracts import ConfigurationError
        from ai_agent_lib_ui.contracts.queries import ContractError

        if isinstance(error, ContractError):
            message = error.code.value + "; check registry IDs, urls and configuration pins"
        elif isinstance(error, (ValidationError, ConfigurationError, json.JSONDecodeError)):
            message = "invalid settings or registry; check the documented field names and values"
        elif isinstance(error, OSError):
            message = (
                "a required file could not be read or written; check paths and folder permissions"
            )
        elif isinstance(error, ValueError):
            message = str(error)
        else:
            message = "UI startup failed; verify the separate UI environment and configuration"
        print("UI error: " + message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
