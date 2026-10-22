"""The service being deployed, and where its deployment lives in the workspace."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.workspace import AGENTS_FOLDER, SERVERS_FOLDER, WorkspaceAnswers

__all__ = [
    "AWS_DISTRIBUTION",
    "DEPLOY_ENV",
    "MODULE_FOLDER",
    "OPA_FOLDER",
    "Target",
    "target_of",
]

DEPLOY_ENV = "deploy.env"
"""The file in a service's folder that holds the settings it runs with in AWS."""

MODULE_FOLDER = PurePosixPath("deploy", "modules", "agentlib-service")
"""Where the reference Terraform module is copied in a workspace."""

OPA_FOLDER = PurePosixPath("deploy", "opa")
"""Where the OPA sidecar's Dockerfile and the platform bundle are copied."""

AWS_DISTRIBUTION = "ai-agent-lib-aws"
"""The distribution that holds the AWS adapters a deployed service selects."""

_ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - the container's port is reached through the task's network


@dataclass(frozen=True, slots=True)
class Target:
    """The service being deployed.

    Attributes:
        name: The service's name.
        kind: ``agent`` or ``mcp``.
        folder: Its folder, relative to the workspace root.
        port: The port it listens on.
    """

    name: str
    kind: str
    folder: PurePosixPath
    port: int

    @property
    def command(self) -> tuple[str, ...]:
        """The command that serves it in the container."""
        program = f"{self.name}-serve" if self.kind == "agent" else self.name
        return (program, "--host", _ALL_INTERFACES, "--port", str(self.port))

    @property
    def settings(self) -> PurePosixPath:
        """Its ``deploy.env``, relative to the workspace root."""
        return self.folder / DEPLOY_ENV

    @property
    def deployment(self) -> PurePosixPath:
        """The folder of its Terraform root module, relative to the workspace root."""
        return PurePosixPath("deploy", self.name)


def target_of(answers: WorkspaceAnswers, name: str) -> Target:
    """Return the service called ``name``.

    Raises:
        CliError: If the workspace has no such service.
    """
    agent = answers.agent(name)
    if agent is not None:
        return Target(agent.name, "agent", PurePosixPath(AGENTS_FOLDER, agent.name), agent.port)
    server = answers.mcp_server(name)
    if server is not None:
        return Target(server.name, "mcp", PurePosixPath(SERVERS_FOLDER, server.name), server.port)
    known = ", ".join(sorted(answers.service_names())) or "none"
    raise CliError(
        f"there is no service called {name!r} in this workspace",
        expected="the name of an agent or MCP server of this workspace",
        actual=f"{name!r}; the services are: {known}",
    )
