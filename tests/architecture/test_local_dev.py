"""Local launchers preserve environments and state while supervising only their children."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from tools import local_dev, local_processes
from tools.local_environment import agent_environment
from tools.local_processes import RunningService, Service


def test_copy_settings_preserves_existing_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    (tmp_path / ".env.example").write_text("EAP_MODEL_PROVIDER=fake\n", encoding="utf-8")
    local_dev.copy_settings(path)
    assert path.read_text(encoding="utf-8") == "EAP_MODEL_PROVIDER=fake\n"
    path.write_text("existing private settings\n", encoding="utf-8")
    local_dev.copy_settings(path)
    assert path.read_text(encoding="utf-8") == "existing private settings\n"


def test_missing_template_does_not_leave_empty_settings(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    with pytest.raises(FileNotFoundError):
        local_dev.copy_settings(path)
    assert not path.exists()


@pytest.mark.parametrize(
    ("scenario", "names"),
    [
        ("rmprep", ["rmprep-data", "rmprep", "ui"]),
        ("services", ["rmprep-data", "rmprep"]),
        ("ui", ["ui"]),
    ],
)
def test_service_plan_uses_dependency_order_and_separate_interpreters(
    tmp_path: Path,
    scenario: str,
    names: list[str],
) -> None:
    root = tmp_path / "checkout with spaces"
    config = root / "ui state/config.json"
    services = local_dev.services_for(root, scenario, config, 8766)
    assert [service.name for service in services] == names
    for service in services:
        assert Path(service.command[0]).is_absolute()
        if service.name == "ui":
            assert service.command[0] == str(
                local_dev.interpreter(root / local_dev.UI_PROJECT / ".venv")
            )
            assert service.command[-1] == str(config)
            assert service.port == 8766
            assert service.health_path == "/healthz"
        else:
            assert service.command[0] == str(local_dev.interpreter(root / ".venv"))
            assert service.cwd == root / local_dev.WORKSPACE
            assert service.health_path == "/readyz"


def test_setup_installs_ui_extras_only_in_ui_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = Mock()
    monkeypatch.setattr(local_dev, "run", execute)
    monkeypatch.setattr(local_dev, "copy_settings", Mock())
    monkeypatch.setattr(local_dev, "prepare_ui", Mock())
    local_dev.setup(tmp_path)
    commands = [call.args[0] for call in execute.call_args_list]
    agent = str(local_dev.interpreter(tmp_path / ".venv"))
    ui = str(local_dev.interpreter(tmp_path / local_dev.UI_PROJECT / ".venv"))
    installs = [command for command in commands if command[1:4] == ["-m", "pip", "install"]]
    assert len(installs) == 2
    assert installs[0][0] == agent
    assert all("ai-agent-lib-ui" not in value for value in installs[0])
    assert installs[1][0] == ui
    assert installs[1][-1].endswith("ai-agent-lib-ui[chat,dev]")
    assert [
        agent,
        "-m",
        "ai_agent_lib_cli",
        "install",
        "--workspace",
        str(tmp_path / local_dev.WORKSPACE),
    ] in commands
    assert [agent, "-m", "pip", "check"] in commands
    assert [ui, "-m", "pip", "check"] in commands


def test_existing_ui_configuration_is_checked_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    python = tmp_path / "python.exe"
    python.touch()
    config = tmp_path / "config.json"
    config.write_text('{"port": 9876}', encoding="utf-8")
    execute = Mock()
    monkeypatch.setattr(local_dev, "run", execute)
    assert local_dev.prepare_ui(tmp_path, python, config) == 9876
    execute.assert_called_once_with(
        [str(python), "-m", "ai_agent_lib_ui", "check", "--config", str(config)], tmp_path
    )
    assert config.read_text(encoding="utf-8") == '{"port": 9876}'


def test_missing_environment_gives_actionable_error_without_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    assert local_dev.main(["services"], root=tmp_path) == 1
    assert "setup-local.bat" in capsys.readouterr().out
    start.assert_not_called()


def test_failed_configuration_check_prevents_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        local_dev, "prepare_ui", Mock(side_effect=subprocess.CalledProcessError(2, "check"))
    )
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    assert local_dev.main(["ui"], root=tmp_path) == 1
    start.assert_not_called()


def test_custom_ui_only_config_needs_no_agent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare = Mock(return_value=9000)
    start = Mock()
    config = tmp_path / "custom/config.json"
    config.parent.mkdir()
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(local_dev, "prepare_ui", prepare)
    monkeypatch.setattr(local_dev, "serve", start)
    assert (
        local_dev.main(["ui", "--config", "custom/config.json", "--smoke-test"], root=tmp_path) == 0
    )
    assert prepare.call_args.args[-1] == (tmp_path / "custom/config.json").resolve()
    services = start.call_args.args[0]
    assert [service.name for service in services] == ["ui"]
    assert services[0].port == 9000
    assert start.call_args.kwargs["smoke_test"] is True


def test_missing_custom_ui_config_is_not_initialized_as_rmprep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prepare = Mock()
    monkeypatch.setattr(local_dev, "prepare_ui", prepare)
    assert local_dev.main(["ui", "--config", "missing/config.json"], root=tmp_path) == 1
    prepare.assert_not_called()
    assert "ai_agent_lib_ui init" in capsys.readouterr().out


def test_failed_installation_stops_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    execute = Mock(side_effect=subprocess.CalledProcessError(1, "pip"))
    prepare = Mock()
    monkeypatch.setattr(local_dev, "run", execute)
    monkeypatch.setattr(local_dev, "prepare_ui", prepare)
    assert local_dev.main(["setup"], root=tmp_path) == 1
    execute.assert_called_once()
    prepare.assert_not_called()
    assert not (tmp_path / local_dev.AGENT_ENV).exists()


def test_port_conflict_does_not_adopt_or_stop_existing_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawn = Mock()
    monkeypatch.setattr("tools.local_processes.subprocess.Popen", spawn)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        service = Service("existing", (sys.executable,), tmp_path, port)
        with pytest.raises(ValueError, match=f"Port {port}"):
            local_processes.serve([service], tmp_path / "logs", smoke_test=True)
        assert listener.fileno() != -1
    spawn.assert_not_called()
    assert not (tmp_path / "logs").exists()


def test_duplicate_ports_fail_before_launch(tmp_path: Path) -> None:
    services = [Service(name, (sys.executable,), tmp_path, 8765) for name in ("agent", "ui")]
    with pytest.raises(ValueError, match="different ports"):
        local_processes.check_ports(services)


@pytest.mark.parametrize("failure", ["timeout", "exit", "interrupt", "spawn", "none"])
def test_supervisor_cleans_up_after_readiness_failure_or_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    services = [
        Service(name, (sys.executable,), tmp_path, port)
        for name, port in (("mcp", 8100), ("agent", 8000))
    ]
    children: list[Mock] = []
    stopped: list[str] = []

    def spawn(*args: object, **kwargs: object) -> Mock:
        if failure == "spawn" and children:
            raise OSError("cannot start agent")
        child = Mock()
        child.poll.return_value = None
        name = services[len(children)].name
        child.send_signal.side_effect = lambda event: stopped.append(name)
        children.append(child)
        return child

    def ready(service: Service) -> bool:
        if service.name == "mcp":
            return True
        if failure == "interrupt":
            raise KeyboardInterrupt
        if failure == "exit":
            children[0].poll.return_value = 3
        return failure == "none"

    monkeypatch.setattr(local_processes, "check_ports", Mock())
    monkeypatch.setattr("tools.local_processes.subprocess.Popen", spawn)
    monkeypatch.setattr(local_processes, "is_ready", ready)
    monkeypatch.setattr("tools.local_processes.time.sleep", Mock())
    logs = tmp_path / "logs"
    if failure == "none":
        local_processes.serve(services, logs, smoke_test=True)
    else:
        expected = (
            KeyboardInterrupt
            if failure == "interrupt"
            else OSError
            if failure == "spawn"
            else RuntimeError
        )
        with pytest.raises(expected):
            local_processes.serve(
                services, logs, startup_timeout=0 if failure == "timeout" else 1, smoke_test=True
            )
    assert stopped == (
        ["mcp"] if failure == "spawn" else ["agent"] if failure == "exit" else ["agent", "mcp"]
    )


def test_shutdown_kills_unresponsive_child_and_continues(tmp_path: Path) -> None:
    hung = Mock()
    hung.poll.return_value = None
    hung.wait.side_effect = [subprocess.TimeoutExpired("child", 30), 0]
    stopped = Mock()
    stopped.poll.return_value = 0
    services = [
        RunningService(Service("done", (), tmp_path, 8000), stopped, tmp_path / "done.log"),
        RunningService(Service("hung", (), tmp_path, 8100), hung, tmp_path / "hung.log"),
    ]
    local_processes.stop_children(services)
    hung.kill.assert_called_once()
    stopped.send_signal.assert_not_called()


@pytest.mark.skipif(sys.platform != "win32", reason="Command Prompt batch entry points")
@pytest.mark.parametrize("name", ["start-local.bat", "setup-local.bat"])
def test_batch_help_runs_from_another_directory(tmp_path: Path, name: str) -> None:
    # A space in the script path exercises cmd.exe's outer quote handling.
    root = tmp_path / "checkout with spaces"
    (root / "tools").mkdir(parents=True)
    source = Path(__file__).resolve().parents[2]
    for relative in (
        name,
        "tools/__init__.py",
        "tools/local_dev.py",
        "tools/local_processes.py",
        "tools/local_control.py",
    ):
        (root / relative).write_bytes((source / relative).read_bytes())
    result = subprocess.run(  # noqa: S603 - Windows shell entry point under test
        f'cmd.exe /d /s /c ""{root / name}" --help"',
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--smoke-test" in result.stdout


def test_stop_signal_uses_normal_cleanup_path() -> None:
    with pytest.raises(KeyboardInterrupt):
        local_dev.interrupt(signal.SIGTERM, None)


def test_agent_environment_precedence_and_secret_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = tmp_path / "service.env"
    service.write_text("EAP_MODEL_PROVIDER=fake\nEAP_MODEL_ID=fake-model\n")
    overrides = tmp_path / ".env"
    overrides.write_text(
        "EAP_MODEL_PROVIDER=anthropic\nEAP_MODEL_ID=file-model\n"
        'ANTHROPIC_API_KEY="literal-${DO_NOT_EXPAND}-key"\n'
    )
    monkeypatch.delenv("EAP_MODEL_PROVIDER", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("EAP_MODEL_ID", "shell-model")
    monkeypatch.setenv("DO_NOT_EXPAND", "expanded")
    before = dict(os.environ)
    environment = agent_environment(service, overrides, required=True)
    assert environment["EAP_MODEL_PROVIDER"] == "anthropic"
    assert environment["EAP_MODEL_ID"] == "shell-model"
    assert environment["ANTHROPIC_API_KEY"] == "literal-${DO_NOT_EXPAND}-key"
    assert dict(os.environ) == before
    output = capsys.readouterr().out
    assert str(overrides) in output
    assert "anthropic / shell-model" in output
    assert "literal-" not in output
    # Shell credentials, including an explicit empty value, also take precedence.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert agent_environment(service, overrides, required=True)["ANTHROPIC_API_KEY"] == ""


def test_no_launcher_dotenv_preserves_service_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in ("EAP_MODEL_PROVIDER", "EAP_MODEL_ID"):
        monkeypatch.delenv(name, raising=False)
    service = tmp_path / "service.env"
    service.write_text("EAP_MODEL_PROVIDER=fake\nEAP_MODEL_ID=fake-model\n")
    environment = agent_environment(service, tmp_path / ".env", required=False)
    assert "EAP_MODEL_PROVIDER" not in environment
    assert "fake / fake-model" in capsys.readouterr().out


@pytest.mark.parametrize("explicit", [False, True])
def test_launcher_passes_selected_file_only_to_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    root = tmp_path / "checkout with spaces"
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    for relative in (local_dev.AGENT_ENV, local_dev.MCP_ENV, Path(".venv/Scripts/python.exe")):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()
    # Use the platform's environment path when running on Unix too.
    python = local_dev.interpreter(root / ".venv")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.touch()
    for name in ("EAP_MODEL_PROVIDER", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (root / ".env").write_text("EAP_MODEL_PROVIDER=anthropic\nANTHROPIC_API_KEY=root-secret\n")
    (caller / "custom.env").write_text("EAP_MODEL_PROVIDER=fake\nANTHROPIC_API_KEY=custom-secret\n")
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    monkeypatch.setattr(local_dev, "prepare_ui", Mock(return_value=8765))
    args = ["--env-file", "custom.env"] if explicit else []
    assert local_dev.main(args, root=root) == 0
    mcp, agent, ui = start.call_args.args[0]
    assert agent.environment is not None
    assert agent.environment["ANTHROPIC_API_KEY"] == (
        "custom-secret" if explicit else "root-secret"
    )
    assert agent.environment["EAP_MODEL_PROVIDER"] == ("fake" if explicit else "anthropic")
    assert mcp.environment is None
    assert ui.environment is None
    assert "secret" not in repr(agent)
    assert "ANTHROPIC_API_KEY" not in os.environ


@pytest.mark.parametrize("exists", [False, True])
def test_invalid_explicit_env_file_fails_before_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exists: bool,
) -> None:
    monkeypatch.setattr(local_dev, "require_file", Mock())
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    overrides = tmp_path / "settings.env"
    if exists:
        overrides.write_text("EAP_TELEMETRY=on\nANTHROPIC_API_KEY=private-test-key\n")
    assert local_dev.main(["services", "--env-file", str(overrides)], root=tmp_path) == 1
    start.assert_not_called()
    output = capsys.readouterr().out
    assert ("EAP_TELEMETRY" if exists else str(overrides)) in output
    assert "private-test-key" not in output


@pytest.mark.parametrize("scenario", ["ui", "setup", "demo"])
def test_env_file_option_is_not_silently_ignored(scenario: str) -> None:
    with pytest.raises(SystemExit) as error:
        local_dev.arguments([scenario, "--env-file", "settings.env"])
    assert error.value.code == 2


def test_supervisor_supplies_the_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = {"ANTHROPIC_API_KEY": "private-test-key", "PATH": "inherited-path"}
    service = Service("agent", (sys.executable,), tmp_path, 8000, environment=environment)
    child = Mock()
    child.poll.return_value = None
    spawn = Mock(return_value=child)
    monkeypatch.setattr(local_processes, "check_ports", Mock())
    monkeypatch.setattr("tools.local_processes.subprocess.Popen", spawn)
    monkeypatch.setattr(local_processes, "is_ready", Mock(return_value=True))
    local_processes.serve([service], tmp_path / "logs", smoke_test=True)
    assert spawn.call_args.kwargs["env"] == environment
    assert "private-test-key" not in (tmp_path / "logs/agent.log").read_text()
