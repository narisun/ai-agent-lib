"""Looking at a workspace: doctor, config explain, policy test, graph, run and update."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from contextlib import AbstractContextManager
from pathlib import Path, PurePosixPath

import jinja2
import pytest
import yaml

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.policy import Sample, load_samples, opa_server, samples_text
from ai_agent_lib_cli.processes import read_graph, run_code, run_service
from ai_agent_lib_cli.render import TemplateRenderer
from ai_agent_lib_cli.scaffold import LOCK_FILE, Scaffolder
from ai_agent_lib_cli.testing import as_written, toolbox_for_tests
from ai_agent_lib_cli.toolbox import Toolbox
from ai_agent_lib_cli.workspace import (
    AgentAnswers,
    LibrarySource,
    McpAnswers,
    WorkspaceAnswers,
)
from ai_agent_lib_core.adapters import OpaPolicyDecisionPoint
from ai_agent_lib_core.config import Key, secret_key, variable_for

PINS = {"hello.greet": "ab" * 32, "hello.people_by_team": "cd" * 32}
RULES = Path("policies/agentlib/rules/data.yaml")
SAMPLES = Path("tests/policy-samples.yaml")


# No formatter and no server processes: these tests are about the commands.
TOOLS = toolbox_for_tests(pins=PINS)


def run(*arguments: str | Path, tools: Toolbox = TOOLS) -> int:
    return main([str(argument) for argument in arguments], toolbox=tools)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    assert run("init", "demo", "--dir", tmp_path, "--owner", "demo-team") == 0
    assert run("new", "mcp", "hello-mcp", "--workspace", root) == 0
    assert run("new", "agent", "hello-agent", "--mcp", "hello", "--workspace", root) == 0
    return root


# ------------------------------------------------------------------------ doctor


def test_doctor_checks_the_shared_files_and_every_service(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert run("doctor", "--workspace", workspace) == 0
    out = capsys.readouterr().out
    for line in (
        "workspace",
        "  ok    registries: valid and consistent",
        "  ok    rules: valid",
        "  ok    pins of hello: 2 tools match",
        "hello-agent",
        "  ok    model: fake serves 'fake-model'",
        "hello-mcp",
        "  ok    data 'people' (duckdb_csv): checked",
        "Everything checked is usable.",
    ):
        assert line in out
    assert "FAIL" not in out


def test_doctor_names_what_is_wrong_and_how_to_fix_it_and_exits_with_one(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / "mcp-servers/hello-mcp/.env").unlink()
    (workspace / "mcp-servers/hello-mcp/queries/people_by_team.sql").write_text(
        "-- description: Broken.\n-- max_rows: 5\nSELECT * FROM people\n", encoding="utf-8"
    )
    agent_env = workspace / "agents/hello-agent/.env"
    agent_env.write_text(
        agent_env.read_text(encoding="utf-8").replace("data.yaml", "no-such-rules.yaml"),
        encoding="utf-8",
    )
    changed = toolbox_for_tests(
        pins={**PINS, "hello.greet": "ef" * 32, "hello.new_tool": "01" * 32}
    )
    capsys.readouterr()

    assert run("doctor", "--workspace", workspace, tools=changed) == 1
    captured = capsys.readouterr()
    out = captured.out
    assert "  FAIL  pins of hello: changed or not pinned: hello.greet, hello.new_tool" in out
    assert "        fix: Run: agentlib registry pin hello" in out
    assert "  FAIL  settings:" in out
    assert "Copy .env.example to .env" in out
    assert "  FAIL  startup:" in out  # the agent cannot be built without its rules file
    assert "no-such-rules.yaml" in out
    assert captured.err.strip() == "agentlib: error: 3 checks failed"

    # One service alone, with the developer's own fix applied.
    (workspace / "mcp-servers/hello-mcp/.env").write_text(
        (workspace / "mcp-servers/hello-mcp/.env.example").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    assert run("doctor", "hello-mcp", "--workspace", workspace) == 1
    out = capsys.readouterr().out
    assert "workspace" not in out
    assert "  FAIL  startup:" in out
    assert "people_by_team" in out


def test_doctor_reports_broken_shared_files_and_unknown_services(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / RULES).write_text("schema: agentlib.rules/v1\nrules: [", encoding="utf-8")
    capsys.readouterr()
    assert run("doctor", "--no-pins", "--workspace", workspace) == 1
    out = capsys.readouterr().out
    assert "  FAIL  rules:" in out
    assert "pins of hello" not in out
    (workspace / "registry/agents.yaml").write_text("agents: [", encoding="utf-8")
    assert run("doctor", "--no-pins", "--workspace", workspace) == 1
    assert "  FAIL  registries:" in capsys.readouterr().out
    assert run("doctor", "nobody", "--workspace", workspace) == 1
    assert "no service called 'nobody'" in capsys.readouterr().err


def test_a_server_that_cannot_report_its_pins_fails_its_check(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing(service: Path, package: str) -> dict[str, str]:
        raise CliError("the tools of hello_mcp could not be pinned: no output")

    capsys.readouterr()
    assert run("doctor", "--workspace", workspace, tools=toolbox_for_tests(read_pins=failing)) == 1
    assert (
        "  FAIL  pins of hello: the tools of hello_mcp could not be pinned"
        in capsys.readouterr().out
    )


# ---------------------------------------------------------------- config explain


def test_config_explain_shows_each_setting_its_origin_and_no_secret(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = workspace / "agents/hello-agent/.env"
    env.write_text(
        env.read_text(encoding="utf-8")
        + f"{variable_for(secret_key('rates_token'))}=s3cret-value\n"
        + f"{variable_for(Key.HTTPS_PROXY)}=http://user:hunter2@proxy.example.test:8080\n"
        + f"{variable_for(Key.MODEL_ALIASES)}="
        + json.dumps({"fast": {"model_id": "m", "provider": "fake"}})
        + "\n",
        encoding="utf-8",
    )
    capsys.readouterr()
    assert run("config", "explain", "hello-agent", "--workspace", workspace) == 0
    out = capsys.readouterr().out
    provider = variable_for(Key.MODEL_PROVIDER)
    assert f"{provider}".ljust(10) in out
    assert f"from variable {provider}" in out
    assert "from profile local" in out
    assert "********" in out
    for hidden in ("s3cret-value", "hunter2"):
        assert hidden not in out
    assert variable_for(secret_key("rates_token")) in out

    env.write_text("NOT_OURS=1\n" + variable_for(Key.DEPLOYMENT_ENV) + "=mars\n", encoding="utf-8")
    assert run("config", "explain", "hello-agent", "--workspace", workspace) == 1
    assert "must be one of" in capsys.readouterr().err


# ------------------------------------------------------------------- policy test


def test_the_generated_samples_come_out_as_they_expect(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    samples = load_samples(workspace / SAMPLES)
    assert [sample.name for sample in samples] == [
        "an analyst calls a tool of hello",
        "anyone else is denied at hello",
        "a manager sees every column of hello",
        "an analyst sees hello without email",
        "hello-agent uses its default model",
        "hello-agent calls its own tool",
        "hello-agent may not call a tool no rule names",
        "hello-agent calls a tool of hello",
    ]
    capsys.readouterr()
    assert run("policy", "test", "--workspace", workspace) == 0
    out = capsys.readouterr().out
    assert "  ok    anyone else is denied at hello: deny (no_matching_rule)" in out
    assert "8 samples came out as expected." in out


def test_a_sample_that_comes_out_differently_fails_and_says_what_was_expected(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rules = workspace / RULES
    rules.write_text(
        rules.read_text(encoding="utf-8").replace("roles: [analyst, manager]", "roles: [manager]"),
        encoding="utf-8",
    )
    mine = {
        "name": "an auditor may not query",
        "action": "data.query",
        "application": "hello-mcp",
        "resource": "people.people_by_team",
        "classification": "confidential",
        "roles": ["auditor"],
        "expect": "allow",
    }
    text = samples_text((workspace / SAMPLES).read_text(encoding="utf-8"), [mine])
    (workspace / SAMPLES).write_text(text, encoding="utf-8")
    capsys.readouterr()
    assert run("policy", "test", "--workspace", workspace) == 1
    captured = capsys.readouterr()
    assert "  FAIL  an analyst calls a tool of hello: deny (no_matching_rule)" in captured.out
    assert "        expected: allow (hello-staff-call-the-tools)" in captured.out
    assert "  FAIL  an auditor may not query: deny (no_matching_rule)" in captured.out
    assert "        expected: allow\n" in captured.out
    assert captured.err.strip() == "agentlib: error: 2 samples did not come out as expected"


def test_samples_are_appended_once_and_the_developers_are_kept() -> None:
    first = samples_text(None, [])
    assert yaml.safe_load(first)["samples"] == []
    one = {"name": "yes: a 'quoted' name", "action": "tool.call", "application": "a",
           "resource": "s/*", "roles": ["on"], "kind": "service", "agent": "x-agent",
           "expect": "deny", "reason": "no_matching_rule"}  # fmt: skip
    text = samples_text(first, [one])
    (parsed,) = yaml.safe_load(text)["samples"]
    assert parsed == one
    assert samples_text(text, [one]) == text
    edited = text.replace("expect: deny", "expect: deny  # ours")
    later = samples_text(edited, [one, {**one, "name": "another"}])
    assert later.startswith(edited.rstrip("\n"))
    assert [s["name"] for s in yaml.safe_load(later)["samples"]] == [one["name"], "another"]
    request = Sample.model_validate(one).request()
    assert (request.principal.delegation_chain, request.principal.kind.value) == (
        ("x-agent",),
        "service",
    )
    assert request.resource.kind == "tool"
    assert request.resource.classification is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("samples: [", "not valid YAML"),
        ("schema: other/v1\nsamples: []\n", "not a samples document"),
        ("schema: agentlib.policy-samples/v1\nsamples:\n  - name: x\n", "action"),
        (
            "schema: agentlib.policy-samples/v1\nsamples:\n"
            + "  - {name: x, action: tool.call, application: a, resource: r, expect: allow}\n" * 2,
            "used more than once",
        ),
    ],
)
def test_a_samples_file_that_is_wrong_is_refused(
    workspace: Path, capsys: pytest.CaptureFixture[str], text: str, message: str
) -> None:
    (workspace / SAMPLES).write_text(text, encoding="utf-8")
    capsys.readouterr()
    assert run("policy", "test", "--workspace", workspace) == 1
    assert message in capsys.readouterr().err
    new = {"name": "n", "action": "tool.call", "application": "a", "resource": "r"}
    with pytest.raises(CliError):
        samples_text(text, [{**new, "expect": "allow"}])


def test_without_samples_or_a_bundle_the_command_says_so(
    workspace: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    # Where the opa program is not installed, the real starter says so before it starts anything.
    with (
        pytest.raises(CliError, match="the opa program is not on PATH"),
        opa_server(tmp_path, tmp_path, find_program=lambda name: None),
    ):
        pass
    with (
        pytest.raises(CliError, match="Rego bundle was not found"),
        opa_server(tmp_path / "none", tmp_path, find_program=lambda name: "/usr/bin/true"),
    ):
        pass

    def no_opa(bundle: Path, policies: Path) -> AbstractContextManager[OpaPolicyDecisionPoint]:
        raise CliError("the opa program is not on PATH")

    without_opa = toolbox_for_tests(pins=PINS, opa_server=no_opa)
    assert run("policy", "test", "--opa", "--workspace", workspace, tools=without_opa) == 1
    assert "the opa program is not on PATH" in capsys.readouterr().err
    (workspace / SAMPLES).write_text(samples_text(None, []), encoding="utf-8")
    assert run("policy", "test", "--workspace", workspace) == 1
    assert "holds no samples" in capsys.readouterr().err
    (workspace / SAMPLES).unlink()
    assert run("policy", "test", "--workspace", workspace) == 1
    assert "no samples to try" in capsys.readouterr().err


# ------------------------------------------------------------------ graph and run


def service(tmp_path: Path, **modules: str) -> Path:
    package = tmp_path / "src" / "demo_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, body in modules.items():
        (package / f"{name}.py").write_text(body, encoding="utf-8")
    return tmp_path


def test_a_service_runs_in_its_own_folder_with_its_arguments(tmp_path: Path) -> None:
    body = (
        "import pathlib, sys\n"
        "pathlib.Path('ran.txt').write_text(' '.join(sys.argv[1:]))\n"
        "raise SystemExit(7)\n"
    )
    folder = service(tmp_path, service=body)
    assert run_service(folder, "demo_agent.service", ["--port", "9000"]) == 7
    assert (folder / "ran.txt").read_text(encoding="utf-8") == "--port 9000"
    with pytest.raises(CliError, match="no src folder"):
        run_service(tmp_path / "gone", "demo_agent.service", [])


STOPPABLE = (
    "import pathlib, signal, sys, time\n"
    "def stop(number, _frame):\n"
    "    pathlib.Path('stopped.txt').write_text(signal.Signals(number).name)\n"
    "    sys.exit(5)\n"
    "signal.signal(signal.SIGTERM, stop)\n"
    "signal.signal(signal.SIGINT, stop)\n"
    "pathlib.Path('ready.txt').write_text('')\n"
    "time.sleep(60)\n"
)
RUN_IT = (
    "import sys, pathlib\n"
    "from ai_agent_lib_cli.processes import run_service\n"
    "sys.exit(run_service(pathlib.Path(sys.argv[1]), 'demo_agent.service', []))\n"
)


@pytest.mark.skipif(sys.platform == "win32", reason="one process cannot ask another to stop")
@pytest.mark.parametrize("asked", [signal.SIGTERM, signal.SIGINT])
def test_a_request_to_stop_only_the_command_still_stops_the_service_in_its_own_way(
    tmp_path: Path, asked: signal.Signals
) -> None:
    folder = service(tmp_path, service=STOPPABLE)
    # Only the command is signalled, as a process manager or an editor would do.
    # A terminal signals the service as well; then there is nothing to pass on.
    wrapper = subprocess.Popen([sys.executable, "-c", RUN_IT, str(folder)])  # noqa: S603
    try:
        deadline = time.monotonic() + 30
        while not (folder / "ready.txt").exists():
            assert time.monotonic() < deadline, "the service never started"
            assert wrapper.poll() is None
            time.sleep(0.05)
        wrapper.send_signal(asked)
        # The command ends with the service's own exit code, after the service has stopped.
        assert wrapper.wait(timeout=30) == 5
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
    assert (folder / "stopped.txt").read_text(encoding="utf-8") == asked.name


def test_code_that_fails_is_reported_by_its_last_line(tmp_path: Path) -> None:
    assert run_code(tmp_path, "print('hello')") == "hello\n"
    with pytest.raises(CliError, match="failed: ValueError: no"):
        run_code(tmp_path, "raise ValueError('no')")
    with pytest.raises(CliError, match="could not be run"):
        run_code(tmp_path / "missing", "print(1)")


def test_run_and_graph_start_the_right_module_of_the_right_service(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[tuple[str, str, tuple[str, ...]]] = []

    def runner(folder: Path, module: str, arguments: tuple[str, ...]) -> int:
        started.append((folder.name, module, tuple(arguments)))
        return 3 if module.endswith("__main__") else 0

    tools = toolbox_for_tests(
        pins=PINS, run_service=runner, read_graph=lambda folder, package: "graph TD;\n"
    )
    assert run("run", "hello-agent", "--workspace", workspace, "--port", "9000", tools=tools) == 0
    assert run("run", "hello", "--workspace", workspace, tools=tools) == 3
    assert started == [
        ("hello-agent", "hello_agent.service", ("--port", "9000")),
        ("hello-mcp", "hello_mcp.__main__", ()),
    ]
    capsys.readouterr()
    assert run("graph", "hello-agent", "--workspace", workspace, tools=tools) == 0
    assert capsys.readouterr().out == "graph TD;\n"
    assert run("graph", "hello-mcp", "--workspace", workspace, tools=tools) == 1
    assert run("run", "nobody", "--workspace", workspace, tools=tools) == 1


def test_the_graph_reader_runs_the_agents_own_graph_module(tmp_path: Path) -> None:
    folder = service(tmp_path, graph="raise SystemExit('the graph module was imported')\n")
    with pytest.raises(CliError, match="the graph module was imported"):
        read_graph(folder, "demo_agent")
    assert sys.executable


# ------------------------------------------------------------------------ update

ANSWERS = WorkspaceAnswers(
    name="demo",
    owner="demo-team",
    library=LibrarySource(kind="index", version=">=0.1"),
    agents=(AgentAnswers("hello-agent", "Greets people.", "fake", 8000, ("hello",)),),
    mcp_servers=(McpAnswers("hello-mcp", "hello", "A directory.", 8100),),
)


class NewerTemplates(TemplateRenderer):
    """The package's templates as a later version of the library might write them."""

    def render(self, folder: str, context: object) -> dict[PurePosixPath, str]:
        files = super().render(folder, context)  # type: ignore[arg-type]
        for path in list(files):
            if path.name in {"tools.py", "graph.py"}:
                files[path] += "\n# written by a newer version\n"
        if folder == "agent":
            files[PurePosixPath("src/hello_agent/extra.py")] = '"""New in this version."""\n'
        return files


