"""Adding an MCP server: what ``agentcraft2 new mcp`` does once its options are read.

The server is generated from its answers and from what a reader proposed, if
one did; then its tools' input schemas are pinned, unless that was declined.
A server whose tools cannot be pinned yet is still created: the pin can be
written later, and the result says how.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.readers import Proposal
from ai_agent_lib_cli.render import Report
from ai_agent_lib_cli.scaffold import Scaffolder
from ai_agent_lib_cli.workspace import SERVERS_FOLDER, McpAnswers, WorkspaceAnswers

__all__ = ["SAMPLE_DESCRIPTION", "McpCreated", "create_mcp", "default_description"]

SAMPLE_DESCRIPTION = "A greeting and a people directory, over sample data."
"""What the hello-world server offers, when no reader proposed anything."""


@dataclass(frozen=True, slots=True)
class McpCreated:
    """What adding an MCP server did, and what to tell the developer.

    Attributes:
        server: The answers the server was generated from.
        report: The files written.
        pinned: Whether the tools' input schemas were pinned in the registry.
        not_pinned: Why they were not, when pinning was asked for and failed.
    """

    server: McpAnswers
    report: Report
    pinned: bool = False
    not_pinned: str | None = None

    @property
    def headline(self) -> str:
        """The server, its registry ID and where it was written."""
        return (
            f"MCP server {self.server.name} (registry ID {self.server.server_id}) in "
            f"{SERVERS_FOLDER}/{self.server.name}"
        )

    @property
    def pin_note(self) -> str | None:
        """What happened to the pins, for the developer: nothing when pinning was declined."""
        if self.pinned:
            return "  pinned   the input schema of each tool in registry/mcp-tools.yaml"
        if self.not_pinned is not None:
            return (
                f"  not pinned: {self.not_pinned}\n"
                f"  Pin later with: agentcraft2 registry pin {self.server.server_id}"
            )
        return None

    @property
    def next_steps(self) -> list[str]:
        """What to do next, one command per line."""
        return [
            "python -m ai_agent_lib_cli install",
            f"python -m pytest {SERVERS_FOLDER}/{self.server.name}",
            self.server.name,
        ]


def default_description(proposal: Proposal | None) -> str:
    """What the server offers, unless the developer says otherwise."""
    return proposal.description if proposal is not None else SAMPLE_DESCRIPTION


def create_mcp(
    scaffolder: Scaffolder,
    root: Path,
    answers: WorkspaceAnswers,
    *,
    name: str,
    server_id: str,
    description: str,
    port: int,
    proposal: Proposal | None,
    pin: bool,
    force: bool,
) -> McpCreated:
    """Add the MCP server ``name`` to the workspace at ``root``.

    Args:
        scaffolder: Writes the server.
        root: The workspace.
        answers: The workspace's answers, as they are before the server is added.
        name: The server's name.
        server_id: Its ID in the tool registry.
        description: One line on what it offers.
        port: Its local HTTP port.
        proposal: What a reader proposed from the developer's data, if one ran.
            Without it the server is the hello-world sample.
        pin: Pin the tools' input schemas once the server is written. A pin
            that fails is reported, not raised: the server is there to fix.
        force: Overwrite files that exist and differ.

    Raises:
        CliError: If the name or the registry ID is taken, or a file would be
            overwritten.
    """
    server = McpAnswers(
        name=name,
        server_id=server_id,
        description=description,
        port=port,
        data=proposal.plan if proposal is not None else None,
    )
    report = scaffolder.add_mcp(
        root,
        server,
        pin=False,
        force=force,
        data_files=proposal.data_files if proposal is not None else None,
        extra_files=proposal.extra_files if proposal is not None else None,
        answers=answers,
    )
    if not pin:
        return McpCreated(server, report)
    try:
        scaffolder.pin(root, server.name, answers=answers.with_mcp_server(server))
    except CliError as problem:
        return McpCreated(server, report, not_pinned=str(problem))
    return McpCreated(server, report, pinned=True)
