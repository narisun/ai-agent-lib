"""Run a separate Phoenix trial with pip, SQLite, and a loopback-only listener."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import uuid4

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import package_runtime
from tools.local_processes import Service, report, serve
from tools.phoenix_server import PINNED_VERSION as VERSION

ROOT = Path(__file__).resolve().parents[1]


def setup(root: Path) -> None:
    """Install server dependencies using the existing Python and enterprise pip settings."""
    report(f"Python for Phoenix and pip: {sys.executable}")
    package_runtime.install(root, "phoenix", [f"arize-phoenix=={VERSION}"])
    report("Phoenix setup complete. Enable it in the root .env, then run start-local.bat.")


def server_environment(folder: Path) -> dict[str, str]:
    """Isolate storage and telemetry from agent credentials and inherited server settings."""
    environment = {
        key: value
        for key, value in os.environ.items()
        # LANGFUSE_ is defense for stale environments: the library no longer reads it.
        # EAP_ is the earlier prefix of the library's variables (deprecation window).
        if not key.startswith(
            (
                "AC2_",
                "EAP_",
                "RMPREP_",
                "PHOENIX_",
                "OTEL_",
                "LANGFUSE_",
                "ANTHROPIC_",
                "OPENAI_",
                "AWS_",
            )
        )
    }
    environment.update(
        {
            "PHOENIX_WORKING_DIR": str(folder / "data"),
            "PHOENIX_SQL_DATABASE_URL": "sqlite:///" + (folder / "data" / "phoenix.db").as_posix(),
            "PHOENIX_HOST": "127.0.0.1",
            "PHOENIX_PORT": "6006",
            "PHOENIX_TELEMETRY_ENABLED": "false",
            "PHOENIX_DISABLE_AGENT_ASSISTANT": "true",
            # This local component collects traces; code evaluation would need
            # optional sandbox runtimes or downloads that the trial does not use.
            "PHOENIX_ALLOWED_SANDBOX_PROVIDERS": "NONE",
            # Agents own model/MCP calls. Phoenix only receives and displays
            # their traces; it does not need a second playground or MCP server.
            "PHOENIX_ALLOWED_PROVIDERS": "NONE",
            "PHOENIX_ENABLE_MCP_SERVER": "false",
            "PHOENIX_ENABLE_MCP_CODE_MODE": "false",
            "PHOENIX_ENABLE_PROMETHEUS": "false",
        }
    )
    return environment


def service(root: Path) -> Service:
    """Describe the same isolated server for standalone and supervised startup."""
    folder = root / ".agentcraft2" / "phoenix"
    if not package_runtime.package_folder(root, "phoenix").is_dir():
        raise ValueError("Run start-phoenix.bat setup first to install Phoenix's packages.")
    (folder / "data").mkdir(parents=True, exist_ok=True)
    report(f"Phoenix {VERSION}: http://127.0.0.1:6006")
    report(f"SQLite database: {folder / 'data' / 'phoenix.db'}")
    return Service(
        "phoenix",
        tuple(
            package_runtime.command(
                root,
                "phoenix",
                str(root / "tools/local_service.py"),
                "tools.phoenix_server",
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                "6006",
            )
        ),
        root,
        6006,
        "/healthz",
        environment=server_environment(folder),
    )


def configured_service(
    root: Path, service_env: Path, environment: Mapping[str, str], *, agent_cwd: Path
) -> Service | None:
    """Start the local trial only when the agent's effective settings select it."""
    from ai_agent_lib_core.config import MappingConfigSource
    from ai_agent_lib_core.config.bindings import legacy_spelling
    from ai_agent_lib_core.config.phoenix import PHOENIX_LABELS, load_phoenix_settings
    from ai_agent_lib_core.contracts import ConfigurationError

    # Environment-file paths in shell/root overrides use the agent's working
    # directory, just as they do when the child resolves its own settings. Either
    # spelling may carry one; when both do, the loader judges whether they agree.
    settings_env = dict(environment)
    for name in (PHOENIX_LABELS["env_file"], legacy_spelling(PHOENIX_LABELS["env_file"])):
        if extra := settings_env.get(name):
            settings_env[name] = str((agent_cwd / extra).resolve())
    try:
        settings = load_phoenix_settings(service_env, environment=MappingConfigSource(settings_env))
    except ConfigurationError as error:
        raise ValueError(str(error)) from error
    if settings is None:
        return None
    if settings.base_url.rstrip("/") not in {
        "http://127.0.0.1:6006",
        "http://localhost:6006",
    }:
        report(f"Phoenix endpoint is managed separately: {settings.base_url}")
        return None
    return service(root)


def start(root: Path) -> None:
    """Register standalone Phoenix with the same cooperative stop command."""
    phoenix = service(root)
    serve([phoenix], root / ".agentcraft2/local-runs" / uuid4().hex)


def main(argv: Sequence[str] | None = None) -> int:
    """Expose setup and foreground startup without requiring shell activation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "start"), nargs="?", default="start")
    args = parser.parse_args(argv)
    try:
        (setup if args.action == "setup" else start)(ROOT)
    except KeyboardInterrupt:
        report("Phoenix stopped. Saved traces are kept.")
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        report(f"Phoenix could not start: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
