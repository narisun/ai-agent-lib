"""The service being deployed, the platform it goes to, and where its deployment lives."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from agentcraft2_cli.errors import CliError
from agentcraft2_cli.names import package_name
from agentcraft2_cli.workspace import AGENTS_FOLDER, SERVERS_FOLDER, WorkspaceAnswers

__all__ = [
    "AGENTCORE",
    "AWS_DISTRIBUTION",
    "DEPLOY_ENV",
    "FARGATE",
    "MODULE_FOLDER",
    "OPA_FOLDER",
    "PLATFORMS",
    "Target",
    "target_of",
]

DEPLOY_ENV = "deploy.env"
"""The file in a service's folder that holds the settings it runs with in AWS."""

MODULE_FOLDER = PurePosixPath("deploy", "modules", "agentcraft2-service")
"""Where the reference Terraform module is copied in a workspace."""

OPA_FOLDER = PurePosixPath("deploy", "opa")
"""Where the OPA sidecar's Dockerfile and the platform bundle are copied."""

AWS_DISTRIBUTION = "agentcraft2-aws"
"""The distribution that holds the AWS adapters a deployed service selects."""

FARGATE = "fargate"
"""The platform that runs the service as an ECS service on Fargate."""

AGENTCORE = "agentcore"
"""The platform that runs the service as an Amazon Bedrock AgentCore Runtime."""

PLATFORMS = (FARGATE, AGENTCORE)
"""The platforms ``agentcraft2 deploy`` writes a deployment for, the default first."""

_ALL_INTERFACES = "0.0.0.0"  # noqa: S104 - the container's port is reached through the task's network

# AgentCore Runtime reaches an HTTP agent on 8080 and an MCP server on 8000, whatever
# the service listens on at home; its ``agentcore`` profile knows these ports.
_AGENTCORE_PORTS = {"agent": 8080, "mcp": 8000}
_AGENTCORE_PROFILE = ("--profile", "agentcore")


@dataclass(frozen=True, slots=True)
class Target:
    """The service being deployed, and the platform it is deployed to.

    Attributes:
        name: The service's name.
        kind: ``agent`` or ``mcp``.
        folder: Its folder, relative to the workspace root.
        service_port: The port it listens on in the workspace, which is the one
            a Fargate task exposes. AgentCore Runtime fixes the port itself.
        platform: ``fargate`` or ``agentcore``.
    """

    name: str
    kind: str
    folder: PurePosixPath
    service_port: int
    platform: str = FARGATE

    def __post_init__(self) -> None:
        if self.platform not in PLATFORMS:
            raise ValueError(
                f"platform must be one of {', '.join(PLATFORMS)}, not {self.platform!r}"
            )

    @property
    def package(self) -> str:
        """The service's Python package: its name with underscores for hyphens."""
        return package_name(self.name)

    @property
    def entry_module(self) -> str:
        """The module whose ``main`` serves it.

        ``<package>.service`` for an agent; for an MCP server the package itself,
        whose ``__main__`` serves.
        """
        return f"{self.package}.service" if self.kind == "agent" else self.package

    @property
    def port(self) -> int:
        """The port the deployed container listens on."""
        if self.platform == AGENTCORE:
            return _AGENTCORE_PORTS[self.kind]
        return self.service_port

    @property
    def protocol(self) -> str:
        """What the service speaks, as AgentCore Runtime names it: ``HTTP`` or ``MCP``."""
        return "HTTP" if self.kind == "agent" else "MCP"

    @property
    def command(self) -> tuple[str, ...]:
        """The command that serves it in the container.

        On Fargate the console script listens on every interface at the
        workspace's port. On AgentCore Runtime the entry module runs under
        ``opentelemetry-instrument`` (the ADOT distribution the image installs)
        with the service's ``agentcore`` profile, which picks the address, the
        port and the platform's routes.
        """
        if self.platform == AGENTCORE:
            return (
                "opentelemetry-instrument",
                "python",
                "-m",
                self.entry_module,
                *_AGENTCORE_PROFILE,
            )
        program = f"{self.name}-serve" if self.kind == "agent" else self.name
        return (program, "--host", _ALL_INTERFACES, "--port", str(self.port))

    @property
    def settings(self) -> PurePosixPath:
        """Its ``deploy.env``, relative to the workspace root."""
        return self.folder / DEPLOY_ENV

    @property
    def deployment(self) -> PurePosixPath:
        """The folder of its Terraform root module, relative to the workspace root.

        One folder per service, whatever the platform: a service is deployed to
        one platform at a time. Switching platforms means emptying the folder
        first, since the files written once are the developer's.
        """
        return PurePosixPath("deploy", self.name)


def target_of(answers: WorkspaceAnswers, name: str, platform: str = FARGATE) -> Target:
    """Return the service called ``name``, deployed to ``platform``.

    Raises:
        CliError: If the workspace has no such service.
    """
    agent = answers.agent(name)
    if agent is not None:
        folder = PurePosixPath(AGENTS_FOLDER, agent.name)
        return Target(agent.name, "agent", folder, agent.port, platform)
    server = answers.mcp_server(name)
    if server is not None:
        folder = PurePosixPath(SERVERS_FOLDER, server.name)
        return Target(server.name, "mcp", folder, server.port, platform)
    known = ", ".join(sorted(answers.service_names())) or "none"
    raise CliError(
        f"there is no service called {name!r} in this workspace",
        expected="the name of an agent or MCP server of this workspace",
        actual=f"{name!r}; the services are: {known}",
    )
