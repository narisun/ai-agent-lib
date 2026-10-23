"""Cooperative stop owns no ports or PIDs and leaves durable application data alone."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from architecture.launchers import interpreter_first_environment
from tools import local_processes, stop_local
from tools.local_control import RunControl, active, status
from tools.local_processes import Service


def run_folder(root: Path, name: str = "run") -> Path:
    folder = root / ".agentcraft2/local-runs" / name
    folder.mkdir(parents=True)
    return folder


def test_os_lock_distinguishes_a_live_launcher_from_stale_metadata(tmp_path: Path) -> None:
    folder = run_folder(tmp_path)
    with RunControl(folder, ["ui"]).own() as control:
        assert active(folder)
        assert status(folder)["state"] == "starting"
        control.update("running")
    assert not active(folder)
    # Simulate a crash: stale "running" metadata must not authorize PID signalling.
    assert stop_local.main([], root=tmp_path) == 0
    assert not (folder / "stop.request").exists()


@pytest.mark.parametrize("final_state", ["stopped", "forced", "shutdown_failed"])
def test_all_runs_receive_stop_before_waiting_and_report_only_confirmed_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, final_state: str
) -> None:
    folders = [run_folder(tmp_path, name) for name in ("ui", "services", "phoenix")]
    with ExitStack() as stack:
        controls = [
            stack.enter_context(RunControl(folder, [folder.name]).own()) for folder in folders
        ]

        def finish(_: float) -> None:
            assert all(control.stop_requested() for control in controls)
            for control in controls:
                control.update(final_state)
            stack.close()

        monkeypatch.setattr("tools.stop_local.time.sleep", finish)
        assert stop_local.stop(tmp_path) == (0 if final_state == "stopped" else 1)
    assert stop_local.stop(tmp_path) == 0  # Repeated stop is harmless.


def test_timeout_does_not_force_a_process_or_claim_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = run_folder(tmp_path)
    with RunControl(folder, ["ui"]).own() as control:
        assert stop_local.stop(tmp_path, timeout=0) == 1
        assert control.stop_requested()
        assert active(folder)
        output = capsys.readouterr().out
        assert "still pending" in output
        assert "Stopped gracefully" not in output


def test_unknown_control_format_is_not_signalled(tmp_path: Path) -> None:
    folder = run_folder(tmp_path)
    with RunControl(folder, ["ui"]).own():
        (folder / "control.json").write_text(json.dumps({"version": 999}))
        assert stop_local.main([], root=tmp_path) == 1
        assert not (folder / "stop.request").exists()


def test_stop_during_startup_prevents_launching_later_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = Mock()
    child.wait.return_value = 0
    child.poll.return_value = None
    spawn = Mock(return_value=child)
    logs = tmp_path / "logs"

    def ready(_: Service) -> bool:
        (logs / "stop.request").touch()
        return False

    monkeypatch.setattr(local_processes, "check_ports", Mock())
    monkeypatch.setattr("tools.local_processes.subprocess.Popen", spawn)
    monkeypatch.setattr(local_processes, "is_ready", ready)
    monkeypatch.setattr("tools.local_processes.time.sleep", Mock())
    local_processes.serve(
        [
            Service(name, (sys.executable,), tmp_path, port)
            for name, port in (("mcp", 8100), ("agent", 8000))
        ],
        logs,
    )
    spawn.assert_called_once()
    child.send_signal.assert_called_once()
    child.kill.assert_not_called()
    assert status(logs)["state"] == "stopped"
    assert not active(logs)


def test_status_write_failure_cannot_skip_child_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    child = Mock()
    child.wait.return_value = 0
    child.poll.return_value = None
    update = RunControl.update

    def fail_stopping(self: RunControl, state: str) -> None:
        if state == "stopping":
            raise OSError("status file unavailable")
        update(self, state)

    monkeypatch.setattr(RunControl, "update", fail_stopping)
    monkeypatch.setattr(local_processes, "check_ports", Mock())
    monkeypatch.setattr("tools.local_processes.subprocess.Popen", Mock(return_value=child))
    monkeypatch.setattr(local_processes, "is_ready", Mock(return_value=True))
    with pytest.raises(OSError, match="status file"):
        local_processes.serve(
            [Service("ui", (sys.executable,), tmp_path, 8765)], tmp_path / "logs", smoke_test=True
        )
    child.send_signal.assert_called_once()
    child.kill.assert_not_called()


@pytest.mark.parametrize("failure", ["kill", "wait"])
def test_failed_force_stop_does_not_skip_other_children(tmp_path: Path, failure: str) -> None:
    healthy, broken = Mock(), Mock()
    healthy.poll.return_value = broken.poll.return_value = None
    healthy.wait.return_value = 0
    broken.send_signal.side_effect = OSError("signal unavailable")
    if failure == "kill":
        broken.kill.side_effect = OSError("cannot kill")
    else:
        broken.wait.side_effect = subprocess.TimeoutExpired("child", 5)
    children = [
        local_processes.RunningService(Service(name, (), tmp_path, port), child, tmp_path / name)
        for name, child, port in [("mcp", healthy, 8100), ("ui", broken, 8765)]
    ]
    assert not local_processes.stop_children(children)
    healthy.send_signal.assert_called_once()
    healthy.wait.assert_called_once_with(timeout=30)


@pytest.mark.parametrize("already_exited", [True, False])
@pytest.mark.parametrize("exit_code", [0, 1, 3, 130, -2, 3221225786, -1073741510])
def test_shutdown_requires_confirmed_normal_exit(
    tmp_path: Path, already_exited: bool, exit_code: int
) -> None:
    child = Mock()
    child.poll.return_value = exit_code if already_exited else None
    child.wait.return_value = exit_code
    running = local_processes.RunningService(
        Service("ui", (), tmp_path, 8765), child, tmp_path / "ui.log"
    )
    interrupt_codes = {130, 3221225786, -1073741510} if sys.platform == "win32" else {130, -2}
    expected = exit_code == 0 or (not already_exited and exit_code in interrupt_codes)
    assert local_processes.stop_children([running]) is expected
    child.kill.assert_not_called()


@pytest.mark.slow
@pytest.mark.parametrize("uvicorn_server", [False, True])
def test_real_supervisor_stops_in_reverse_order_and_preserves_saved_files(
    tmp_path: Path,
    uvicorn_server: bool,
) -> None:
    root = Path(__file__).resolve().parents[2]
    names = ["phoenix", "mcp", "agent", "ui"]
    ports = []
    for _ in names:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            ports.append(listener.getsockname()[1])
    journal = tmp_path / "stopped.txt"
    logs = tmp_path / ".agentcraft2/local-runs/real-run"
    saved = [
        tmp_path / name
        for name in (
            "conversations.sqlite",
            "model-trace.jsonl",
            "checkpoints.sqlite",
            "phoenix.db",
        )
    ]
    for file in saved:
        file.write_bytes(b"existing saved data")
    child_code = """
