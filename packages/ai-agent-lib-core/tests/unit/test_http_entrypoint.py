"""The entry-point wiring: configuration, logging, telemetry, the coroutine, the exit code."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from ai_agent_lib_core.contracts import ConfigurationError, ServiceConfig, ValidationFailed
from ai_agent_lib_core.integrations.http import (
    EXIT_INTERRUPTED,
    entrypoint,
    question_input,
    run_command,
    run_service,
)


class Recorder:
    """Stands in for the helpers the entry point composes and records the order of calls."""

    def __init__(self, config: ServiceConfig | None = None) -> None:
        self.config = config if config is not None else ServiceConfig.for_testing()
        self.calls: list[tuple[str, Any]] = []

    def load_service_config(self, env_file: Path | None) -> ServiceConfig:
        self.calls.append(("load", env_file))
        return self.config

    def configure_logging(self, service: str, **options: Any) -> logging.Handler:
        self.calls.append(("logging", (service, options)))
        return logging.NullHandler()

    def start_telemetry(self, service: str, config: ServiceConfig | None) -> Any:
        self.calls.append(("telemetry", (service, config)))

        def stop() -> None:
            self.calls.append(("stop", None))

        return stop

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(entrypoint, "load_service_config", self.load_service_config)
        monkeypatch.setattr(entrypoint, "configure_logging", self.configure_logging)
        monkeypatch.setattr(entrypoint, "start_telemetry", self.start_telemetry)

    @property
    def steps(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()
    recorder.install(monkeypatch)
    return recorder


# ------------------------------------------------------------------ run_service


def test_a_service_sets_up_logging_then_telemetry_and_stops_telemetry_after_serving(
    recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    served: list[ServiceConfig] = []

    async def serve(config: ServiceConfig) -> None:
        served.append(config)
        recorder.calls.append(("serve", config))

    code = run_service("accounts-agent", serve, env_file=Path("elsewhere/.env"))

    assert code == 0
    assert served == [recorder.config]
    assert recorder.steps == ["load", "logging", "telemetry", "serve", "stop"]
    assert recorder.calls[0] == ("load", Path("elsewhere/.env"))
    # Logging is set up for the application, and details follow the environment.
    assert recorder.calls[1] == ("logging", ("accounts-agent", {"details": True}))
    assert recorder.calls[2] == ("telemetry", ("accounts-agent", recorder.config))
    assert capsys.readouterr() == ("", "")


def test_telemetry_is_stopped_and_a_library_error_is_explained_when_serving_fails(
    recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    async def serve(config: ServiceConfig) -> None:
        raise ConfigurationError("the port is taken", expected="a free port", actual="8000 in use")

    code = run_service("accounts-agent", serve, program="accounts-serve")

    assert code == 1
    assert recorder.steps == ["load", "logging", "telemetry", "stop"]
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("accounts-serve: ConfigurationError: the port is taken\n")
    assert "  expected: a free port\n" in err
    assert "Traceback" not in err


def test_the_program_name_defaults_to_the_application(
    recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    async def serve(config: ServiceConfig) -> None:
        raise ConfigurationError("no model")

    assert run_service("accounts-agent", serve) == 1
    assert capsys.readouterr().err.startswith("accounts-agent: ConfigurationError: no model\n")


def test_an_interrupted_service_exits_130_and_says_nothing(
    recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    async def serve(config: ServiceConfig) -> None:
        raise KeyboardInterrupt

    code = run_service("accounts-agent", serve)

    assert code == EXIT_INTERRUPTED == 130
    assert recorder.steps == ["load", "logging", "telemetry", "stop"]
    assert capsys.readouterr() == ("", "")


def test_a_configuration_that_does_not_load_is_reported_with_details_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = Recorder()
    recorder.install(monkeypatch)

    def failing_load(env_file: Path | None) -> ServiceConfig:
        raise RuntimeError("the .env file is not UTF-8")

    monkeypatch.setattr(entrypoint, "load_service_config", failing_load)

    async def serve(config: ServiceConfig) -> None:
        raise AssertionError("never reached")

    code = run_service("accounts-agent", serve, program="accounts-serve")

    assert code == 1
    # Nothing was set up: there is no configuration to set it up from.
    assert recorder.steps == []
    err = capsys.readouterr().err
    # With no configuration there is nothing a caller sent to hide, so a bug's
    # traceback comes first and the error's text is shown.
    assert err.startswith("Traceback")
    assert "accounts-serve: RuntimeError: the .env file is not UTF-8" in err


# ------------------------------------------------------------------ run_command


def test_a_command_prints_its_answer_and_sets_up_neither_logging_nor_telemetry(
    recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    async def run(config: ServiceConfig) -> str:
        assert config is recorder.config
        return "Hello, Ada."

    code = run_command("hello-agent", run, env_file=Path("elsewhere/.env"))

    assert code == 0
    assert recorder.calls == [("load", Path("elsewhere/.env"))]
    assert capsys.readouterr() == ("Hello, Ada.\n", "")


def test_a_command_that_returns_nothing_prints_nothing(
    recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    async def run(config: ServiceConfig) -> None:
        return None

    assert run_command("hello-agent", run) == 0
    assert capsys.readouterr() == ("", "")


def test_a_failing_command_is_explained_under_its_name(
    recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    async def run(config: ServiceConfig) -> str:
        raise ValidationFailed("the answer is not text")

    assert run_command("hello-agent", run) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("hello-agent: ValidationFailed: the answer is not text\n")


def test_an_interrupted_command_exits_130(
    recorder: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    async def run(config: ServiceConfig) -> str:
        raise KeyboardInterrupt

    assert run_command("hello-agent", run) == EXIT_INTERRUPTED
    assert capsys.readouterr() == ("", "")


# --------------------------------------------------------------- question_input


def test_a_question_is_taken_from_the_request_body() -> None:
    assert question_input({"question": "x"}) == "x"
    assert question_input({"question": "  spaced  ", "extra": 1}) == "  spaced  "


EMPTY = [None, {}, {"question": ""}, {"question": "   "}, {"question": 3}]


@pytest.mark.parametrize("given", EMPTY)
def test_a_body_without_a_question_is_refused(given: Any) -> None:
    with pytest.raises(ValidationFailed) as caught:
        question_input(given)
    error = caught.value
    assert error.message == "the input is not a question"
    assert error.expected == 'an object such as {"question": "Say hello to Ada."}'
    assert error.actual == "no non-empty 'question' in it"


def test_the_example_question_in_the_explanation_can_be_changed() -> None:
    with pytest.raises(ValidationFailed) as caught:
        question_input({}, example="Which accounts are overdue?")
    assert caught.value.expected == 'an object such as {"question": "Which accounts are overdue?"}'
