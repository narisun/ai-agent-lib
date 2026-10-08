"""Rendering a template folder, and writing the result without losing work."""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import jinja2

from ai_agent_lib_cli.errors import CliError

__all__ = ["Report", "TemplateRenderer", "write_files"]

_SUFFIX = ".jinja"
_PATH_MARK = re.compile(r"__([a-z]+(?:_[a-z]+)*)__")
_DOT_MARK = "dot_"


@functools.cache
def _package_templates() -> jinja2.Environment:
    """Return the environment over this package's templates, compiled once a process."""
    return jinja2.Environment(
        loader=jinja2.PackageLoader("ai_agent_lib_cli", "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,  # noqa: S701 - the output is source code, not HTML
    )


class TemplateRenderer:
    """Renders the templates that ship inside this package.

    A template folder is rendered as a whole. In a path, ``__package__`` is
    replaced by the context value called ``package``, and likewise for any
    other context value, and a leading ``dot_`` by a dot. A variable the
    context does not supply is an error, never empty text.

    Args:
        environment: The Jinja environment. By default it loads the templates
            of this package.
    """

    def __init__(self, environment: jinja2.Environment | None = None) -> None:
        self._environment = environment or _package_templates()

    def render(self, folder: str, context: Mapping[str, object]) -> dict[PurePosixPath, str]:
        """Return the files a template folder produces, by path relative to the target."""
        prefix = f"{folder}/"
        names = [
            name
            for name in self._environment.list_templates()
            if name.startswith(prefix) and name.endswith(_SUFFIX)
        ]
        if not names:
            raise CliError(f"there is no template called {folder!r}")
        files: dict[PurePosixPath, str] = {}
        for name in sorted(names):
            relative = name.removeprefix(prefix).removesuffix(_SUFFIX)
            parts = [
                _PATH_MARK.sub(
                    # A mark that names nothing in the context is a real name: __init__.py.
                    lambda mark: str(context.get(mark.group(1), mark.group(0))),
                    "." + part.removeprefix(_DOT_MARK) if part.startswith(_DOT_MARK) else part,
                )
                for part in relative.split("/")
            ]
            files[PurePosixPath(*parts)] = self._environment.get_template(name).render(context)
        return files


@dataclass(frozen=True, slots=True)
class Report:
    """What a command did to the files of a workspace.

    Attributes:
        created: Files that did not exist.
        updated: Files whose content changed.
        unchanged: Files that already had the content.
    """

    created: tuple[Path, ...] = ()
    updated: tuple[Path, ...] = ()
    unchanged: tuple[Path, ...] = ()

    def merged(self, other: Report) -> Report:
        """Return the two reports as one."""
        return Report(
            created=self.created + other.created,
            updated=self.updated + other.updated,
            unchanged=self.unchanged + other.unchanged,
        )


def write_files(
    root: Path,
    files: Mapping[PurePosixPath, str],
    *,
    replace: frozenset[PurePosixPath] = frozenset(),
    force: bool = False,
) -> Report:
    """Write ``files`` under ``root``, or write nothing at all.

    A file that exists with other content is the developer's work. Unless the
    file is one the tool maintains (``replace``) or ``force`` is set, the
    command stops before anything is written.

    Raises:
        CliError: If a file would be overwritten.
    """
    created: list[Path] = []
    updated: list[Path] = []
    unchanged: list[Path] = []
    conflicts: list[str] = []
    for relative, text in files.items():
        path = root.joinpath(*relative.parts)
        if not path.exists():
            created.append(path)
        elif path.is_file() and path.read_text(encoding="utf-8") == text:
            unchanged.append(path)
        elif path.is_file() and (force or relative in replace):
            updated.append(path)
        else:
            conflicts.append(relative.as_posix())
    if conflicts:
        raise CliError(
            "these files exist and differ from what would be written: "
            + ", ".join(sorted(conflicts))
            + ". Nothing was changed. Use --force to overwrite them."
        )
    for relative, text in files.items():
        path = root.joinpath(*relative.parts)
        if path in unchanged:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    return Report(tuple(created), tuple(updated), tuple(unchanged))
