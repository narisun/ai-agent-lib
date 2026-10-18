"""Architecture rules are checked without installing the UI in agent environments."""

import ast
import subprocess
import tomllib
from pathlib import Path

from python_runtime import python_command

PACKAGE = Path(__file__).resolve().parents[1]
ROOT = PACKAGE.parents[1]


def imports(path: Path) -> set[str]:
    result: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_ui_uses_core_contracts_only_and_keeps_frameworks_out_of_domain() -> None:
    for path in (PACKAGE / "src").rglob("*.py"):
        for name in imports(path):
            assert not name.startswith(
                ("langgraph", "langchain", "ai_agent_lib_aws", "ai_agent_lib_cli")
            ), (path, name)
            if name.startswith("ai_agent_lib_core"):
                assert (
                    name.startswith("ai_agent_lib_core.contracts")
                    or (
                        name == "ai_agent_lib_core.kit"
                        and path.name == "catalog.py"
                        and path.parent.name == "adapters"
                    )
                    # Task 7's append-only feedback log is explicitly meant for UI
                    # capture (its own brief lists this package as a consumer); it
                    # has no contracts-package home, so this one module is named
                    # explicitly rather than opening all of `evaluation` to the UI.
                    or (
                        name == "ai_agent_lib_core.evaluation.feedback"
                        and path.name in {"feedback.py", "runtime.py"}
                        and path.parent.name in {"contracts", "integrations"}
                    )
                ), (path, name)
            if path.parent.name == "contracts" and name.startswith("ai_agent_lib_ui"):
                assert name.startswith("ai_agent_lib_ui.contracts"), (path, name)
            if path.parent.name in {"contracts", "application"}:
                assert not name.startswith(
                    (
                        "chainlit",
                        "fastapi",
                        "starlette",
                        "sqlalchemy",
                        "ai_agent_lib_ui.integrations",
                        "ai_agent_lib_ui.adapters",
                    )
                ), (path, name)


def test_agent_packages_do_not_depend_on_ui() -> None:
    for distribution in ("ai-agent-lib-core", "ai-agent-lib-aws", "ai-agent-lib-cli"):
        package = ROOT / "packages" / distribution
        metadata = tomllib.loads((package / "pyproject.toml").read_text())
        assert "ai-agent-lib-ui" not in str(metadata["project"])
        for path in (package / "src").rglob("*.py"):
            assert not any(name.startswith("ai_agent_lib_ui") for name in imports(path)), path


def test_importing_ui_does_not_load_agent_or_browser_frameworks() -> None:
    script = (
        "import sys; import ai_agent_lib_ui.contracts.ports; "
        "import ai_agent_lib_ui.application.question_answer; "
        "blocked = {'chainlit','langgraph','langchain_core','mcp','starlette','boto3'}; "
        "assert not any(n.split('.')[0] in blocked for n in sys.modules)"
    )
    result = subprocess.run(  # noqa: S603 - fixed script in this environment's interpreter
        python_command("-c", script), capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
