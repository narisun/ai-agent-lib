"""What the commands use to reach outside the process.

A command formats code, starts a generated server to read its pins, runs a
service, starts OPA, calls AWS and asks questions at a terminal. Each of those
is a function a ``Toolbox`` holds, so a command never reaches for one itself:
the real ones are the defaults, and a test passes its own.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field

import click

from ai_agent_lib_cli.formatting import Formatter, format_python
from ai_agent_lib_cli.pins import PinReader, read_pins
from ai_agent_lib_cli.policy import OpaStarter, opa_server
from ai_agent_lib_cli.processes import GraphReader, ServiceRunner, read_graph, run_service
from ai_agent_lib_cli.read_redshift import CatalogReader, fetch_catalog
from ai_agent_lib_cli.render import TemplateRenderer
from ai_agent_lib_cli.scaffold import Scaffolder

__all__ = ["Prompt", "Toolbox", "terminal_prompt"]

Prompt = Callable[[str, str], str | None]
"""Given a question and a default answer, returns what a person typed.

Returns ``None`` when there is nobody to ask, so the default stands.
"""


def terminal_prompt(question: str, default: str) -> str | None:
    """Ask at the terminal, if the command is running at one."""
    if not sys.stdin.isatty():
        return None
    return str(click.prompt(question, default=default))


@dataclass(frozen=True, slots=True)
class Toolbox:
    """The functions the commands use to reach outside the process.

    Attributes:
        renderer: Renders the templates.
        format_python: Formats generated Python.
        read_pins: Asks a generated MCP server for the pins of its tools.
        run_service: Runs a service until it ends.
        read_graph: Asks a generated agent for its graph.
        fetch_catalog: Reads tables and columns from the Redshift catalogue.
        opa_server: Starts a local OPA.
        prompt: Asks a person a question.
    """

    renderer: TemplateRenderer = field(default_factory=TemplateRenderer)
    format_python: Formatter = format_python
    read_pins: PinReader = read_pins
    run_service: ServiceRunner = run_service
    read_graph: GraphReader = read_graph
    fetch_catalog: CatalogReader = fetch_catalog
    opa_server: OpaStarter = opa_server
    prompt: Prompt = terminal_prompt

    def scaffolder(self) -> Scaffolder:
        """Return what creates workspaces and adds services, built from these tools."""
        return Scaffolder(self.renderer, pin_reader=self.read_pins, formatter=self.format_python)

    def ask(self, given: str | None, question: str, default: str) -> str:
        """Return an answer: the one given, or one a person types, or the default."""
        if given is not None:
            return given
        answer = self.prompt(question, default)
        return default if answer is None else answer
