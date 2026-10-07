"""A scanner that keeps configuration names inside the ``config`` package.

Outside ``config`` no module may read the process environment, load a ``.env``
file or contain a literal that starts with the library's variable prefix. That
is what makes renaming a variable a change to one module only.
"""

from __future__ import annotations

import ast

__all__ = ["find_violations"]

PREFIX = "EAP_"
_ENV_ATTRIBUTES = frozenset({"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"})
_FORBIDDEN_MODULES = frozenset({"dotenv"})


class _Scanner(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[tuple[int, str]] = []
        self._os_aliases: set[str] = set()

    def _add(self, node: ast.AST, message: str) -> None:
        self.violations.append((getattr(node, "lineno", 0), message))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root == "os":
                self._os_aliases.add(alias.asname or root)
            if root in _FORBIDDEN_MODULES:
                self._add(node, f"imports {root}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        root = (node.module or "").split(".")[0]
        if root == "os":
            for alias in node.names:
                if alias.name in _ENV_ATTRIBUTES:
                    self._add(node, f"imports os.{alias.name}")
        if root in _FORBIDDEN_MODULES:
            self._add(node, f"imports {root}")
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            node.attr in _ENV_ATTRIBUTES
            and isinstance(node.value, ast.Name)
            and node.value.id in self._os_aliases
        ):
            self._add(node, f"reads the environment through os.{node.attr}")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and PREFIX in node.value:
            self._add(node, f"contains a literal with the {PREFIX} prefix")
        self.generic_visit(node)


def find_violations(source: str) -> list[tuple[int, str]]:
    """Return ``(line, message)`` for each containment violation in ``source``."""
    scanner = _Scanner()
    scanner.visit(ast.parse(source))
    return sorted(scanner.violations)