def scaffolder(renderer: TemplateRenderer | None = None) -> Scaffolder:
    return Scaffolder(
        renderer, pin_reader=lambda service, package: dict(PINS), formatter=as_written
    )


def test_update_replaces_what_the_developer_left_alone_and_keeps_what_they_changed(
    tmp_path: Path,
) -> None:
    scaffolder().init(tmp_path, ANSWERS)
    agent = tmp_path / "agents/hello-agent"
    lock = json.loads((tmp_path / LOCK_FILE).read_text(encoding="utf-8"))
    assert "agents/hello-agent/src/hello_agent/tools.py" in lock["files"]
    assert "agents/hello-agent/.env.example" in lock["files"]
    assert not any(name.endswith("/.env") or "registry" in name for name in lock["files"])

    # The developer works: one file changed, one deleted, their own settings and a rule edited.
    tools = agent / "src/hello_agent/tools.py"
    tools.write_text(tools.read_text(encoding="utf-8") + "\n# my tool\n", encoding="utf-8")
    (agent / "tests/test_hello_agent_service.py").unlink()
    (agent / ".env").write_text("# mine\n", encoding="utf-8")
    rules = (tmp_path / RULES).read_text(encoding="utf-8") + "  # my comment\n"
    (tmp_path / RULES).write_text(rules, encoding="utf-8")

    assert scaffolder().update(tmp_path).updated == ()  # the same version: nothing to bring up

    report = scaffolder(NewerTemplates()).update(tmp_path)
    assert [path.as_posix() for path in report.updated] == [
        "agents/hello-agent/src/hello_agent/graph.py"
    ]
    assert [path.as_posix() for path in report.created] == [
        "agents/hello-agent/src/hello_agent/extra.py"
    ]
    assert [path.as_posix() for path in report.kept] == [
        "agents/hello-agent/src/hello_agent/tools.py"
    ]
    assert "# written by a newer version" in report.new_text[report.kept[0]]
    assert "# written by a newer version" in (agent / "src/hello_agent/graph.py").read_text("utf-8")
    assert tools.read_text(encoding="utf-8").endswith("# my tool\n")
    assert not (agent / "tests/test_hello_agent_service.py").exists()
    assert (agent / ".env").read_text(encoding="utf-8") == "# mine\n"
    assert (tmp_path / RULES).read_text(encoding="utf-8") == rules

    # Updating again changes nothing more, and the kept file is still theirs.
    again = scaffolder(NewerTemplates()).update(tmp_path)
    assert (again.updated, again.created) == ((), ())
    assert [path.name for path in again.kept] == ["tools.py"]


def test_the_update_command_lists_what_it_did_and_can_show_the_difference(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert run("update", "--workspace", workspace) == 0
    assert "nothing to do" in capsys.readouterr().out

    tools = workspace / "agents/hello-agent/src/hello_agent/tools.py"
    original = tools.read_text(encoding="utf-8")
    tools.write_text(original.replace("Hello, ", "Good day, "), encoding="utf-8")
    assert run("update", "--workspace", workspace) == 0
    out = capsys.readouterr().out
    assert "  kept     agents/hello-agent/src/hello_agent/tools.py  (you changed it)" in out
    assert "agentlib update --diff" in out
    assert run("update", "--diff", "--workspace", workspace) == 0
    out = capsys.readouterr().out
    assert '    -    return f"Good day, {cleaned}!"' in out
    assert '    +    return f"Hello, {cleaned}!"' in out
    assert "Good day" in tools.read_text(encoding="utf-8")


def test_a_lock_file_that_cannot_be_read_stops_the_command(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (workspace / LOCK_FILE).write_text("[not json", encoding="utf-8")
    capsys.readouterr()
    assert run("update", "--workspace", workspace) == 1
    assert "delete it to start a new record" in capsys.readouterr().err
    assert isinstance(jinja2.__version__, str)
