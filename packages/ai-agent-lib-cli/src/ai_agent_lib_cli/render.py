"""Rendering a template folder, and writing the result without losing work."""

from __future__ import annotations

import ast
import functools
import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import jinja2
import yaml

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.toml_text import toml_text

__all__ = ["Report", "TemplateRenderer", "validate_files", "write_files"]

_SUFFIX = ".jinja"
_PATH_MARK = re.compile(r"__([a-z]+(?:_[a-z]+)*)__")
_DOT_MARK = "dot_"


@functools.cache
def _package_templates() -> jinja2.Environment:
    """Return the environment over this package's templates, compiled once a process."""
    environment = jinja2.Environment(
        loader=jinja2.PackageLoader("ai_agent_lib_cli", "templates"),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,  # noqa: S701 - the output is source code, not HTML
    )
    # A value as Python source, for the code and the tests the templates write.
    environment.filters["py"] = repr
    environment.filters["toml"] = toml_text
    return environment


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


def validate_files(files: Mapping[PurePosixPath, str]) -> None:
    """Parse generated Python, TOML, YAML, and JSON before writing any files.

    This catches syntax errors, not runtime behavior, import failures, schema
    violations, or filesystem errors. Generated-consumer tests cover behavior.
    """
    for path, text in files.items():
        try:
            if path.suffix == ".py":
                ast.parse(text, filename=path.as_posix())
            elif path.suffix == ".toml":
                tomllib.loads(text)
            elif path.suffix in {".yaml", ".yml"}:
                yaml.safe_load(text)
            elif path.suffix == ".json" or path.name == "agentlib.lock":
                json.loads(text)
        except (SyntaxError, ValueError, yaml.YAMLError) as error:
            raise CliError(
                f"generated {path} is not valid {path.suffix[1:]}",
                fix="correct the generation inputs or template; nothing was written",
            ) from error


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
    """Validate generated files and check all conflicts before writing under ``root``.

    A file that exists with other content is the developer's work. Unless the
    file is one the tool maintains (``replace``) or ``force`` is set, the
    command stops before anything is written.

    Paths must be trusted, target-relative template paths. Writes are sequential,
    not transactional: a filesystem failure can leave earlier writes in place.

    Raises:
        CliError: If supported source cannot be parsed or an existing file
            conflicts with the requested content.
        OSError: If reading or writing the filesystem fails.
    """
    validate_files(files)
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
