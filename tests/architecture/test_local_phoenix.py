"""The batch stack owns Phoenix only when its agent selects the local trial."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock

import pytest
from tools import local_dev, local_processes, package_runtime

from ai_agent_lib_core.config import deprecations as deprecations_module
from ai_agent_lib_core.config.bindings import legacy_spelling
from ai_agent_lib_core.config.phoenix import PHOENIX_VARIABLES


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in PHOENIX_VARIABLES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(legacy_spelling(name), raising=False)
    root = tmp_path / "checkout with spaces"
    for relative in (local_dev.AGENT_ENV, local_dev.MCP_ENV):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    package_runtime.package_folder(root, "phoenix").mkdir(parents=True)
    (root / ".env").write_text(
        "AC2_MODEL_PROVIDER=fake\nAC2_PHOENIX_ENABLED=true\nANTHROPIC_API_KEY=root-private-key\n"
    )
    monkeypatch.setattr(local_dev, "prepare_ui", Mock(return_value=8765))
    return root


@pytest.mark.parametrize("scenario", ["rmprep", "services"])
def test_root_enabled_phoenix_starts_first_in_its_own_environment(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    # Finding the root .env must not depend on the caller's current folder.
    monkeypatch.chdir(checkout.parent)
    before = dict(os.environ)
    assert local_dev.main([scenario, "--smoke-test"], root=checkout) == 0
    services = start.call_args.args[0]
    assert [s.name for s in services] == ["phoenix", "rmprep-data", "rmprep"] + (
        ["ui"] if scenario == "rmprep" else []
    )
    phoenix = services[0]
    assert phoenix.command[0] == sys.executable
    assert phoenix.command[1:3] == ("-I", "-S")
    assert phoenix.command[-4:] == ("--host", "127.0.0.1", "--port", "6006")
    assert phoenix.health_path == "/healthz"
    assert phoenix.environment["PHOENIX_SQL_DATABASE_URL"].endswith("/data/phoenix.db")
    assert "root-private-key" not in str(phoenix.environment)
    assert services[2].environment["ANTHROPIC_API_KEY"] == "root-private-key"
    assert start.call_args.kwargs["smoke_test"] is True
    assert dict(os.environ) == before


@pytest.mark.parametrize("disabled_by", ["root", "shell", "selected_file", "unset"])
def test_disabled_or_unset_phoenix_needs_no_extra_environment(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, disabled_by: str
) -> None:
    package_runtime.package_folder(checkout, "phoenix").rmdir()
    args = ["services"]
    if disabled_by == "shell":
        monkeypatch.setenv("AC2_PHOENIX_ENABLED", "false")
    elif disabled_by == "selected_file":
        custom = checkout / "selected.env"
        custom.write_text("AC2_MODEL_PROVIDER=fake\n")
        args += ["--env-file", str(custom)]
    else:
        (checkout / ".env").write_text(
            "AC2_PHOENIX_ENABLED=false\n" if disabled_by == "root" else ""
        )
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    assert local_dev.main(args, root=checkout) == 0
    assert [s.name for s in start.call_args.args[0]] == ["rmprep-data", "rmprep"]


@pytest.mark.parametrize(
    "endpoint",
    ["https://phoenix.example", "http://127.0.0.1:7000", "http://[::1]:6006"],
)
def test_external_destination_does_not_start_a_local_server(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    package_runtime.package_folder(checkout, "phoenix").rmdir()
    monkeypatch.setenv("PHOENIX_COLLECTOR_ENDPOINT", endpoint)
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    assert local_dev.main(["services"], root=checkout) == 0
    assert [s.name for s in start.call_args.args[0]] == ["rmprep-data", "rmprep"]


@pytest.mark.parametrize("failure", ["missing_environment", "invalid_flag", "missing_file"])
def test_invalid_phoenix_setup_fails_before_starting_children(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    if failure == "missing_environment":
        package_runtime.package_folder(checkout, "phoenix").rmdir()
        expected = "start-phoenix.bat setup"
    elif failure == "invalid_flag":
        monkeypatch.setenv("AC2_PHOENIX_ENABLED", "yes")
        expected = "must be true or false"
    else:
        monkeypatch.setenv("AC2_PHOENIX_ENV_FILE", str(checkout / "missing.env"))
        expected = "missing.env"
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    assert local_dev.main(["services"], root=checkout) == 1
    start.assert_not_called()
    output = capsys.readouterr().out
    assert expected in output
    assert "root-private-key" not in output


@pytest.mark.parametrize("file_owner", ["service", "launcher"])
def test_separate_settings_file_is_resolved_as_the_agent_would_resolve_it(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, file_owner: str
) -> None:
    (checkout / ".env").write_text("")
    owner = checkout / (local_dev.AGENT_ENV if file_owner == "service" else ".env")
    owner.write_text("AC2_PHOENIX_ENV_FILE=phoenix.env\n")
    base = owner.parent if file_owner == "service" else checkout / local_dev.WORKSPACE
    (base / "phoenix.env").write_text(
        "AC2_PHOENIX_ENABLED=true\nPHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006/\n"
    )
    monkeypatch.chdir(checkout.parent)
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    assert local_dev.main(["services"], root=checkout) == 0
    assert start.call_args.args[0][0].name == "phoenix"


@pytest.mark.parametrize("scenario", ["ui", "demo"])
def test_ui_only_and_demo_do_not_own_phoenix(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    package_runtime.package_folder(checkout, "phoenix").rmdir()
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    monkeypatch.setattr(local_dev, "run", Mock())
    assert local_dev.main([scenario], root=checkout) == 0
    if scenario == "ui":
        assert [s.name for s in start.call_args.args[0]] == ["ui"]
    else:
        start.assert_not_called()


@pytest.mark.parametrize("failure", ["none", "phoenix_not_ready", "later_spawn", "interrupt"])
def test_supervisor_owns_phoenix_lifecycle_and_stops_it_last(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    children: list[Mock] = []
    stopped: list[str] = []
    names = ["phoenix", "rmprep-data", "rmprep"]

    def spawn(*args: object, **kwargs: object) -> Mock:
        if failure == "later_spawn" and len(children) == 2:
            raise OSError("agent failed to spawn")
        child = Mock()
        child.wait.return_value = 0
        child.poll.return_value = None
        name = names[len(children)]
        child.send_signal.side_effect = lambda event: stopped.append(name)
        children.append(child)
        return child

    def ready(service: local_processes.Service) -> bool:
        if failure == "interrupt" and service.name == "rmprep":
            raise KeyboardInterrupt
        if failure == "phoenix_not_ready":
            raise RuntimeError("Phoenix readiness failed")
        return True

    monkeypatch.setattr(local_processes, "check_ports", Mock())
    monkeypatch.setattr("tools.local_processes.subprocess.Popen", spawn)
    monkeypatch.setattr(local_processes, "is_ready", ready)
    expected = 1 if failure in {"later_spawn", "phoenix_not_ready"} else 0
    assert local_dev.main(["services", "--smoke-test"], root=checkout) == expected
    assert stopped == (
        ["phoenix"]
        if failure == "phoenix_not_ready"
        else ["rmprep-data", "phoenix"]
        if failure == "later_spawn"
        else ["rmprep", "rmprep-data", "phoenix"]
    )
    assert all(child.wait.called for child in children)
    assert all(not child.kill.called for child in children)


# ------------------------------------------------- the EAP_ spellings, for one minor release
#
# Every test below is a dedicated legacy-alias test: it spells Phoenix variables with the
# old ``EAP_`` prefix on purpose. The rest of this file speaks ``AC2_``.


@pytest.fixture
def fresh_deprecation_channel() -> Iterator[None]:
    deprecations_module._LOGGED.clear()
    yield
    deprecations_module._LOGGED.clear()


def test_a_legacy_enabled_root_setting_still_starts_phoenix_first_and_warns(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fresh_deprecation_channel: None,
) -> None:
    (checkout / ".env").write_text("AC2_MODEL_PROVIDER=fake\nEAP_PHOENIX_ENABLED=true\n")
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    with caplog.at_level(logging.WARNING, logger="ai_agent_lib_core.config"):
        assert local_dev.main(["services"], root=checkout) == 0
    assert [s.name for s in start.call_args.args[0]] == ["phoenix", "rmprep-data", "rmprep"]
    assert [r.getMessage() for r in caplog.records if r.name == "ai_agent_lib_core.config"] == [
        "EAP_PHOENIX_ENABLED is deprecated; use AC2_PHOENIX_ENABLED instead"
    ]


def test_a_legacy_shell_disable_still_wins_over_an_enabled_root_file(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_runtime.package_folder(checkout, "phoenix").rmdir()
    monkeypatch.setenv("EAP_PHOENIX_ENABLED", "false")
    (checkout / ".env").write_text("EAP_PHOENIX_ENABLED=false\n")
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    assert local_dev.main(["services"], root=checkout) == 0
    assert [s.name for s in start.call_args.args[0]] == ["rmprep-data", "rmprep"]


@pytest.mark.parametrize("file_owner", ["service", "launcher"])
def test_a_legacy_separate_settings_file_is_resolved_as_the_agent_would_resolve_it(
    checkout: Path, monkeypatch: pytest.MonkeyPatch, file_owner: str
) -> None:
    (checkout / ".env").write_text("")
    owner = checkout / (local_dev.AGENT_ENV if file_owner == "service" else ".env")
    owner.write_text("EAP_PHOENIX_ENV_FILE=phoenix.env\n")
    base = owner.parent if file_owner == "service" else checkout / local_dev.WORKSPACE
    (base / "phoenix.env").write_text(
        "EAP_PHOENIX_ENABLED=true\nPHOENIX_COLLECTOR_ENDPOINT=http://localhost:6006/\n"
    )
    monkeypatch.chdir(checkout.parent)
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    assert local_dev.main(["services"], root=checkout) == 0
    assert start.call_args.args[0][0].name == "phoenix"


def test_a_legacy_shell_file_path_is_relative_to_the_agent_like_the_canonical_one(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (checkout / ".env").write_text("")
    workspace = checkout / local_dev.WORKSPACE
    (workspace / "phoenix.env").write_text("AC2_PHOENIX_ENABLED=true\n")
    monkeypatch.setenv("EAP_PHOENIX_ENV_FILE", "phoenix.env")
    monkeypatch.chdir(checkout.parent)
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    assert local_dev.main(["services"], root=checkout) == 0
    assert start.call_args.args[0][0].name == "phoenix"


@pytest.mark.parametrize("variable", ["ENABLED", "ENV_FILE"])
def test_two_phoenix_spellings_that_disagree_stop_startup_naming_both(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
) -> None:
    (checkout / ".env").write_text(
        f"AC2_PHOENIX_{variable}={'true' if variable == 'ENABLED' else 'one.env'}\n"
        f"EAP_PHOENIX_{variable}={'false' if variable == 'ENABLED' else 'two.env'}\n"
    )
    start = Mock()
    monkeypatch.setattr(local_dev, "serve", start)
    assert local_dev.main(["services"], root=checkout) == 1
    start.assert_not_called()
    output = capsys.readouterr().out
    assert (
        f"EAP_PHOENIX_{variable} is deprecated and conflicts with AC2_PHOENIX_{variable}" in output
    )
    assert "root-private-key" not in output
