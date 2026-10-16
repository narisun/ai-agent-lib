"""Set up and launch the repository's local testing scenarios using Python and pip."""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

# Direct script execution puts tools/ on sys.path; imports use the repository root.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.local_processes import Service, report, serve

ROOT = Path(__file__).resolve().parents[1]
UI_PROJECT = Path("packages/ai-agent-lib-ui")
WORKSPACE = Path("examples/rmprep")
AGENT_ENV = WORKSPACE / "agents/rmprep/.env"
MCP_ENV = WORKSPACE / "mcp-servers/rmprep-data/.env"
UI_CONFIG = Path(".agentlib/ui-rmprep/config.json")


def interpreter(environment: Path) -> Path:
    """Find a virtual environment's Python without activating it in the shell."""
    return environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


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


def prepare_ui(root: Path, ui_python: Path, config: Path) -> int:
    """Initialize a missing rmprep UI configuration, then validate saved registry pins."""
    require_file(ui_python)
    if not config.exists():
        require_file(root / AGENT_ENV)
        report("Creating the local rmprep UI configuration...")
        run(
            [
                str(ui_python),
                "-m",
                "ai_agent_lib_ui",
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
            ],
            root,
        )
    run([str(ui_python), "-m", "ai_agent_lib_ui", "check", "--config", str(config)], root)
    # The UI CLI above validates settings using its own installed contracts.
    values = json.loads(config.read_text(encoding="utf-8"))
    port = int(values.get("port", 8765))
    report(f"UI configuration: {config}")
    report(f"Open http://127.0.0.1:{port} when ready. For the login code, run in another window:")
    report(f'type "{config.parent / "access-code"}"')
    return port


def setup(root: Path) -> None:
    """Install the rmprep and UI environments using the enterprise pip configuration."""
    agent_python = interpreter(root / ".venv")
    ui_python = interpreter(root / UI_PROJECT / ".venv")
    for environment, python in (
        (root / ".venv", agent_python),
        (root / UI_PROJECT / ".venv", ui_python),
    ):
        if not python.exists():
            report(f"Creating environment: {environment}")
            run([sys.executable, "-m", "venv", str(environment)], root)
    report("Installing the library, rmprep agent, and MCP server with pip...")
    run(
        [
            str(agent_python),
            "-m",
            "pip",
            "install",
            "-e",
            str(root / "packages/ai-agent-lib-core"),
            "-e",
            str(root / "packages/ai-agent-lib-aws"),
            "-e",
            str(root / "packages/ai-agent-lib-cli"),
        ],
        root,
    )
    run(
        [
            str(agent_python),
            "-m",
            "ai_agent_lib_cli",
            "install",
            "--workspace",
            str(root / WORKSPACE),
        ],
        root,
    )
    report("Installing the UI in its separate environment...")
    run(
        [
            str(ui_python),
            "-m",
            "pip",
            "install",
            "-e",
            str(root / "packages/ai-agent-lib-core"),
            "-e",
            f"{root / UI_PROJECT}[chat,dev]",
        ],
        root,
    )
    for python in (agent_python, ui_python):
        run([str(python), "-m", "pip", "check"], root)
    for relative in (AGENT_ENV, MCP_ENV):
        copy_settings(root / relative)
    prepare_ui(root, ui_python, root / UI_CONFIG)
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
    agent_python = str(interpreter(root / ".venv"))
    ui_python = str(interpreter(root / UI_PROJECT / ".venv"))
    services: list[Service] = [phoenix] if phoenix is not None else []
    if scenario in {"rmprep", "services"}:
        services.extend(
            [
                Service(
                    "rmprep-data",
                    (agent_python, "-u", "-m", "rmprep_data", "--port", "8100"),
                    root / WORKSPACE,
                    8100,
                ),
                Service(
                    "rmprep",
                    (agent_python, "-u", "-m", "rmprep.service", "--port", "8000"),
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
                (
                    ui_python,
                    "-u",
                    str(root / "tools/local_service.py"),
                    "ai_agent_lib_ui",
                    "serve",
                    "--config",
                    str(config),
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
    args = parser.parse_args(argv)
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
            setup(root)
            return 0
        agent_python = interpreter(root / ".venv")
        if args.scenario != "ui":
            require_file(agent_python)
            for relative in (AGENT_ENV, MCP_ENV):
                require_file(root / relative)
        if args.scenario == "demo":
            run([str(agent_python), "-m", "rmprep.examples.trace_react"], root / WORKSPACE)
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
                "Create it with python -m ai_agent_lib_ui init in the UI environment first."
            )
        ui_port = 8765
        if args.scenario in {"rmprep", "ui"}:
            ui_port = prepare_ui(root, interpreter(root / UI_PROJECT / ".venv"), config)
        serve(
            services_for(
                root, args.scenario, config, ui_port, agent_env=agent_env, phoenix=phoenix
            ),
            root / ".agentlib/local-runs" / uuid4().hex,
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
    signal.signal(signal.SIGTERM, interrupt)
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, interrupt)
    raise SystemExit(main())
