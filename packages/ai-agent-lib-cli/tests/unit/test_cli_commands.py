"""The agentlib command line: what each command writes, and how it fails."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_agent_lib_cli import __version__
from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.scaffold import Scaffolder
from ai_agent_lib_cli.shared import read_agents, read_servers
from ai_agent_lib_cli.testing import as_written, toolbox_for_tests
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_cli.workspace import (
    AgentAnswers,
    LibrarySource,
    McpAnswers,
    WorkspaceAnswers,
    load_answers,
)
from ai_agent_lib_core.adapters import FileRegistryOptions, FileRegistrySource

RULES = Path("policies/agentlib/rules/data.yaml")


# Generated code is left unformatted here: these tests are about which files are
# written. Formatting has its own test, and the generated-workspace tests run it for real.
TOOLS = toolbox_for_tests()


def run(*arguments: str | Path, tools: Toolbox = TOOLS) -> int:
    return main([str(argument) for argument in arguments], toolbox=tools)


def tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def rule_ids(root: Path) -> list[str]:
    document = yaml.safe_load((root / RULES).read_text(encoding="utf-8"))
    return [rule["id"] for rule in document["rules"]]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    assert run("init", "demo", "--dir", tmp_path, "--owner", "demo-team") == 0
    return tmp_path / "demo"


# ------------------------------------------------------------ the command itself


def test_the_version_and_the_help_exit_with_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--version") == 0
    assert capsys.readouterr().out == f"agentlib, version {__version__}\n"
    assert run("--help") == 0
    listed = capsys.readouterr().out
    for command in ("init", "new", "link", "registry"):
        assert command in listed
    assert run("new", "-h") == 0


@pytest.mark.parametrize(
    "arguments",
    [
        ["frobnicate"],
        ["init"],
        ["init", "demo", "--no-such-option"],
        ["new", "agent"],
        ["new", "agent", "x", "--model", "gpt"],
        ["new", "mcp", "x", "--port", "80"],
        ["init", "demo", "--lib-path", ".", "--lib-version", ">=0.1"],
    ],
)
def test_a_wrong_command_line_exits_with_two_and_says_why(
    arguments: list[str], capsys: pytest.CaptureFixture[str], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    monkeypatch.chdir(tmp_path)
    assert run(*arguments) == 2
    captured = capsys.readouterr()
    assert captured.err.strip()
    assert "Traceback" not in captured.err


def test_a_command_that_cannot_be_done_exits_with_one_and_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("new", "agent", "hello-agent", "--workspace", tmp_path) == 1
    error = capsys.readouterr().err
    assert error.startswith("agentlib: error: ")
    assert "not inside a workspace" in error
    assert len(error.strip().splitlines()) == 1
    assert run("init", "Not_A_Name", "--dir", tmp_path) == 1
    assert run("init", "demo", "--dir", tmp_path, "--lib-path", tmp_path) == 1
    assert "not a checkout of the library" in capsys.readouterr().err
    assert not (tmp_path / "demo").exists()


# -------------------------------------------------------------------- workspace


def test_init_creates_a_workspace_that_holds_nothing_yet(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("init", "demo", "--dir", tmp_path, "--owner", "demo-team") == 0
    workspace = tmp_path / "demo"
    out = capsys.readouterr().out
    assert "created  pyproject.toml" in out
    assert "python -m ai_agent_lib_cli new mcp hello-mcp" in out
    files = tree(workspace)
    assert sorted(files) == [
        ".github/workflows/check.yml",
        ".gitignore",
        "README.md",
        "agentlib.lock",
        "agentlib.toml",
        "agents/.gitkeep",
        "mcp-servers/.gitkeep",
        "policies/.manifest",
        "policies/agentlib/rules/data.yaml",
        "pyproject.toml",
        "registry/agents.yaml",
        "registry/mcp-tools.yaml",
        "tests/policy-samples.yaml",
        "tests/test_workspace.py",
    ]
    answers = load_answers(workspace)
    assert (answers.name, answers.owner) == ("demo", "demo-team")
    # The library is installed from the checkout this command runs from.
    assert answers.library.kind == "path"
    assert f'path = "{answers.library.path}/packages/ai-agent-lib-core"' in files["pyproject.toml"]
    assert read_agents(workspace) == ()
    assert rule_ids(workspace) == []
    assert ".env\n" in files[".gitignore"]


def test_init_can_name_a_package_index_instead_of_a_checkout(tmp_path: Path) -> None:
    assert run("init", "demo", "--dir", tmp_path, "--lib-version", ">=0.1") == 0
    project = (tmp_path / "demo" / "pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.uv.sources]" not in project
    assert '"ai-agent-lib-cli>=0.1"' in project
    assert run("new", "agent", "hello-agent", "--workspace", tmp_path / "demo") == 0
    member = (tmp_path / "demo" / "agents/hello-agent/pyproject.toml").read_text(encoding="utf-8")
    assert '"ai-agent-lib-core[jwt,mcp,otel,serve]>=0.1"' in member


def test_init_again_changes_nothing_and_keeps_what_was_added(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("new", "agent", "hello-agent", "--workspace", workspace) == 0
    before = tree(workspace)
    capsys.readouterr()
    assert run("init", "demo", "--dir", workspace.parent, "--owner", "demo-team") == 0
    assert "nothing to do" in capsys.readouterr().out
    assert tree(workspace) == before


# ---------------------------------------------------------------------- services


def test_new_mcp_adds_a_server_registers_it_and_adds_its_rules(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("new", "mcp", "hello-mcp", "--no-pin", "--workspace", workspace) == 0
    out = capsys.readouterr().out
    assert "MCP server hello-mcp (registry ID hello)" in out
    assert "python -m pytest mcp-servers/hello-mcp" in out
    files = tree(workspace)
    service = "mcp-servers/hello-mcp/"
    assert {name.removeprefix(service) for name in files if name.startswith(service)} == {
        ".env",
        ".env.example",
        "README.md",
        "data/people.csv",
        "pyproject.toml",
        "queries/people_by_team.sql",
        "src/hello_mcp/__init__.py",
        "src/hello_mcp/__main__.py",
        "src/hello_mcp/pins.py",
        "src/hello_mcp/py.typed",
        "src/hello_mcp/server.py",
        "src/hello_mcp/settings.py",
        "tests/test_hello_mcp.py",
    }
    assert files[service + ".env"] == files[service + ".env.example"]
    assert 'SERVER_ID = "hello"' in files[service + "src/hello_mcp/server.py"]
    assert '@server.tool(name="hello.people_by_team")' in files[service + "src/hello_mcp/server.py"]

    (server,) = read_servers(workspace)
    assert (server.id, server.audience, server.owner) == ("hello", "hello-mcp", "demo-team")
    assert server.url == "http://127.0.0.1:8100/mcp"
    assert [tool.name for tool in server.tools] == ["hello.greet", "hello.people_by_team"]
    assert all(tool.schema_sha256 is None for tool in server.tools)
    assert rule_ids(workspace) == [
        "hello-staff-call-the-tools",
        "hello-managers-see-everything",
        "hello-analysts-see-no-email",
    ]
    assert load_answers(workspace).mcp_servers == (
        McpAnswers(
            "hello-mcp", "hello", "A greeting and a people directory, over sample data.", 8100
        ),
    )


def test_new_agent_adds_an_agent_linked_to_the_servers_it_names(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("new", "mcp", "hello-mcp", "--no-pin", "--workspace", workspace) == 0
    capsys.readouterr()
    assert (
        run("new", "agent", "hello-agent", "--mcp", "hello-mcp", "--description", "Says hello.",
            "--workspace", workspace) == 0
    )  # fmt: skip
    assert "Agent hello-agent in agents/hello-agent" in capsys.readouterr().out
    files = tree(workspace)
    service = "agents/hello-agent/"
    assert {name.removeprefix(service) for name in files if name.startswith(service)} == {
        ".env",
        ".env.example",
        "README.md",
        "evals/cases.jsonl",
        "pyproject.toml",
        "src/hello_agent/__init__.py",
        "src/hello_agent/__main__.py",
        "src/hello_agent/graph.py",
        "src/hello_agent/py.typed",
        "src/hello_agent/service.py",
        "src/hello_agent/settings.py",
        "src/hello_agent/tools.py",
        "tests/test_hello_agent.py",
        "tests/test_hello_agent_as_configured.py",
        "tests/test_hello_agent_eval.py",
        "tests/test_hello_agent_service.py",
    }
    assert "tests/test_hello_agent_with_hello_mcp.py" in files
    assert 'description = "Says hello."' in files[service + "pyproject.toml"]
    (agent,) = read_agents(workspace)
    assert (agent.id, agent.mcp_servers, agent.owner) == ("hello-agent", ("hello",), "demo-team")
    assert rule_ids(workspace)[3:] == [
        "hello-agent-uses-its-models",
        "hello-agent-calls-its-own-tools",
        "hello-agent-calls-hello",
    ]
    # The registries the services will load are consistent.
    FileRegistrySource(
        FileRegistryOptions(
            agents_path=workspace / "registry/agents.yaml",
            tools_path=workspace / "registry/mcp-tools.yaml",
        )
    )


def test_services_get_ports_that_do_not_collide_and_keep_them(workspace: Path) -> None:
    for command in (
        ["new", "mcp", "one-mcp", "--no-pin"],
        ["new", "mcp", "two-mcp", "--no-pin", "--server-id", "second"],
        ["new", "agent", "one-agent"],
        ["new", "agent", "two-agent", "--port", "9000", "--model", "anthropic"],
        ["new", "mcp", "one-mcp", "--no-pin"],
    ):
        assert run(*command, "--workspace", workspace) == 0
    answers = load_answers(workspace)
    assert {s.name: s.port for s in answers.mcp_servers} == {"one-mcp": 8100, "two-mcp": 8101}
    assert {a.name: a.port for a in answers.agents} == {"one-agent": 8000, "two-agent": 9000}
    assert answers.mcp_server("second") is not None
    assert answers.agent("two-agent").model_provider == "anthropic"  # type: ignore[union-attr]


def test_link_lets_an_agent_call_a_server_and_is_safe_to_repeat(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("new", "agent", "hello-agent", "--workspace", workspace) == 0
    assert run("new", "mcp", "hello-mcp", "--no-pin", "--workspace", workspace) == 0
    assert not (workspace / "tests/test_hello_agent_with_hello_mcp.py").exists()
    assert run("link", "hello-agent", "hello", "--workspace", workspace) == 0
    assert "hello-agent may now call the tools of hello" in capsys.readouterr().out
    linked = tree(workspace)
    assert read_agents(workspace)[0].mcp_servers == ("hello",)
    assert load_answers(workspace).agents[0].mcp_servers == ("hello",)
    assert rule_ids(workspace).count("hello-agent-calls-hello") == 1
    assert "tests/test_hello_agent_with_hello_mcp.py" in linked

    assert run("link", "hello-agent", "hello-mcp", "--workspace", workspace) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert tree(workspace) == linked


def test_link_brings_the_agents_readme_up_to_date_unless_the_developer_changed_it(
    workspace: Path,
) -> None:
    for agent in ("hello-agent", "their-agent"):
        assert run("new", "agent", agent, "--workspace", workspace) == 0
    assert run("new", "mcp", "hello-mcp", "--no-pin", "--workspace", workspace) == 0
    theirs = workspace / "agents/their-agent/README.md"
    theirs.write_text("# Ours now\n", encoding="utf-8")

    for agent in ("hello-agent", "their-agent"):
        assert run("link", agent, "hello", "--workspace", workspace) == 0

    readme = (workspace / "agents/hello-agent/README.md").read_text(encoding="utf-8")
    assert "hello-mcp" in readme
    assert theirs.read_text(encoding="utf-8") == "# Ours now\n"
    # The record follows, so a later update has nothing left to do for it.
    before = tree(workspace)
    assert run("update", "--workspace", workspace) == 0
    assert tree(workspace) == before


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["new", "agent", "hello-agent", "--mcp", "nowhere"], "no MCP server called 'nowhere'"),
        (["link", "hello-agent", "nowhere"], "no MCP server called 'nowhere'"),
        (["link", "nobody", "hello"], "no agent called 'nobody'"),
        (["new", "mcp", "hello-agent"], "already used by another service"),
        (["new", "agent", "hello-mcp"], "already used by another service"),
        (["new", "mcp", "other-mcp", "--server-id", "hello"], "belongs to hello-mcp"),
        (["new", "mcp", "Bad_Name"], "is not usable"),
        (["registry", "pin", "nowhere"], "no MCP server called 'nowhere'"),
    ],
)
def test_a_command_that_does_not_fit_the_workspace_changes_nothing(
    workspace: Path, capsys: pytest.CaptureFixture[str], arguments: list[str], message: str
) -> None:
    assert run("new", "mcp", "hello-mcp", "--no-pin", "--workspace", workspace) == 0
    if arguments[:2] != ["new", "agent"] or arguments[2] != "hello-agent":
        assert run("new", "agent", "hello-agent", "--workspace", workspace) == 0
    before = tree(workspace)
    capsys.readouterr()
    assert run(*arguments, "--workspace", workspace) == 1
    assert message in capsys.readouterr().err
    assert tree(workspace) == before


# ------------------------------------------------------------ the developer's work


def test_a_file_the_developer_changed_is_never_overwritten_without_force(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("new", "agent", "hello-agent", "--workspace", workspace) == 0
    tools = workspace / "agents/hello-agent/src/hello_agent/tools.py"
    tools.write_text(tools.read_text(encoding="utf-8") + "\n# my change\n", encoding="utf-8")
    env = workspace / "agents/hello-agent/.env"
    env.write_text("# my own settings\n", encoding="utf-8")
    before = tree(workspace)
    capsys.readouterr()

    assert run("new", "agent", "hello-agent", "--workspace", workspace) == 1
    error = capsys.readouterr().err
    assert "agents/hello-agent/src/hello_agent/tools.py" in error
    assert "Nothing was changed" in error
    assert tree(workspace) == before

    assert run("new", "agent", "hello-agent", "--force", "--workspace", workspace) == 0
    assert "# my change" not in tools.read_text(encoding="utf-8")
    # The developer's own settings are theirs even then.
    assert env.read_text(encoding="utf-8") == "# my own settings\n"


def test_rules_and_registry_entries_the_developer_edited_are_kept(workspace: Path) -> None:
    assert run("new", "mcp", "hello-mcp", "--no-pin", "--workspace", workspace) == 0
    rules = workspace / RULES
    edited = rules.read_text(encoding="utf-8").replace(
        "roles: [analyst, manager]", "roles: [analyst, manager, auditor]  # audit asked for this"
    )
    rules.write_text(edited, encoding="utf-8")
    registry = workspace / "registry/mcp-tools.yaml"
    registry.write_text(
        registry.read_text(encoding="utf-8").replace("A greeting for a person.", "Says hello."),
        encoding="utf-8",
    )

    assert run("new", "agent", "hello-agent", "--mcp", "hello", "--workspace", workspace) == 0
    assert run("new", "mcp", "hello-mcp", "--no-pin", "--workspace", workspace) == 0
    after = rules.read_text(encoding="utf-8")
    assert after.startswith(edited.rstrip("\n"))
    assert "# audit asked for this" in after
    assert read_servers(workspace)[0].tools[0].description == "Says hello."


# --------------------------------------------------------------------- pins, replay


def test_the_pin_command_writes_what_the_server_reports(workspace: Path) -> None:
    assert run("new", "mcp", "hello-mcp", "--no-pin", "--workspace", workspace) == 0
    asked: list[tuple[str, str]] = []

    def reader(service: Path, package: str) -> dict[str, str]:
        asked.append((service.relative_to(workspace).as_posix(), package))
        return {"hello.greet": "ab" * 32, "hello.unknown": "cd" * 32}

    report = Scaffolder(pin_reader=reader, formatter=as_written).pin(workspace, "hello")
    assert asked == [("mcp-servers/hello-mcp", "hello_mcp")]
    assert [path.name for path in report.updated] == ["mcp-tools.yaml"]
    greet, people = read_servers(workspace)[0].tools
    assert (greet.schema_sha256, people.schema_sha256) == ("ab" * 32, None)


def test_a_workspace_is_generated_again_from_its_answers_alone(tmp_path: Path) -> None:
    def reader(service: Path, package: str) -> dict[str, str]:
        return {"hello.greet": "ab" * 32, "hello.people_by_team": "cd" * 32}

    answers = WorkspaceAnswers(
        name="demo",
        owner="demo-team",
        library=LibrarySource(kind="index", version=">=0.1"),
        agents=(
            AgentAnswers("hello-agent", "Greets people.", "fake", 8000, ("hello",)),
            AgentAnswers("other-agent", "Does other things.", "bedrock", 8001),
        ),
        mcp_servers=(McpAnswers("hello-mcp", "hello", "A directory.", 8100),),
    )
    first, second = tmp_path / "first", tmp_path / "second"
    Scaffolder(pin_reader=reader, formatter=as_written).init(first, answers)
    assert load_answers(first) == answers

    Scaffolder(pin_reader=reader, formatter=as_written).init(second, load_answers(first))
    assert tree(second) == tree(first)
    assert "tests/test_hello_agent_with_hello_mcp.py" in tree(first)
    assert "tests/test_other_agent_with_hello_mcp.py" not in tree(first)


def test_init_from_an_answers_file_on_the_command_line(
    workspace: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("new", "agent", "hello-agent", "--workspace", workspace) == 0
    copy = tmp_path / "copies"
    copy.mkdir()
    capsys.readouterr()
    assert run("init", "--answers", workspace / "agentlib.toml", "--dir", copy) == 0
    assert "created  agents/hello-agent/src/hello_agent/graph.py" in capsys.readouterr().out
    assert tree(copy / "demo") == tree(workspace)
    assert (
        run("init", "renamed", "--answers", workspace / "agentlib.toml", "--dir", copy,
            "--lib-version", ">=0.2") == 0
    )  # fmt: skip
    assert load_answers(copy / "renamed").library == LibrarySource(kind="index", version=">=0.2")
    bad = tmp_path / "bad.toml"
    bad.write_text("nothing = true\n", encoding="utf-8")
    assert run("init", "--answers", bad, "--dir", copy) == 1


# ------------------------------------------------------------- pins and prompts


def pinned(pins: dict[str, str]) -> Toolbox:
    """A toolbox whose servers report whatever ``pins`` holds when they are asked."""
    return toolbox_for_tests(read_pins=lambda service, package: dict(pins))


def test_new_mcp_pins_the_tools_and_registry_pin_does_it_again(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pins = {"hello.greet": "ab" * 32, "hello.people_by_team": "cd" * 32}
    assert run("new", "mcp", "hello-mcp", "--workspace", workspace, tools=pinned(pins)) == 0
    assert "pinned   the input schema of each tool" in capsys.readouterr().out
    assert [tool.schema_sha256 for tool in read_servers(workspace)[0].tools] == list(pins.values())

    pins["hello.greet"] = "ef" * 32
    assert run("registry", "pin", "hello", "--workspace", workspace, tools=pinned(pins)) == 0
    assert "updated  registry/mcp-tools.yaml" in capsys.readouterr().out
    assert read_servers(workspace)[0].tools[0].schema_sha256 == "ef" * 32


def test_a_server_that_cannot_be_pinned_yet_is_still_created(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(service: Path, package: str) -> dict[str, str]:
        raise CliError("the tools of hello_mcp could not be pinned: no module named mcp")

    tools = toolbox_for_tests(read_pins=failing)
    assert run("new", "mcp", "hello-mcp", "--workspace", workspace, tools=tools) == 0
    captured = capsys.readouterr()
    assert "not pinned: the tools of hello_mcp could not be pinned" in captured.err
    assert "agentlib registry pin hello" in captured.err
    assert (workspace / "mcp-servers/hello-mcp/src/hello_mcp/server.py").is_file()
    assert run("registry", "pin", "hello", "--workspace", workspace, tools=tools) == 1


def test_at_a_terminal_the_missing_answers_are_asked_for_one_at_a_time(tmp_path: Path) -> None:
    asked: list[str] = []
    replies = {"Which team": "payments-team", "What does the agent": "Pays.", "Model": "bedrock"}

    def prompt(question: str, default: str) -> str:
        asked.append(question)
        return next(reply for start, reply in replies.items() if question.startswith(start))

    at_a_terminal = toolbox_for_tests(prompt=prompt)
    assert run("init", "demo", "--dir", tmp_path, tools=at_a_terminal) == 0
    demo = tmp_path / "demo"
    assert run("new", "agent", "pay-agent", "--workspace", demo, tools=at_a_terminal) == 0
    # An answer given on the command line is not asked for.
    assert run("new", "agent", "x-agent", "--description", "X.", "--model", "fake",
               "--workspace", demo, tools=at_a_terminal) == 0  # fmt: skip
    assert [question.split()[0] for question in asked] == ["Which", "What", "Model"]
    answers = load_answers(tmp_path / "demo")
    assert answers.owner == "payments-team"
    pay = answers.agent("pay-agent")
    assert pay is not None
    assert (pay.description, pay.model_provider) == ("Pays.", "bedrock")


def test_an_agent_missing_from_the_registry_cannot_be_linked(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("new", "agent", "hello-agent", "--workspace", workspace) == 0
    assert run("new", "mcp", "hello-mcp", "--no-pin", "--workspace", workspace) == 0
    registry = workspace / "registry/agents.yaml"
    registry.write_text("schema: agentlib.registry/v1\nagents: []\n", encoding="utf-8")
    capsys.readouterr()
    assert run("link", "hello-agent", "hello", "--workspace", workspace) == 1
    assert "is not in registry/agents.yaml" in capsys.readouterr().err
