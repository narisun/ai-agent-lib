"""Architecture rules are checked without installing the UI in agent environments."""

import ast
import subprocess
import tomllib
from pathlib import Path

import pytest
from python_runtime import python_command

pytestmark = pytest.mark.slow

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
                ("langgraph", "langchain", "agentcraft2_aws", "agentcraft2_cli")
            ), (path, name)
            if name.startswith("agentcraft2_core"):
                assert (
                    name.startswith("agentcraft2_core.contracts")
                    or (
                        name == "agentcraft2_core.kit"
                        and path.name == "catalog.py"
                        and path.parent.name == "adapters"
                    )
                    # Task 7's append-only feedback log is explicitly meant for UI
                    # capture (its own brief lists this package as a consumer); it
                    # has no contracts-package home, so this one module is named
                    # explicitly rather than opening all of `evaluation` to the UI.
                    or (
                        name == "agentcraft2_core.evaluation.feedback"
                        and path.name in {"feedback.py", "runtime.py"}
                        and path.parent.name in {"contracts", "integrations"}
                    )
                    # Task 10's interaction exporter joins saved conversations with
                    # viewer feedback for offline/online evals; both evaluation
                    # modules it needs are named explicitly for this one file only.
                    or (
                        name
                        in {
                            "agentcraft2_core.evaluation.feedback",
                            "agentcraft2_core.evaluation.online",
                        }
                        and path.name == "interactions.py"
                        and path.parent.name == "adapters"
                    )
                ), (path, name)
            if path.parent.name == "contracts" and name.startswith("agentcraft2_ui"):
                assert name.startswith("agentcraft2_ui.contracts"), (path, name)
            if path.parent.name in {"contracts", "application"}:
                assert not name.startswith(
                    (
                        "chainlit",
                        "fastapi",
                        "starlette",
                        "sqlalchemy",
                        "agentcraft2_ui.integrations",
                        "agentcraft2_ui.adapters",
                    )
                ), (path, name)


def test_agent_packages_do_not_depend_on_ui() -> None:
    for distribution in ("agentcraft2-core", "agentcraft2-aws", "agentcraft2-cli"):
        package = ROOT / "packages" / distribution
        metadata = tomllib.loads((package / "pyproject.toml").read_text())
        assert "agentcraft2-ui" not in str(metadata["project"])
        for path in (package / "src").rglob("*.py"):
            assert not any(name.startswith("agentcraft2_ui") for name in imports(path)), path


def test_importing_ui_does_not_load_agent_or_browser_frameworks() -> None:
    script = (
        "import sys; import agentcraft2_ui.contracts.ports; "
        "import agentcraft2_ui.application.question_answer; "
        "blocked = {'chainlit','langgraph','langchain_core','mcp','starlette','boto3'}; "
        "assert not any(n.split('.')[0] in blocked for n in sys.modules)"
    )
    result = subprocess.run(  # noqa: S603 - fixed script in this environment's interpreter
        python_command("-c", script), capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
