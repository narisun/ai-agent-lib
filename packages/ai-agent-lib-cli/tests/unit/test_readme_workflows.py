"""Exercise generated README commands and the documented CI setup boundary."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import threading
import venv
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath

import pytest
import yaml

from ai_agent_lib_cli.scaffold import Scaffolder
from ai_agent_lib_cli.workspace import AgentAnswers, LibrarySource, McpAnswers, WorkspaceAnswers


def workspace(library: LibrarySource) -> WorkspaceAnswers:
    return WorkspaceAnswers(name="demo", owner="learners", library=library)


@pytest.mark.parametrize("source", ["path", "index"])
def test_cmd_setup_runs_the_selected_python_in_a_path_with_spaces(
    source: str, tmp_path: Path
) -> None:
    cmd = shutil.which("cmd.exe")
    if cmd is None:
        pytest.skip("Windows Command Prompt is not installed")
    checkout = tmp_path / "library's folder & name"
    venv.EnvBuilder(with_pip=False).create(checkout / ".venv")
    library = (
        LibrarySource.from_path(checkout)
        if source == "path"
        else LibrarySource(kind="index", version="==1.2.3")
    )
    files = Scaffolder()._workspace_files(workspace(library))
    readme = files[PurePosixPath("README.md")]
    setup = re.findall(r"```bat\n(.*?)```", readme, re.DOTALL)[0]
    assignments = "\n".join(line for line in setup.splitlines() if line.startswith("set "))
    script = tmp_path / "select_python.cmd"
    script.write_text(
        "@echo off\n" + assignments + '\n"%PYTHON%" -c "import sys; print(sys.executable)"\n',
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - commands rendered from the repository template
        [cmd, "/d", "/v:off", "/c", str(script)],
        cwd=checkout,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    expected = checkout.resolve() / ".venv" / "Scripts" / "python.exe"
    assert Path(result.stdout.strip()) == expected


@pytest.mark.parametrize("source", ["path", "index"])
def test_ci_setup_gate_matches_the_library_source(source: str, tmp_path: Path) -> None:
    library = (
        LibrarySource.from_path(tmp_path)
        if source == "path"
        else LibrarySource(kind="index", version="==1.2.3")
    )
    files = Scaffolder()._workspace_files(workspace(library))
    readme = files[PurePosixPath("README.md")]
    workflow = yaml.safe_load(files[PurePosixPath(".github/workflows/check.yml")])
    steps = workflow["jobs"]["check"]["steps"]
    gates = [step for step in steps if step.get("name") == "Configure the library source for CI"]
    if source == "index":
        assert gates == []
        assert any('pip install "ai-agent-lib-cli==1.2.3"' in s.get("run", "") for s in steps)
        return
    assert "**CI is not configured yet.**" in readme
    (gate,) = gates
    assert steps.index(gate) > next(
        i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-python")
    )
    command = shlex.split(gate["run"])
    assert command[:2] == ["python", "-c"]
    result = subprocess.run(  # noqa: S603 - fixed interpreter and repository workflow template
        [sys.executable, *command[1:]], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 1
    assert "README.md, Configure CI" in result.stderr
    assert "## Configure CI" in readme


@pytest.mark.parametrize("stream", [False, True])
def test_documented_http_client_sends_a_request_and_reads_the_response(
    stream: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    received: list[tuple[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, json.loads(body)))
            response = (
                b'event: update\ndata: {"answer": "Hello, Ada!"}\n\nevent: end\ndata: {}\n\n'
                if self.path.endswith("/stream")
                else b'{"output": {"answer": "Hello, Ada!"}}'
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as http:
        answers = workspace(LibrarySource(kind="index", version="==1.2.3"))
        answers = answers.with_mcp_server(McpAnswers("hello-mcp", "hello", "Greetings"))
        agent = AgentAnswers(
            "hello-agent", "A practice assistant", port=http.server_port, mcp_servers=("hello",)
        )
        files = Scaffolder()._agent_files(answers, agent)
        readme = files[PurePosixPath("agents/hello-agent/README.md")]
        assert readme.index("-m ai_agent_lib_cli run hello-mcp") < readme.index(
            "-m hello_agent.service"
        )
        (client,) = re.findall(r"```python\n(.*?)```", readme, re.DOTALL)
        client = client.replace("stream = False", f"stream = {stream}", 1)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            exec(compile(client, "<README HTTP client>", "exec"), {})  # noqa: S102 - trusted example
        finally:
            http.shutdown()
            thread.join(timeout=5)
    assert received == [
        (
            "/invoke/stream" if stream else "/invoke",
            {"input": {"question": "Say hello to Ada."}, "thread_id": "demo"},
        )
    ]
    output = capsys.readouterr().out
    assert "Hello, Ada!" in output
    assert ("event: end" in output) is stream
