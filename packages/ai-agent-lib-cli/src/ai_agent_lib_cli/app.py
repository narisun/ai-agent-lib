"""The ``agentlib`` command line: one group, and the commands it is made of."""

from __future__ import annotations

import click

from ai_agent_lib_cli import __version__, commands

__all__ = ["cli"]

_CONTEXT = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=_CONTEXT)
@click.version_option(__version__, "-V", "--version", prog_name="agentlib")
def cli() -> None:
    """Create, check and deploy a workspace of agents and MCP servers.

    Start with 'agentlib init <name>'. Every command writes working code with
    its tests, which run with no account and no network. 'agentlib deploy'
    writes what runs a service on AWS; it applies nothing.
    """


for _name in commands.__all__:
    cli.add_command(getattr(commands, _name))
