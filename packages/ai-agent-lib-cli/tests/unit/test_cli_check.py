"""'agentcraft2 check': the same gate on a developer's machine and in CI."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.render import TemplateRenderer
from ai_agent_lib_cli.testing import toolbox_for_tests
from ai_agent_lib_cli.toolbox import Toolbox

PINS = {"hello.greet": "ab" * 32, "hello.people_by_team": "cd" * 32}


class Tools:
    """Stands in for ruff, mypy and pytest, and remembers what was run."""

    def __init__(self, failing: Sequence[str] = ()) -> None:
        self.failing = set(failing)
        self.ran: list[tuple[str, tuple[str, ...], Path]] = []

    def __call__(self, module: str, arguments: Sequence[str], folder: Path) -> int:
        self.ran.append((module, tuple(arguments), folder))
        return 1 if module in self.failing else 0


def toolbox(tools: Tools) -> Toolbox:
    return toolbox_for_tests(pins=PINS, run_tool=tools)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    tools = toolbox(Tools())
    assert main(["init", "demo", "--dir", str(tmp_path), "--owner", "o"], toolbox=tools) == 0
    root = tmp_path / "demo"
    assert main(["new", "mcp", "hello-mcp", "--workspace", str(root)], toolbox=tools) == 0
    return root


def test_every_check_runs_in_the_workspace_and_the_policy_samples_pass(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tools = Tools()
    assert main(["check", "--workspace", str(workspace)], toolbox=toolbox(tools)) == 0
    assert [(module, arguments) for module, arguments, _ in tools.ran] == [
        ("ruff", ("check", ".")),
        ("ruff", ("format", "--check", ".")),
        ("mypy", ()),
        ("pytest", ("-q",)),
    ]
    assert {folder for _, _, folder in tools.ran} == {workspace}
    out = capsys.readouterr().out
    assert "samples came out as expected" in out
    assert out.rstrip().endswith("Every check passed.")


def test_a_failed_check_stops_the_rest_and_says_which(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tools = Tools(failing=["mypy"])
    assert main(["check", "--workspace", str(workspace)], toolbox=toolbox(tools)) == 1
    assert [module for module, _, _ in tools.ran] == ["ruff", "ruff", "mypy"]
    captured = capsys.readouterr()
    assert "  FAIL  types" in captured.out
    assert "1 check failed: types" in captured.err


def test_keep_going_runs_every_check(workspace: Path, capsys: pytest.CaptureFixture[str]) -> None:
    tools = Tools(failing=["ruff"])
    command = ["check", "--keep-going", "--workspace", str(workspace)]
    assert main(command, toolbox=toolbox(tools)) == 1
    assert [module for module, _, _ in tools.ran] == ["ruff", "ruff", "mypy", "pytest"]
    assert "2 checks failed: lint, format" in capsys.readouterr().err


def test_a_new_workspace_has_a_ci_workflow_and_blocks_the_network_in_tests() -> None:
    files = TemplateRenderer().render(
        "workspace",
        {
            "name": "demo",
            "owner": "o",
            "library": {"kind": "index", "path": "", "version": ">=0.1"},
        },
    )
    by_name = {path.as_posix(): text for path, text in files.items()}
    workflow = by_name[".github/workflows/check.yml"]
    assert "runs-on: ${{ matrix.os }}" in workflow
    assert "os: [ubuntu-latest, windows-latest]" in workflow
    assert "python -m ai_agent_lib_cli check" in workflow
    pyproject = by_name["pyproject.toml"]
    assert '"pytest-socket>=0.7"' in pyproject
    assert '"--allow-hosts=127.0.0.1,::1"' in pyproject
    assert '"-m", "not integration and not eval"' in pyproject


def test_eval_runs_the_tests_marked_eval_of_one_service(workspace: Path) -> None:
    tools = Tools()
    assert (
        main(["new", "agent", "helper", "--workspace", str(workspace)], toolbox=toolbox(tools)) == 0
    )
    assert (workspace / "agents/helper/evals/cases.jsonl").is_file()
    assert (workspace / "agents/helper/tests/test_helper_eval.py").is_file()
    assert main(["eval", "helper", "--workspace", str(workspace)], toolbox=toolbox(tools)) == 0
    assert tools.ran[-1] == ("pytest", ("-m", "eval", "-q", "-rs", "agents/helper"), workspace)


def test_eval_says_so_when_there_are_none(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_tests(module: str, arguments: Sequence[str], folder: Path) -> int:
        return 5

    tools = toolbox_for_tests(pins=PINS, run_tool=no_tests)
    assert main(["eval", "hello-mcp", "--workspace", str(workspace)], toolbox=tools) == 1
    err = capsys.readouterr().err
    assert "there are no evals in mcp-servers/hello-mcp" in err
    assert "@pytest.mark.eval" in err


def test_eval_accept_promotes_the_report_to_the_baseline(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tools = Tools()
    assert (
        main(["new", "agent", "helper", "--workspace", str(workspace)], toolbox=toolbox(tools)) == 0
    )
    report = workspace / "agents" / "helper" / ".agentcraft2" / "eval-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        '{"schema": "agentcraft2.eval-report/v1", "means": {"m": 1.0}, '
        '"results": [], "metadata": {}}',
        encoding="utf-8",
    )
    assert (
        main(["eval", "helper", "--accept", "--workspace", str(workspace)], toolbox=toolbox(tools))
        == 0
    )
    baseline = workspace / "agents" / "helper" / "evals" / "baseline.json"
    assert json.loads(baseline.read_text(encoding="utf-8"))["means"] == {"m": 1.0}
    assert "baseline accepted: agents/helper/evals/baseline.json" in capsys.readouterr().out


def test_eval_accept_without_a_service_is_refused(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tools = Tools()
    assert main(["eval", "--accept", "--workspace", str(workspace)], toolbox=toolbox(tools)) != 0
    assert "--accept needs a service" in capsys.readouterr().err


def test_eval_accept_without_a_report_names_the_fix(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tools = Tools()
    assert (
        main(["new", "agent", "helper", "--workspace", str(workspace)], toolbox=toolbox(tools)) == 0
    )
    assert (
        main(["eval", "helper", "--accept", "--workspace", str(workspace)], toolbox=toolbox(tools))
        != 0
    )
    assert "eval-report.json" in capsys.readouterr().err


def _write_eval_report(workspace: Path) -> None:
    report = workspace / "agents" / "helper" / ".agentcraft2" / "eval-report.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        '{"schema": "agentcraft2.eval-report/v1", "means": {"m": 1.0}, '
        '"results": [], "metadata": {}}',
        encoding="utf-8",
    )


def _accept_marker(workspace: Path) -> Path:
    return workspace / "agents" / "helper" / ".agentcraft2" / "eval-accept.flag"


def test_eval_accept_leaves_a_marker_during_the_pytest_run_and_removes_it(
    workspace: Path,
) -> None:
    tools = Tools()
    assert (
        main(["new", "agent", "helper", "--workspace", str(workspace)], toolbox=toolbox(tools)) == 0
    )
    _write_eval_report(workspace)
    marker = _accept_marker(workspace)
    captured: dict[str, bool] = {}

    def run_tool(module: str, arguments: Sequence[str], folder: Path) -> int:
        captured["during"] = marker.exists()
        return 0

    assert not marker.exists()
    assert (
        main(
            ["eval", "helper", "--accept", "--workspace", str(workspace)],
            toolbox=toolbox_for_tests(pins=PINS, run_tool=run_tool),
        )
        == 0
    )
    assert captured["during"] is True
    assert not marker.exists()


def test_eval_without_accept_leaves_no_marker(workspace: Path) -> None:
    tools = Tools()
    assert (
        main(["new", "agent", "helper", "--workspace", str(workspace)], toolbox=toolbox(tools)) == 0
    )
    marker = _accept_marker(workspace)
    captured: dict[str, bool] = {}

    def run_tool(module: str, arguments: Sequence[str], folder: Path) -> int:
        captured["during"] = marker.exists()
        return 0

    assert (
        main(
            ["eval", "helper", "--workspace", str(workspace)],
            toolbox=toolbox_for_tests(pins=PINS, run_tool=run_tool),
        )
        == 0
    )
    assert captured["during"] is False
    assert not marker.exists()


def test_eval_accept_removes_the_marker_even_when_the_evals_fail(workspace: Path) -> None:
    tools = Tools()
    assert (
        main(["new", "agent", "helper", "--workspace", str(workspace)], toolbox=toolbox(tools)) == 0
    )
    marker = _accept_marker(workspace)
    failing = Tools(failing=["pytest"])
    assert (
        main(
            ["eval", "helper", "--accept", "--workspace", str(workspace)], toolbox=toolbox(failing)
        )
        != 0
    )
    assert not marker.exists()
