"""Looking at a workspace without changing it: what is wrong, and what is configured."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.names import package_name
from ai_agent_lib_cli.pins import PinReader
from ai_agent_lib_cli.shared import AGENTS_FILE, RULES_FILE, TOOLS_FILE
from ai_agent_lib_cli.workspace import AGENTS_FOLDER, SERVERS_FOLDER, WorkspaceAnswers
from ai_agent_lib_core.adapters import (
    FileRegistryOptions,
    FileRegistrySource,
    RulesPolicyDecisionPoint,
    RulesPolicyOptions,
    UuidGenerator,
)
from ai_agent_lib_core.config import DEFAULT_BINDINGS, load_service_config, service_resolver
from ai_agent_lib_core.contracts import AgentLibError, CheckResult
from ai_agent_lib_core.di import diagnose, fix_for
from ai_agent_lib_core.observability import redact

__all__ = [
    "Setting",
    "check_service",
    "check_workspace",
    "explain_service",
    "service_folder",
]

_ENV = ".env"
_MAX_VALUE = 120


def service_folder(root: Path, answers: WorkspaceAnswers, name: str) -> Path:
    """Return the folder of the service called ``name``.

    Raises:
        CliError: If the workspace has no such service.
    """
    if answers.agent(name) is not None:
        return root / AGENTS_FOLDER / name
    server = answers.mcp_server(name)
    if server is not None:
        return root / SERVERS_FOLDER / server.name
    known = ", ".join(sorted(answers.service_names())) or "none"
    raise CliError(f"there is no service called {name!r} in this workspace (services: {known})")


async def check_service(folder: Path) -> tuple[CheckResult, ...]:
    """Check everything the service in ``folder`` is configured to use."""
    dotenv = folder / _ENV
    if not dotenv.is_file():
        return (
            CheckResult(
                "settings",
                ok=False,
                detail=f"{dotenv} does not exist",
                fix="Copy .env.example to .env in the service's folder.",
            ),
        )
    try:
        config = load_service_config(dotenv)
    except AgentLibError as problem:
        return (CheckResult("settings", ok=False, detail=str(problem), fix=fix_for(problem)),)
    return (CheckResult("settings", ok=True, detail="resolved"), *await diagnose(config))


def _pins(
    root: Path, answers: WorkspaceAnswers, registry: FileRegistrySource, pin_reader: PinReader
) -> list[CheckResult]:
    results: list[CheckResult] = []
    for server in answers.mcp_servers:
        name = f"pins of {server.server_id}"
        fix = f"Run: agentlib registry pin {server.server_id}"
        entry = registry.tools.get(server.server_id)
        if entry is None:
            results.append(
                CheckResult(
                    name,
                    ok=False,
                    detail=f"{server.server_id} is not in {TOOLS_FILE}",
                    fix=f"Run: agentlib new mcp {server.name}",
                )
            )
            continue
        try:
            live = pin_reader(root / SERVERS_FOLDER / server.name, package_name(server.name))
        except CliError as problem:
            results.append(CheckResult(name, ok=False, detail=str(problem), fix=fix))
            continue
        registered = {tool.name: tool.schema_sha256 for tool in entry.tools}
        stale = sorted(tool for tool, pin in live.items() if registered.get(tool) != pin)
        gone = sorted(set(registered) - set(live))
        if stale or gone:
            parts = [f"changed or not pinned: {', '.join(stale)}"] if stale else []
            parts += [f"registered but not offered: {', '.join(gone)}"] if gone else []
            results.append(CheckResult(name, ok=False, detail="; ".join(parts), fix=fix))
        else:
            results.append(CheckResult(name, ok=True, detail=f"{len(live)} tools match"))
    return results


def check_workspace(
    root: Path, answers: WorkspaceAnswers, *, pin_reader: PinReader | None
) -> tuple[CheckResult, ...]:
    """Check the files every service shares, and each server's tools against their pins.

    Args:
        root: The workspace.
        answers: Its recorded answers.
        pin_reader: Asks a server for its pins. ``None`` skips the pin checks.
    """
    results: list[CheckResult] = []
    try:
        registry = FileRegistrySource(
            FileRegistryOptions(
                agents_path=root.joinpath(*AGENTS_FILE.parts),
                tools_path=root.joinpath(*TOOLS_FILE.parts),
            )
        )
    except AgentLibError as problem:
        fix = "Correct the registry file the message names."
        return (CheckResult("registries", ok=False, detail=str(problem), fix=fix),)
    results.append(CheckResult("registries", ok=True, detail="valid and consistent"))
    try:
        RulesPolicyDecisionPoint(
            RulesPolicyOptions(path=root.joinpath(*RULES_FILE.parts)), UuidGenerator()
        )
    except AgentLibError as problem:
        fix = f"Correct {RULES_FILE}."
        results.append(CheckResult("rules", ok=False, detail=str(problem), fix=fix))
    else:
        results.append(CheckResult("rules", ok=True, detail="valid"))
    if pin_reader is not None:
        results += _pins(root, answers, registry, pin_reader)
    return tuple(results)


@dataclass(frozen=True, slots=True)
class Setting:
    """One setting of a service, as it may be shown.

    Attributes:
        variable: The variable that sets it.
        value: Its value. Sensitive values are masked.
        origin: Where the value came from.
    """

    variable: str
    value: str
    origin: str


def explain_service(folder: Path) -> tuple[Sequence[Setting], Sequence[str]]:
    """Return every setting of the service in ``folder`` and where each came from.

    Returns:
        The settings, in the order of the variable reference, and any warnings.

    Raises:
        CliError: If the configuration is not valid.
    """
    try:
        resolution = service_resolver(folder / _ENV).explain()
    except AgentLibError as problem:
        raise CliError(str(problem)) from None
    by_key = {binding.key: binding.name for binding in DEFAULT_BINDINGS}
    settings = [
        Setting(
            variable=by_key[key],
            # A value that is not marked sensitive can still carry a credential.
            value=redact(resolution.shown[key])[:_MAX_VALUE],
            origin=resolution.origins.get(key, ""),
        )
        for key in by_key
        if key in resolution.shown
    ]
    settings += [
        Setting(
            variable=resolution.origins[key].removeprefix("variable "),
            value=value,
            origin=resolution.origins[key],
        )
        for key, value in sorted(resolution.shown.items())
        if key not in by_key
    ]
    return settings, resolution.warnings
