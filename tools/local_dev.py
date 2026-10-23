"""Set up and launch the repository's local testing scenarios using Python and pip."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

# Direct script execution puts tools/ on sys.path; imports use the repository root.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import package_runtime
from tools.local_processes import Service, report, serve

ROOT = Path(__file__).resolve().parents[1]
UI_PROJECT = Path("packages/agentcraft2-ui")
WORKSPACE = Path("examples/rmprep")
AGENT_ENV = WORKSPACE / "agents/rmprep/.env"
MCP_ENV = WORKSPACE / "mcp-servers/rmprep-data/.env"
UI_CONFIG = Path(".agentcraft2/ui-rmprep/config.json")


def run(command: Sequence[str], root: Path) -> None:
    """Run an explicit setup/check command and stop at its first failure."""
    subprocess.run(command, cwd=root, check=True)  # noqa: S603 - local CLI, no shell


def require_file(path: Path) -> None:
    """Explain how to prepare a missing environment or configuration file."""
    if not path.is_file():
        raise ValueError(f"Required file is missing: {path}. Run setup-local.bat first.")


def copy_settings(path: Path) -> None:
    """Create local defaults once, preserving existing credentials and settings."""
    contents = path.with_name(path.name + ".example").read_bytes()
    try:
        with path.open("xb") as output:
            output.write(contents)
    except FileExistsError:
        return


def prepare_ui(root: Path, config: Path) -> int:
    """Initialize a missing rmprep UI configuration, then validate saved registry pins."""
    if not config.exists():
        require_file(root / AGENT_ENV)
        report("Creating the local rmprep UI configuration...")
        run(
            package_runtime.command(
                root,
                "ui",
                "-m",
                "agentcraft2_ui",
                "init",
                "--config",
                str(config),
                "--env-file",
                str(root / AGENT_ENV),
                "--agent",
                "rmprep",
                "--artifacts-agent",
                "rmprep",
                "--allow-url",
                "http://127.0.0.1:8000",
            ),
            root,
        )
    run(
        package_runtime.command(
            root, "ui", "-m", "agentcraft2_ui", "check", "--config", str(config)
        ),
        root,
    )
    # The UI CLI above validates settings using its own installed contracts.
    values = json.loads(config.read_text(encoding="utf-8"))
    port = int(values.get("port", 8765))
    report(f"UI configuration: {config}")
    report(f"Open http://127.0.0.1:{port} when ready. For the login code, run in another window:")
    report(f'type "{config.parent / "access-code"}"')
    return port


# Setup runs before the library is installed, so it cannot ask the library for these names.
_PROVIDER = "AC2_MODEL_PROVIDER"
_LEGACY_PROVIDER = "EAP_MODEL_PROVIDER"  # Deprecation window: accepted, with a notice.


def setup_model(root: Path, selected: str = "auto") -> str:
    """Read only the provider setting, before third-party packages are installed.

    Precedence matches startup: explicit setup option, shell, root .env, service
    .env (or its example), then fake. This does not read or print credentials.
    The earlier ``EAP_`` spelling is read as well. Both spellings with different
    values stop setup, as they stop the agent, because neither is guessed to be meant.
    """
    if selected != "auto":
        return selected
    found: dict[str, str] = {}
    service = root / AGENT_ENV
    for path in (
        service if service.is_file() else service.with_name(".env.example"),
        root / ".env",
    ):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            key, separator, value = line.partition("=")
            name = key.strip().removeprefix("export ").strip()
            if separator and name in {_PROVIDER, _LEGACY_PROVIDER}:
                parts = shlex.split(value, comments=True)
                found[name] = parts[0] if len(parts) == 1 else ""
    found.update(
        (name, os.environ[name]) for name in (_PROVIDER, _LEGACY_PROVIDER) if name in os.environ
    )
    provider = found.get(_PROVIDER)
    if (legacy := found.get(_LEGACY_PROVIDER)) is not None:
        if provider is None:
            report(f"{_LEGACY_PROVIDER} is deprecated; use {_PROVIDER} instead")
            provider = legacy
        elif provider != legacy:
            raise ValueError(
                f"{_LEGACY_PROVIDER} is deprecated and conflicts with {_PROVIDER}; "
                f"set only {_PROVIDER}"
            )
    provider = "fake" if provider is None else provider
    if provider not in {"fake", "anthropic", "bedrock"}:
        raise ValueError("Select --model fake, anthropic, or bedrock for local setup.")
    return provider


def setup(root: Path, *, dev: bool = False, model: str = "auto") -> None:
    """Install isolated packages with the default Python and its pip module."""
    provider = setup_model(root, model)
    report(f"Python for all services and pip: {sys.executable}")
    report(f"Model dependencies: {provider}")
    # Resolve the local services together. Their package metadata selects the
    # runtime extras, including DuckDB, MCP, HTTP serving and trace export.
    agent_install: list[str] = []
    for project in (
        Path("packages/agentcraft2-core"),
        WORKSPACE / "agents/rmprep",
        WORKSPACE / "mcp-servers/rmprep-data",
    ):
        requirement = str(root / project)
        if project == Path("packages/agentcraft2-core") and provider == "anthropic":
            requirement += "[anthropic]"
        agent_install.extend(("-e", requirement))
    if provider == "bedrock":
        agent_install.extend(("-e", f"{root / 'packages/agentcraft2-aws'}[bedrock]"))
    if dev:
        project_config = tomllib.loads((root / WORKSPACE / "pyproject.toml").read_text("utf-8"))
        agent_install.extend(("-e", str(root / "packages/agentcraft2-cli")))
        # The CLI comes from this checkout. A second, bare requirement can make
        # older pip versions look for the unpublished package on the index.
        agent_install.extend(
            requirement
            for requirement in project_config["dependency-groups"]["dev"]
            if requirement != "agentcraft2-cli"
        )
    report("Installing the library, rmprep agent, and MCP server with pip...")
    package_runtime.install(root, "agent", agent_install)
    report("Installing Agent Lab's packages for the same Python...")
    package_runtime.install(
        root,
        "ui",
        [
            "-e",
            str(root / "packages/agentcraft2-core"),
            "-e",
            f"{root / UI_PROJECT}[{'chat,dev' if dev else 'chat'}]",
        ],
    )
    for relative in (AGENT_ENV, MCP_ENV):
        copy_settings(root / relative)
    prepare_ui(root, root / UI_CONFIG)
    if not dev:
        report("For automated tests and development tools, run setup-local.bat --dev.")
    report("Setup complete. Run start-local.bat to open the rmprep testing stack.")


def services_for(
    root: Path,
    scenario: str,
    config: Path,
    ui_port: int,
    *,
    agent_env: Mapping[str, str] | None = None,
    phoenix: Service | None = None,
) -> list[Service]:
    """Describe the dependency order and interpreter for each supported scenario."""
    services: list[Service] = [phoenix] if phoenix is not None else []
    if scenario in {"rmprep", "services"}:
        services.extend(
            [
                Service(
                    "rmprep-data",
                    tuple(
                        package_runtime.command(
                            root, "agent", "-m", "rmprep_data", "--port", "8100"
                        )
                    ),
                    root / WORKSPACE,
                    8100,
                ),
                Service(
                    "rmprep",
                    tuple(
                        package_runtime.command(
                            root, "agent", "-m", "rmprep.service", "--port", "8000"
                        )
                    ),
                    root / WORKSPACE,
                    8000,
                    environment=agent_env,
                ),
            ]
        )
    if scenario in {"rmprep", "ui"}:
        services.append(
            Service(
                "ui",
                tuple(
                    package_runtime.command(
                        root,
                        "ui",
                        str(root / "tools/local_service.py"),
                        "agentcraft2_ui",
                        "serve",
                        "--config",
                        str(config),
                    )
                ),
                root,
                ui_port,
                "/healthz",
            )
        )
    return services


def interrupt(_signum: int, _frame: object) -> NoReturn:
    """Let the supervisor's finally block handle terminal and process stop signals."""
    raise KeyboardInterrupt


def arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """Parse scenario options and reject combinations that would be ignored."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenario",
        nargs="?",
        default="rmprep",
        choices=("setup", "rmprep", "services", "ui", "demo"),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=UI_CONFIG,
        help="UI config path, relative to the repository root",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="rmprep overrides (default: root .env); explicit paths use your current folder",
    )
    parser.add_argument(
        "--smoke-test", action="store_true", help="start services, verify readiness, then stop them"
    )
    parser.add_argument(
        "--dev", action="store_true", help="setup only: also install the CLI and development tools"
    )
    parser.add_argument(
        "--model",
        choices=("auto", "fake", "anthropic", "bedrock"),
        default="auto",
        help="setup only: model dependencies (default: shell/root/service settings)",
    )
    args = parser.parse_args(argv)
    if args.dev and args.scenario != "setup":
        parser.error("--dev applies to setup; run setup-local.bat --dev")
    if args.model != "auto" and args.scenario != "setup":
        parser.error("--model applies to setup; configure the runtime model in .env")
    if args.smoke_test and args.scenario in {"setup", "demo"}:
        parser.error("--smoke-test applies to rmprep, services, or ui")
    if args.env_file is not None and args.scenario not in {"rmprep", "services"}:
        parser.error("--env-file applies to rmprep or services; demo uses scripted replies")
    if args.config != UI_CONFIG and args.scenario != "ui":
        parser.error(
            "--config applies to the UI-only scenario; the rmprep stack has its own saved UI"
        )
    return args


def main(argv: Sequence[str] | None = None, *, root: Path = ROOT) -> int:
    """Run setup or a selected local scenario; preserve settings and saved threads."""
    args = arguments(argv)
    try:
        if sys.version_info < (3, 11):  # noqa: UP036 - the setup entry point uses system Python
            raise ValueError("Use Python 3.11 or newer to run these scripts.")
        if args.scenario == "setup":
            setup(root, dev=args.dev, model=args.model)
            return 0
        report(f"Python for all services: {sys.executable}")
        if args.scenario != "ui":
            for relative in (AGENT_ENV, MCP_ENV):
                require_file(root / relative)
        if args.scenario == "demo":
            run(
                package_runtime.command(root, "agent", "-m", "rmprep.examples.trace_react"),
                root / WORKSPACE,
            )
            return 0
        agent_env = None
        phoenix = None
        if args.scenario in {"rmprep", "services"}:
            from tools.local_environment import agent_environment
            from tools.phoenix_dev import configured_service

            agent_env = agent_environment(
                (root / AGENT_ENV).resolve(),
                (args.env_file if args.env_file is not None else root / ".env").resolve(),
                required=args.env_file is not None,
                working_dir=root / WORKSPACE,
            )
            phoenix = configured_service(
                root, (root / AGENT_ENV).resolve(), agent_env, agent_cwd=root / WORKSPACE
            )
            report(f"MCP environment file: {(root / MCP_ENV).resolve()} (separate settings)")
        config = (root / args.config).resolve()
        if args.config != UI_CONFIG and not config.is_file():
            raise ValueError(
                f"UI configuration is missing: {config}. "
                "Create it with python tools/package_runtime.py ui -m agentcraft2_ui init first."
            )
        ui_port = 8765
        if args.scenario in {"rmprep", "ui"}:
            ui_port = prepare_ui(root, config)
        serve(
            services_for(
                root, args.scenario, config, ui_port, agent_env=agent_env, phoenix=phoenix
            ),
            root / ".agentcraft2/local-runs" / uuid4().hex,
            smoke_test=args.smoke_test,
        )
        return 0
    except KeyboardInterrupt:
        report("Stopped. Saved conversations and checkpoints are kept.")
        return 0
    except subprocess.CalledProcessError as error:
        report(
            f"A setup or configuration check failed (exit {error.returncode}). "
            "See the output above."
        )
        return 1
    except (OSError, ValueError, RuntimeError) as error:
        report(f"Local startup error: {error}")
        return 1


if __name__ == "__main__":
    # The supervisor resolves agent configuration through core. Load that same
    # isolated package set using the caller's Python, before any core imports.
    options = arguments(None)
    if options.scenario not in {"setup", "ui"} and not sys.flags.no_site:
        raise SystemExit(
            package_runtime.wait_for_child(
                package_runtime.command(ROOT, "agent", str(Path(__file__).resolve()), *sys.argv[1:])
            )
        )
    signal.signal(signal.SIGTERM, interrupt)
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, interrupt)
    raise SystemExit(main())
