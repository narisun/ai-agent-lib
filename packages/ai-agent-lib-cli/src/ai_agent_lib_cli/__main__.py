"""Entry point of the ``agentlib`` command."""

from __future__ import annotations

import sys
from collections.abc import Sequence

import click

from ai_agent_lib_cli.app import cli
from ai_agent_lib_cli.errors import EXIT_FAILED, EXIT_OK, EXIT_USAGE, CliError
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_core.contracts import AgentLibError

__all__ = ["main"]


def main(argv: Sequence[str] | None = None, toolbox: Toolbox | None = None) -> int:
    """Run the command line and return its exit code.

    0 means done, 1 means the command was understood and could not be done,
    2 means the command line itself was wrong. A problem is one line on
    standard error, never a traceback.

    Args:
        argv: The command line. By default the process's own.
        toolbox: What the commands use to reach outside the process. By
            default the real tools; a test passes its own.
    """
    try:
        result = cli.main(
            args=list(argv) if argv is not None else None,
            prog_name="agentlib",
            standalone_mode=False,
            obj=toolbox if toolbox is not None else Toolbox(),
        )
    except click.UsageError as problem:
        problem.show()
        return EXIT_USAGE
    except click.ClickException as problem:
        problem.show()
        return EXIT_FAILED
    except click.Abort:
        click.echo("agentlib: stopped", err=True)
        return EXIT_FAILED
    except (CliError, AgentLibError) as problem:
        click.echo(f"agentlib: error: {problem}", err=True)
        return EXIT_FAILED
    return result if isinstance(result, int) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
