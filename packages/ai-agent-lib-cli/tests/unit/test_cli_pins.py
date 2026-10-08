"""Reading the pins a generated server prints, from a separate process."""

from __future__ import annotations

from pathlib import Path

import pytest

from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.pins import read_pins

PIN = "ab" * 32


def service(tmp_path: Path, body: str) -> Path:
    package = tmp_path / "src" / "hello_mcp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "pins.py").write_text(body, encoding="utf-8")
    return tmp_path


def test_the_pins_a_server_prints_are_read(tmp_path: Path) -> None:
    body = (
        "import pathlib\n"
        "assert pathlib.Path('src').is_dir()  # it runs in the service's folder\n"
        f"print('hello.greet: {PIN}')\n"
        "print('a line that is not a pin')\n"
        f"print('hello.people_by_team: {PIN}')\n"
    )
    assert read_pins(service(tmp_path, body), "hello_mcp") == {
        "hello.greet": PIN,
        "hello.people_by_team": PIN,
    }


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("raise SystemExit('the data folder is missing')\n", "the data folder is missing"),
        ("print('nothing useful')\n", "no output"),
        (f"print('hello.greet: {PIN}')\nraise RuntimeError('late failure')\n", "late failure"),
    ],
)
def test_a_server_that_cannot_report_its_pins_is_an_error(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(CliError, match=message):
        read_pins(service(tmp_path, body), "hello_mcp")


def test_a_server_that_is_not_there_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(CliError, match="could not be pinned"):
        read_pins(tmp_path, "no_such_package")
    with pytest.raises(CliError, match="could not be pinned"):
        read_pins(tmp_path / "no-such-folder", "hello_mcp")