import signal,sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
    def log_message(self, *args): pass
def interrupt(*args): raise KeyboardInterrupt
signal.signal(signal.SIGINT, interrupt)
if sys.platform == 'win32': signal.signal(signal.SIGBREAK, interrupt)
server = HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler)
try: server.serve_forever()
except KeyboardInterrupt: pass
finally:
    server.server_close()
    with open(sys.argv[2], 'a') as output: output.write(sys.argv[3] + '\\n')
"""
    commands = [
        (sys.executable, "-u", "-c", child_code, str(port), str(journal), name)
        for name, port in zip(names, ports, strict=True)
    ]
    if uvicorn_server:
        (tmp_path / "test_server.py").write_text(
            "import sys, uvicorn\n"
            "async def app(scope, receive, send):\n"
            "    if scope['type'] == 'lifespan':\n"
            "        await receive()\n"
            "        await send({'type': 'lifespan.startup.complete'})\n"
            "        await receive()\n"
            "        with open(sys.argv[2], 'a') as f: f.write(sys.argv[3] + '\\n')\n"
            "        await send({'type': 'lifespan.shutdown.complete'})\n"
            "    else:\n"
            "        await send({'type': 'http.response.start', 'status': 200, 'headers': []})\n"
            "        await send({'type': 'http.response.body', 'body': b'OK'})\n"
            "uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]), ws='none')\n"
        )
        commands = [
            (
                sys.executable,
                "-u",
                str(root / "tools/local_service.py"),
                "test_server",
                str(port),
                str(journal),
                name,
            )
            for name, port in zip(names, ports, strict=True)
        ]
    code = (
        "from pathlib import Path\nfrom tools.local_processes import Service, serve\n"
        f"services=[Service(name, command, Path({str(tmp_path)!r}), port) "
        f"for name,port,command in {list(zip(names, ports, commands, strict=True))!r}]\n"
        f"serve(services, Path({str(logs)!r}))\n"
    )
    options: dict[str, Any] = {}
    if sys.platform == "win32":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        options = {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": startup}
    with (tmp_path / "launcher.log").open("wb") as output:
        process = subprocess.Popen(  # noqa: S603 - test-owned local Python services
            [sys.executable, "-u", "-c", code],
            cwd=root,
            stdout=output,
            stderr=output,
            **options,
        )
        try:
            deadline = time.monotonic() + 25
            while not (logs / "control.json").exists() or status(logs)["state"] != "running":
                assert process.poll() is None, (tmp_path / "launcher.log").read_text()
                assert time.monotonic() < deadline, "test services did not become ready"
                time.sleep(0.1)
            assert stop_local.stop(tmp_path, timeout=35) == 0
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                (logs / "stop.request").touch()
                process.wait(timeout=40)
    assert journal.read_text().splitlines() == list(reversed(names))
    assert all(file.read_bytes() == b"existing saved data" for file in saved)
    assert status(logs)["state"] == "stopped"
    for port in ports:
        with socket.socket() as listener:
            assert listener.connect_ex(("127.0.0.1", port)) != 0


@pytest.mark.slow
@pytest.mark.skipif(sys.platform != "win32", reason="Command Prompt batch entry point")
def test_stop_batch_runs_from_another_directory_with_spaces(tmp_path: Path) -> None:
    root = tmp_path / "checkout with spaces"
    (root / "tools").mkdir(parents=True)
    source = Path(__file__).resolve().parents[2]
    for relative in (
        "stop-local.bat",
        "tools/__init__.py",
        "tools/stop_local.py",
        "tools/local_control.py",
        "tools/local_processes.py",
    ):
        (root / relative).write_bytes((source / relative).read_bytes())
    for argument in ("--help", ""):
        result = subprocess.run(  # noqa: S603 - test-owned batch file, no user input
            f'cmd.exe /d /s /c ""{root / "stop-local.bat"}" {argument}"',
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
            env=interpreter_first_environment(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert ("usage:" if argument else "No managed local launchers") in result.stdout
