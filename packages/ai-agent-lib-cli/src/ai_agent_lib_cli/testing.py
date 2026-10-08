"""Testing the commands without what makes them slow or reach outside the process.

A test runs the command line with a toolbox of its own::

    main(["new", "mcp", "hello-mcp"], toolbox=toolbox_for_tests(pins={...}))
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from ai_agent_lib_cli.pins import PinReader
from ai_agent_lib_cli.toolbox import Toolbox

__all__ = ["as_written", "fixed_pins", "toolbox_for_tests"]


def as_written(
    files: Mapping[PurePosixPath, str],
    first_party: Sequence[str],  # noqa: ARG001 - the formatter's signature
) -> dict[PurePosixPath, str]:
    """Stand in for the formatter: leave generated code as the templates write it."""
    return dict(files)


def fixed_pins(pins: Mapping[str, str]) -> PinReader:
    """Return a pin reader that reports ``pins`` for every server, without starting one."""

    def read(service_dir: Path, package: str) -> dict[str, str]:  # noqa: ARG001
        return dict(pins)

    return read


def toolbox_for_tests(*, pins: Mapping[str, str] | None = None, **tools: Any) -> Toolbox:
    """Return a toolbox for a test of the commands.

    Generated code is not formatted, which is most of what a command takes
    time for; formatting has tests of its own.

    Args:
        pins: The pins every server reports. Without them a server is really
            started and asked, as the command does.
        **tools: Any other tool to replace, by its name in ``Toolbox``.
    """
    chosen: dict[str, Any] = {"format_python": as_written}
    if pins is not None:
        chosen["read_pins"] = fixed_pins(pins)
    return Toolbox(**{**chosen, **tools})
