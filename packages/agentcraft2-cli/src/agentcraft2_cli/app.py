"""The ``agentcraft2`` command line: one group, and the commands it is made of."""

from __future__ import annotations

import click

from agentcraft2_cli import __version__, commands

__all__ = ["cli"]

_CONTEXT = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=_CONTEXT)
@click.version_option(__version__, "-V", "--version", prog_name="agentcraft2")
def cli() -> None:
    """Create, check and deploy a workspace of agents and MCP servers.

    Start with 'agentcraft2 init <name>'. Every command writes working code with
    its tests, which run with no account and no network. 'agentcraft2 deploy'
    writes what runs a service on AWS; it applies nothing.
    """


for _name in commands.__all__:
    cli.add_command(getattr(commands, _name))
