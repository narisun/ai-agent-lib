"""Adding an MCP server, without the command line around it."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from ai_agent_lib_cli.create_mcp import SAMPLE_DESCRIPTION, create_mcp, default_description
from ai_agent_lib_cli.dataplan import sample_plan
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.readers import Proposal
from ai_agent_lib_cli.scaffold import Scaffolder
from ai_agent_lib_cli.shared import read_servers
from ai_agent_lib_cli.testing import as_written, fixed_pins
from ai_agent_lib_cli.workspace import LibrarySource, McpAnswers, WorkspaceAnswers, load_answers

ANSWERS = WorkspaceAnswers("demo", "demo-team", LibrarySource(kind="index", version=">=0.1"))
PINS = {"hello.greet": "ab" * 32, "hello.people_by_team": "cd" * 32}


def _failing(service: Path, package: str) -> dict[str, str]:
    raise CliError(f"the tools of {package} could not be pinned: no module named mcp")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    Scaffolder(formatter=as_written, pin_reader=fixed_pins(PINS)).init(tmp_path, ANSWERS)
    return tmp_path


def test_the_sample_server_is_created_and_pinned(workspace: Path) -> None:
    scaffolder = Scaffolder(formatter=as_written, pin_reader=fixed_pins(PINS))
    created = create_mcp(
        scaffolder,
        workspace,
        ANSWERS,
        name="hello-mcp",
        server_id="hello",
        description=default_description(None),
        port=8100,
        proposal=None,
        pin=True,
        force=False,
    )
    assert created.server == McpAnswers("hello-mcp", "hello", SAMPLE_DESCRIPTION, 8100)
    assert created.pinned
    assert created.not_pinned is None
    assert created.headline == "MCP server hello-mcp (registry ID hello) in mcp-servers/hello-mcp"
    assert created.pin_note == (
        "  pinned   the input schema of each tool in registry/mcp-tools.yaml"
    )
    assert created.next_steps == [
        "python -m ai_agent_lib_cli install",
        "python -m pytest mcp-servers/hello-mcp",
        "hello-mcp",
    ]
    assert workspace / "mcp-servers/hello-mcp/src/hello_mcp/server.py" in created.report.created
    assert [tool.schema_sha256 for tool in read_servers(workspace)[0].tools] == list(PINS.values())
    assert load_answers(workspace).mcp_servers == (created.server,)


def test_a_server_whose_tools_cannot_be_pinned_is_still_created(workspace: Path) -> None:
    created = create_mcp(
        Scaffolder(formatter=as_written, pin_reader=_failing),
        workspace,
        ANSWERS,
        name="hello-mcp",
        server_id="hello",
        description="Says hello.",
        port=8100,
        proposal=None,
        pin=True,
        force=False,
    )
    assert not created.pinned
    assert created.not_pinned == "the tools of hello_mcp could not be pinned: no module named mcp"
    assert created.pin_note == (
        "  not pinned: the tools of hello_mcp could not be pinned: no module named mcp\n"
        "  Pin later with: agentcraft2 registry pin hello"
    )
    assert (workspace / "mcp-servers/hello-mcp/src/hello_mcp/server.py").is_file()
    assert all(tool.schema_sha256 is None for tool in read_servers(workspace)[0].tools)


def test_declining_the_pin_asks_the_server_nothing(workspace: Path) -> None:
    created = create_mcp(
        Scaffolder(formatter=as_written, pin_reader=_failing),
        workspace,
        ANSWERS,
        name="hello-mcp",
        server_id="hello",
        description="Says hello.",
        port=8100,
        proposal=None,
        pin=False,
        force=False,
    )
    assert (created.pinned, created.not_pinned, created.pin_note) == (False, None, None)


def test_a_proposal_gives_the_server_its_plan_its_data_and_its_extra_files(
    workspace: Path, tmp_path: Path
) -> None:
    data = tmp_path / "source.csv"
    data.write_text("id,team\n1,a\n", encoding="utf-8")
    proposal = Proposal(
        plan=sample_plan(),
        summary="Read one file.",
        description="People, from your own file.",
        data_files={"people.csv": data},
        extra_files={PurePosixPath("NOTES.md"): "# notes\n"},
    )
    created = create_mcp(
        Scaffolder(formatter=as_written, pin_reader=fixed_pins(PINS)),
        workspace,
        ANSWERS,
        name="people",
        server_id="people",
        description=default_description(proposal),
        port=8100,
        proposal=proposal,
        pin=False,
        force=False,
    )
    assert created.server.description == "People, from your own file."
    assert created.server.data == sample_plan()
    folder = workspace / "mcp-servers/people"
    assert (folder / "data/people.csv").read_text(encoding="utf-8") == "id,team\n1,a\n"
    assert (folder / "NOTES.md").read_text(encoding="utf-8") == "# notes\n"


def test_a_taken_name_is_refused_before_anything_is_written(workspace: Path) -> None:
    scaffolder = Scaffolder(formatter=as_written, pin_reader=fixed_pins(PINS))
    answers = ANSWERS.with_mcp_server(McpAnswers("hello-mcp", "hello", "Says hello.", 8100))
    with pytest.raises(CliError, match="belongs to hello-mcp"):
        create_mcp(
            scaffolder,
            workspace,
            answers,
            name="other",
            server_id="hello",
            description="Says hello.",
            port=8101,
            proposal=None,
            pin=False,
            force=False,
        )
    assert not (workspace / "mcp-servers/other").exists()
    assert load_answers(workspace).mcp_servers == ()
