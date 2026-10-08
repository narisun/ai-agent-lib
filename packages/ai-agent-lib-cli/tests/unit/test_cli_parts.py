"""The parts the commands are built from: names, the workspace file, rendering, shared files."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

import jinja2
import pytest
import yaml

from ai_agent_lib_cli.envfiles import agent_env, mcp_env, variable_names
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.formatting import format_python
from ai_agent_lib_cli.names import check_name, package_name, server_id_for
from ai_agent_lib_cli.render import TemplateRenderer, write_files
from ai_agent_lib_cli.shared import (
    agents_text,
    pinned,
    read_agents,
    read_servers,
    rules_text,
    tools_text,
)
from ai_agent_lib_cli.workspace import (
    WORKSPACE_FILE,
    AgentAnswers,
    LibrarySource,
    McpAnswers,
    WorkspaceAnswers,
    find_workspace,
    load_answers,
)
from ai_agent_lib_cli.yaml_lists import yaml_flow, yaml_scalar
from ai_agent_lib_core.adapters import (
    DuckDbCsvOptions,
    FileRegistryOptions,
    RulesPolicyOptions,
    StaticIdentityOptions,
)
from ai_agent_lib_core.config import ConfigResolver, DotenvConfigSource
from ai_agent_lib_core.contracts import AgentEntry, Section, ServerEntry, ToolEntry

# ---------------------------------------------------------------------- names


@pytest.mark.parametrize("name", ["hello-agent", "a", "claims2-mcp", "x" * 28])
def test_a_name_of_lower_case_words_joined_by_hyphens_is_accepted(name: str) -> None:
    assert check_name("agent", name) == name


@pytest.mark.parametrize(
    "name",
    ["", "Hello", "hello_agent", "-hello", "hello-", "hello--agent", "9lives", "x" * 29,
     "hello agent", "héllo", "test", "mcp", "class", "agentlib"],
)  # fmt: skip
def test_a_name_that_cannot_be_a_folder_a_package_and_an_id_is_refused(name: str) -> None:
    with pytest.raises(CliError, match="name"):
        check_name("agent", name)


def test_names_derived_from_a_service_name() -> None:
    assert package_name("claims-lookup-mcp") == "claims_lookup_mcp"
    assert server_id_for("claims-lookup-mcp") == "claims-lookup"
    assert server_id_for("claims") == "claims"


# ------------------------------------------------------------- workspace file

ANSWERS = WorkspaceAnswers(
    name="demo",
    owner='the "platform" team',
    library=LibrarySource(kind="path", path="C:/work/ai-agent-lib"),
    agents=(AgentAnswers("hello-agent", "Greets people.", "fake", 8000, ("hello",)),),
    mcp_servers=(McpAnswers("hello-mcp", "hello", "A directory.", 8100),),
)


def test_the_workspace_file_round_trips_every_answer() -> None:
    text = ANSWERS.to_toml()
    assert WorkspaceAnswers.from_toml(text) == ANSWERS
    assert WorkspaceAnswers.from_toml(text).to_toml() == text
    index = WorkspaceAnswers("demo", "team", LibrarySource(kind="index", version=">=0.1"))
    assert WorkspaceAnswers.from_toml(index.to_toml()) == index


def test_services_are_kept_in_order_and_replaced_by_name() -> None:
    later = ANSWERS.with_agent(AgentAnswers("a-agent", "First.", "fake", 8001))
    assert [agent.name for agent in later.agents] == ["a-agent", "hello-agent"]
    moved = later.with_mcp_server(McpAnswers("hello-mcp", "hello", "Changed.", 8200))
    assert [server.description for server in moved.mcp_servers] == ["Changed."]
    assert moved.used_ports() == {8000, 8001, 8200}
    assert moved.service_names() == {"a-agent", "hello-agent", "hello-mcp"}
    assert moved.mcp_server("hello") is moved.mcp_server("hello-mcp")
    assert moved.agent("nobody") is None


@pytest.mark.parametrize(
    "text",
    ["not toml =", "[workspace]\nname = 'x'", "[workspace]\nname = 1\nowner='o'\n[library]\n",
     "[workspace]\nname='x'\nowner='o'\n[library]\nsource='path'\n"],
)  # fmt: skip
def test_a_file_that_is_not_a_workspace_file_is_refused(text: str) -> None:
    with pytest.raises(CliError):
        WorkspaceAnswers.from_toml(text)


def test_the_workspace_is_found_from_any_folder_inside_it(tmp_path: Path) -> None:
    (tmp_path / WORKSPACE_FILE).write_text(ANSWERS.to_toml(), encoding="utf-8")
    deep = tmp_path / "agents" / "hello-agent" / "src"
    deep.mkdir(parents=True)
    assert find_workspace(deep) == tmp_path.resolve()
    assert load_answers(tmp_path) == ANSWERS
    with pytest.raises(CliError, match="not inside a workspace"):
        find_workspace(tmp_path.parent)
    with pytest.raises(CliError, match="cannot be read"):
        load_answers(tmp_path / "elsewhere")


def test_a_library_source_names_where_it_installs_from(tmp_path: Path) -> None:
    assert LibrarySource.from_path(tmp_path).path == tmp_path.resolve().as_posix()
    with pytest.raises(CliError, match="path"):
        LibrarySource(kind="path")
    with pytest.raises(CliError, match="version"):
        LibrarySource(kind="index")


# ------------------------------------------------------------------ rendering


def renderer(**templates: str) -> TemplateRenderer:
    return TemplateRenderer(
        jinja2.Environment(
            loader=jinja2.DictLoader(templates),
            undefined=jinja2.StrictUndefined,
            keep_trailing_newline=True,
            autoescape=False,  # noqa: S701 - the output is source code, not HTML
        )
    )


def test_a_template_folder_is_rendered_with_names_in_its_paths() -> None:
    files = renderer(
        **{
            "svc/src/__package__/__init__.py.jinja": '"""{{ name }}"""\n',
            "svc/tests/test___package__.py.jinja": "import {{ package }}\n",
            "svc/dot_env.example.jinja": "X=1\n",
            "svc/notes.txt": "not a template",
            "other/x.jinja": "",
        }
    ).render("svc", {"name": "hello-agent", "package": "hello_agent"})
    assert files == {
        PurePosixPath("src/hello_agent/__init__.py"): '"""hello-agent"""\n',
        PurePosixPath("tests/test_hello_agent.py"): "import hello_agent\n",
        PurePosixPath(".env.example"): "X=1\n",
    }


def test_a_missing_variable_or_template_is_an_error_not_empty_text() -> None:
    with pytest.raises(jinja2.UndefinedError):
        renderer(**{"svc/a.jinja": "{{ missing }}"}).render("svc", {})
    with pytest.raises(CliError, match="no template"):
        renderer().render("svc", {})


def test_the_templates_in_the_package_are_found() -> None:
    files = TemplateRenderer().render(
        "workspace", {"name": "demo", "owner": "team", "library": ANSWERS.library}
    )
    assert PurePosixPath(".gitignore") in files
    assert (
        'path = "C:/work/ai-agent-lib/packages/ai-agent-lib-core"'
        in files[PurePosixPath("pyproject.toml")]
    )


def test_files_are_written_together_or_not_at_all(tmp_path: Path) -> None:
    first = {PurePosixPath("a/one.txt"): "1\n", PurePosixPath("two.txt"): "2\n"}
    report = write_files(tmp_path, first)
    assert sorted(path.name for path in report.created) == ["one.txt", "two.txt"]
    assert write_files(tmp_path, first).unchanged == report.created

    # A file that exists with other content is the developer's work.
    (tmp_path / "two.txt").write_text("mine\n", encoding="utf-8")
    changed = {**first, PurePosixPath("three.txt"): "3\n"}
    with pytest.raises(CliError, match=r"two\.txt.*Nothing was changed"):
        write_files(tmp_path, changed)
    assert not (tmp_path / "three.txt").exists()

    forced = write_files(tmp_path, changed, force=True)
    assert [path.name for path in forced.updated] == ["two.txt"]
    (tmp_path / "two.txt").write_text("mine again\n", encoding="utf-8")
    maintained = write_files(tmp_path, changed, replace=frozenset({PurePosixPath("two.txt")}))
    assert [path.name for path in maintained.updated] == ["two.txt"]
    merged = report.merged(forced)
    assert len(merged.created) == 3


def test_a_folder_where_a_file_should_be_is_a_conflict(tmp_path: Path) -> None:
    (tmp_path / "thing").mkdir()
    with pytest.raises(CliError, match="thing"):
        write_files(tmp_path, {PurePosixPath("thing"): "x"}, force=True)


def test_generated_python_is_formatted_whatever_the_names_are() -> None:
    long = "a_very_long_package_name_indeed"
    source = (
        "import pytest\nfrom zebra import z\nfrom "
        + long
        + " import ask\nimport asyncio\n\n\n"
        + "def f():\n    return call(first_argument, second_argument, "
        + ", ".join(f"{long}_{n}" for n in range(4))
        + ")\n"
    )
    files = {PurePosixPath("tests/test_x.py"): source, PurePosixPath("README.md"): "import  x"}
    done = format_python(files, [long])
    text = done[PurePosixPath("tests/test_x.py")]
    assert text.startswith("import asyncio\n\nimport pytest\nfrom zebra import z\n\nfrom " + long)
    assert max(len(line) for line in text.splitlines()) <= 100
    assert done[PurePosixPath("README.md")] == "import  x"
    assert format_python({PurePosixPath("a.txt"): "x"}, []) == {PurePosixPath("a.txt"): "x"}
    with pytest.raises(CliError, match="could not be formatted"):
        format_python({PurePosixPath("bad.py"): "def (:\n"}, [])


# --------------------------------------------------------------- shared files

SERVER = ServerEntry(
    id="hello",
    owner="team",
    url="http://127.0.0.1:8100/mcp",
    audience="hello-mcp",
    tools=(ToolEntry(name="hello.greet", version="0.1.0", read_only=True),),
)
AGENT = AgentEntry(id="hello-agent", owner="team", version="0.1.0", mcp_servers=("hello",))
RULE = {"id": "hello-agent-uses-its-models", "actions": ["model.route"], "resources": ["default"]}


def test_the_registries_round_trip_through_their_files(tmp_path: Path) -> None:
    assert read_agents(tmp_path) == ()
    assert read_servers(tmp_path) == ()
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / "agents.yaml").write_text(agents_text([AGENT]), encoding="utf-8")
    (tmp_path / "registry" / "mcp-tools.yaml").write_text(tools_text([SERVER]), encoding="utf-8")
    assert read_agents(tmp_path) == (AGENT,)
    assert read_servers(tmp_path) == (SERVER,)
    assert agents_text([AGENT]).startswith("# The agent registry")

    pin = "ab" * 32
    assert pinned(SERVER, {"hello.greet": pin, "other": pin}).tools[0].schema_sha256 == pin
    (tmp_path / "registry" / "agents.yaml").write_text("agents: [", encoding="utf-8")
    with pytest.raises(CliError, match="cannot be read"):
        read_agents(tmp_path)
    (tmp_path / "registry" / "mcp-tools.yaml").write_text("schema: other/v9", encoding="utf-8")
    with pytest.raises(CliError):
        read_servers(tmp_path)


def test_rules_are_appended_once_and_what_is_there_is_never_changed() -> None:
    empty = rules_text(None, [])
    assert yaml.safe_load(empty) == {"schema": "agentlib.rules/v1", "rules": []}
    first = rules_text(empty, [RULE], title="hello-agent: its models.")
    assert "  # hello-agent: its models.\n  - id: hello-agent-uses-its-models\n" in first
    assert "    actions: [model.route]\n" in first
    assert rules_text(first, [RULE]) == first

    # The developer edits their rule and adds a comment; a later command keeps both.
    edited = first.replace("resources: [default]", "resources: [default, fast]  # ours")
    masked = {
        "id": "analysts-see-no-email",
        "actions": ["data.query"],
        "roles": ["analyst"],
        "resources": ["people.*"],
        "max_classification": "confidential",
        "obligations": {"mask_columns": ["email"], "max_rows": 50},
    }
    later = rules_text(edited, [RULE, masked])
    assert later.startswith(edited.rstrip("\n"))
    assert "# ours" in later
    rules = yaml.safe_load(later)["rules"]
    assert [rule["id"] for rule in rules] == [RULE["id"], masked["id"]]
    assert rules[0]["resources"] == ["default", "fast"]
    assert rules[1] == masked
    assert '    resources: ["people.*"]\n' in later


def test_a_rules_file_that_cannot_be_extended_safely_is_left_alone() -> None:
    with pytest.raises(CliError, match="not a valid rules document"):
        rules_text("schema: agentlib.rules/v1\nrules: [", [RULE])
    reordered = "rules: []\nschema: agentlib.rules/v1\n"
    with pytest.raises(CliError, match="add these by hand: hello-agent-uses-its-models"):
        rules_text(reordered, [RULE])


# ------------------------------------------------------------- settings files


def resolved(tmp_path: Path, text: str) -> ConfigResolver:
    folder = tmp_path / "agents" / "svc"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / ".env").write_text(text, encoding="utf-8")
    return ConfigResolver(DotenvConfigSource(folder / ".env"), base_dir=folder)


def test_an_agents_settings_select_the_shared_files_and_a_model(tmp_path: Path) -> None:
    config = resolved(tmp_path, agent_env(ANSWERS.agents[0])).resolve()
    assert (config.model.provider, config.model.model_id) == ("fake", "fake-model")
    registry = config.section(Section.REGISTRY).parse_options(FileRegistryOptions)
    rules = config.section(Section.POLICY).parse_options(RulesPolicyOptions)
    assert registry.agents_path.resolve() == (tmp_path / "registry" / "agents.yaml").resolve()
    assert (
        rules.path.resolve()
        == (tmp_path / "policies" / "agentlib" / "rules" / "data.yaml").resolve()
    )
    identity = config.section(Section.IDENTITY).parse_options(StaticIdentityOptions)
    assert (identity.roles, identity.audience) == (("analyst",), None)
    assert config.section(Section.CHECKPOINT).provider == "sqlite"


@pytest.mark.parametrize("provider", ["anthropic", "bedrock"])
def test_a_real_model_provider_is_selected_and_its_model_left_to_the_developer(
    tmp_path: Path, provider: str
) -> None:
    text = agent_env(AgentAnswers("hello-agent", "x", provider, 8000))
    config = resolved(tmp_path, text).resolve()
    assert (config.model.provider, config.model.model_id) == (provider, None)
    assert "<model-id>" in text
    assert not config.secrets


def test_a_servers_settings_select_its_data_and_no_checkpoint_store(tmp_path: Path) -> None:
    config = resolved(tmp_path, mcp_env(ANSWERS.mcp_servers[0])).resolve()
    people = config.data_sources["people"].parse_options(DuckDbCsvOptions)
    assert config.data_sources["people"].provider == "duckdb_csv"
    assert people.data_dir == tmp_path / "agents" / "svc" / "data"
    assert config.section(Section.CHECKPOINT).provider == "none"
    identity = config.section(Section.IDENTITY).parse_options(StaticIdentityOptions)
    assert identity.audience == "hello-mcp"


def test_variable_names_for_the_readmes_come_from_the_binding_table(tmp_path: Path) -> None:
    names = variable_names()
    # One of them is the start of a name: a secret's variable ends with the secret's name.
    names["secret_prefix"] += "RATES_TOKEN"
    text = "\n".join(
        f"{name}={json.dumps({}) if 'options' in key or key == 'data_sources' else 'fake'}"
        for key, name in names.items()
        if key != "deployment_env"
    )
    config = resolved(tmp_path, text).resolve()
    assert config.model.provider == "fake"


# ------------------------------------------------------------- YAML a person keeps


@pytest.mark.parametrize(
    ("value", "written"),
    [
        ("analyst", "analyst"),
        ("people by team", "people by team"),
        ("hello-staff_1.v2", "hello-staff_1.v2"),
        # What YAML would read as something other than this text is quoted.
        ("on", '"on"'),
        ("No", '"No"'),
        ("50", '"50"'),
        ("hello/*", '"hello/*"'),
        ("trailing ", '"trailing "'),
        ("", '""'),
        ("über", '"über"'),
        (50, "50"),
        (True, "true"),
    ],
)
def test_a_value_is_written_plainly_only_where_yaml_reads_it_back_unchanged(
    value: object, written: str
) -> None:
    assert yaml_scalar(value) == written
    assert yaml.safe_load(f"key: {written}")["key"] == value
    assert yaml_flow([value, "x"]) == f"[{written}, x]"
