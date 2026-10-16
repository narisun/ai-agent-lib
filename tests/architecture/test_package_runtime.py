"""One default interpreter runs isolated packages without environment executables."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from tools import package_runtime

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("version_key", ["version", "version_info"])
def test_existing_environment_packages_use_default_python_not_local_binaries(
    tmp_path: Path, version_key: str
) -> None:
    folder = package_runtime.package_folder(tmp_path, "agent")
    folder.mkdir(parents=True)
    environment = tmp_path / ".venv"
    (environment / "pyvenv.cfg").write_text(
        f"home = {sys.base_prefix}\n"
        f"{version_key} = {sys.version_info.major}.{sys.version_info.minor}.0\n"
    )
    scripts = environment / "Scripts"
    scripts.mkdir(exist_ok=True)
    for name in ("python.exe", "pip.exe"):
        (scripts / name).write_text("blocked executable: must never run")
    (folder / "probe.py").write_text("import sys; print(sys.executable)\n")
    default_python = str(getattr(sys, "_base_executable", sys.executable))
    result = subprocess.run(  # noqa: S603 - existing base Python, deliberately unusable env binaries
        [
            default_python,
            "-I",
            "-S",
            str(ROOT / "tools/package_runtime.py"),
            "--packages",
            str(folder),
            "-m",
            "probe",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert Path(result.stdout.strip()) == Path(default_python)
    assert all(path.read_text().startswith("blocked executable") for path in scripts.iterdir())


def test_existing_environment_with_a_different_base_requires_setup(tmp_path: Path) -> None:
    folder = package_runtime.package_folder(tmp_path, "agent")
    folder.mkdir(parents=True)
    (tmp_path / ".venv/pyvenv.cfg").write_text(
        f"home = {tmp_path}/other-python\n"
        f"version = {sys.version_info.major}.{sys.version_info.minor}.0\n"
    )
    with pytest.raises(ValueError, match="different Python"):
        package_runtime.validate_packages(folder)


def test_isolated_default_python_writes_utf8_logs(tmp_path: Path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/package_runtime.py").write_bytes(
        (ROOT / "tools/package_runtime.py").read_bytes()
    )
    target = package_runtime.package_folder(tmp_path, "agent")
    target.mkdir(parents=True)
    (target / package_runtime.MANIFEST).write_text(json.dumps(package_runtime.runtime_identity()))
    (target / "probe.py").write_text(
        "import sys\nassert sys.flags.isolated\nassert sys.flags.utf8_mode\n"
        "assert sys.stdout.encoding == 'utf-8'\nprint('saved → \\ue000')\n",
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - controlled module, current interpreter
        package_runtime.command(tmp_path, "agent", "-m", "probe"),
        env=dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="ascii"),
        capture_output=True,
        check=True,
    )
    assert result.stdout.decode("utf-8").strip() == "saved → \ue000"


def test_terminal_interrupt_does_not_kill_the_supervisor_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child = Mock()
    child.wait.side_effect = [KeyboardInterrupt, 0]
    monkeypatch.setattr("tools.package_runtime.subprocess.Popen", Mock(return_value=child))
    assert package_runtime.wait_for_child([sys.executable, "supervisor.py"]) == 0
    child.kill.assert_not_called()
    child.terminate.assert_not_called()


def test_package_bootstrap_isolates_versions_and_processes_editable_paths(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "sdk_choice.py").write_text("VERSION = 'wrong'\n")
    environment = dict(os.environ, PYTHONPATH=str(foreign))
    for component, version in (("agent", "2"), ("ui", "1"), ("phoenix", "2")):
        target = package_runtime.package_folder(tmp_path, component)
        target.mkdir(parents=True)
        (target / package_runtime.MANIFEST).write_text(
            json.dumps(package_runtime.runtime_identity())
        )
        (target / "sdk_choice.py").write_text(f"VERSION = '{version}'\n")
        source = tmp_path / f"source {component}"
        source.mkdir()
        (source / "probe.py").write_text(
            "import sys, sdk_choice, subprocess\n"
            "print(sdk_choice.VERSION)\n"
            "print(sys.executable)\n"
            f"assert [p for p in sys.path if p.endswith('site-packages')] == [{str(target)!r}]\n"
            "child = subprocess.check_output([sys.executable, '-c', "
            "'import sdk_choice; print(sdk_choice.VERSION)'], text=True)\n"
            "assert child.strip() == sdk_choice.VERSION\n"
        )
        (target / "editable.pth").write_text(str(source) + "\n")
        result = subprocess.run(  # noqa: S603 - controlled isolated interpreter probe
            [
                sys.executable,
                "-I",
                "-S",
                str(ROOT / "tools/package_runtime.py"),
                "--packages",
                str(target),
                "-m",
                "probe",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        assert result.stdout.splitlines() == [version, sys.executable]
    assert not list(tmp_path.rglob("pyvenv.cfg"))


def test_bootstrap_rejects_packages_from_another_python(tmp_path: Path) -> None:
    (tmp_path / package_runtime.MANIFEST).write_text('{"version": [0, 0]}')
    result = subprocess.run(  # noqa: S603 - controlled isolated interpreter probe
        [
            sys.executable,
            "-I",
            "-S",
            str(ROOT / "tools/package_runtime.py"),
            "--packages",
            str(tmp_path),
            "-m",
            "missing",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "different Python" in result.stderr
    assert "No module named" not in result.stderr


@pytest.mark.parametrize("failure", [None, "install", "check"])
def test_installation_is_checked_before_replacing_packages_and_preserves_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str | None
) -> None:
    # The test runner's isolated dependency folder intentionally has no pip.
    # Model the outer setup process's approved pip alongside its mocked commands.
    pip = Mock(origin="C:/approved/Python/Lib/site-packages/pip/__init__.py")
    monkeypatch.setattr("tools.package_runtime.importlib.util.find_spec", Mock(return_value=pip))
    destination = package_runtime.package_folder(tmp_path, "ui")
    destination.mkdir(parents=True)
    (destination / "old.py").write_text("old")
    database = tmp_path / ".agentlib/ui-rmprep/conversations.sqlite"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"saved conversations")
    commands: list[list[str]] = []

    def execute(command: list[str], **kwargs: object) -> None:
        commands.append(command)
        assert command[0] == sys.executable
        if command[1:4] == ["-m", "pip", "install"]:
            assert "--no-user" in command
            staging = Path(command[command.index("--target") + 1])
            staging.mkdir()
            (staging / "new.py").write_text("new")
            if failure == "install":
                raise subprocess.CalledProcessError(1, command)
        elif failure == "check":
            raise subprocess.CalledProcessError(1, command)
        assert (destination / "old.py").is_file()

    monkeypatch.setattr("tools.package_runtime.subprocess.run", execute)
    if failure:
        with pytest.raises(subprocess.CalledProcessError):
            package_runtime.install(tmp_path, "ui", ["example==1"])
    else:
        package_runtime.install(tmp_path, "ui", ["example==1"])
    assert (destination / "old.py").exists() is bool(failure)
    assert (destination / "new.py").exists() is (failure is None)
    assert database.read_bytes() == b"saved conversations"
    assert list(destination.parent.iterdir()) == [destination]
    if failure != "install":
        assert commands[1][1:3] == ["-I", "-S"]
        assert "--check" in commands[1]


def test_failed_promotion_restores_existing_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pip = Mock(origin="C:/approved/Python/Lib/site-packages/pip/__init__.py")
    monkeypatch.setattr("tools.package_runtime.importlib.util.find_spec", Mock(return_value=pip))
    destination = package_runtime.package_folder(tmp_path, "ui")
    destination.mkdir(parents=True)
    (destination / "old.py").write_text("old")
    original = Path.rename

    def rename(path: Path, target: Path) -> Path:
        if path.name.startswith(".ui-") and "previous" not in path.name:
            raise OSError("file locked")
        return original(path, target)

    def execute(command: list[str], **kwargs: object) -> None:
        if "--target" in command:
            Path(command[command.index("--target") + 1]).mkdir()

    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr("tools.package_runtime.subprocess.run", execute)
    with pytest.raises(OSError, match="file locked"):
        package_runtime.install(tmp_path, "ui", ["example==1"])
    assert (destination / "old.py").read_text() == "old"
    assert list(destination.parent.iterdir()) == [destination]


def test_no_installation_starts_without_current_pip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = Mock()
    monkeypatch.setattr("tools.package_runtime.subprocess.run", execute)
    monkeypatch.setattr("tools.package_runtime.importlib.util.find_spec", Mock(return_value=None))
    with pytest.raises(ValueError, match="enterprise-provided pip"):
        package_runtime.install(tmp_path, "ui", ["example==1"])
    execute.assert_not_called()


def test_module_command_uses_same_python_and_isolated_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = Mock(return_value=7)
    monkeypatch.setattr("tools.package_runtime.wait_for_child", execute)
    assert package_runtime.main(["ui", "-m", "ai_agent_lib_ui", "check"]) == 7
    assert execute.call_args.args[0] == package_runtime.command(
        ROOT, "ui", "-m", "ai_agent_lib_ui", "check"
    )


def test_install_command_passes_requirements_to_dependency_installer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install = Mock()
    monkeypatch.setattr(package_runtime, "install", install)
    assert package_runtime.main(["ui", "--install", "-e", "package[chat]"]) == 0
    install.assert_called_once_with(ROOT, "ui", ["-e", "package[chat]"])


def test_check_command_loads_existing_pip_without_global_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execute = Mock(return_value=0)
    pip = Mock(origin="C:/approved/Python/Lib/site-packages/pip/__init__.py")
    monkeypatch.setattr("tools.package_runtime.importlib.util.find_spec", Mock(return_value=pip))
    monkeypatch.setattr("tools.package_runtime.wait_for_child", execute)
    assert package_runtime.main(["phoenix", "--check"]) == 0
    assert execute.call_args.args[0] == package_runtime.command(
        ROOT, "phoenix", "--check", pip.origin
    )
