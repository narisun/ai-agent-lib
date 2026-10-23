"""The code artifact of an AgentCore deployment, built with a fake pip: layout, shim, modes."""

from __future__ import annotations

import stat
import sys
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from agentcraft2_cli.deploy import zipbuild
from agentcraft2_cli.deploy.zipbuild import (
    ADOT,
    PLATFORM,
    ZipBuild,
    build_zip,
    run_command,
    shim,
    upload_zip,
)
from agentcraft2_cli.errors import CliError


class FakePip:
    """Records each command and lays files out in the target folder, as pip would."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> None:
        arguments = list(command)
        self.commands.append(arguments)
        target = Path(arguments[arguments.index("--target") + 1])
        if "--require-hashes" in arguments:
            (target / "pydantic").mkdir(parents=True)
            (target / "pydantic" / "__init__.py").write_text("# a wheel's module\n")
            (target / "pydantic" / "__pycache__").mkdir()
            (target / "pydantic" / "__pycache__" / "__init__.cpython-313.pyc").write_bytes(b"\0")
        elif ADOT in arguments:
            (target / "bin").mkdir(parents=True)
            (target / "bin" / "opentelemetry-instrument").write_text("#!python\n")
        else:
            (target / "helper").mkdir(parents=True)
            (target / "helper" / "__init__.py").write_text("")
            (target / "helper" / "service.py").write_text("def main(argv=None): ...\n")


def _build(tmp_path: Path) -> ZipBuild:
    service = tmp_path / "agents" / "helper"
    service.mkdir(parents=True)
    (service / "pyproject.toml").write_text('[project]\nname = "helper"\n')
    lock = tmp_path / "deploy" / "helper" / "requirements.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("pydantic==2.0 --hash=sha256:00\n")
    return ZipBuild(
        service=service,
        lock=lock,
        entry_module="helper.service",
        build=tmp_path / "deploy" / "helper" / "build",
        output=tmp_path / "deploy" / "helper" / "helper.zip",
    )


def test_build_zip_asks_pip_for_arm64_wheels_then_the_service_then_writes_the_shim(
    tmp_path: Path,
) -> None:
    pip = FakePip()
    build = _build(tmp_path)
    output = build_zip(build, run=pip, python="py")
    assert output == build.output

    lock, adot, service = pip.commands
    site = str(build.build / "site")
    foreign = [
        "--target",
        site,
        "--platform",
        PLATFORM,
        "--implementation",
        "cp",
        "--python-version",
        "3.13",
        "--only-binary=:all:",
    ]
    assert lock[:4] == ["py", "-m", "pip", "install"]
    assert lock[4 : 4 + len(foreign)] == foreign
    assert lock[4 + len(foreign) :] == [
        "--require-hashes",
        "-r",
        str(build.lock),
        "--disable-pip-version-check",
        "--no-cache-dir",
    ]
    assert adot[4 : 4 + len(foreign)] == foreign
    assert ADOT in adot
    assert service[4:] == [
        "--target",
        site,
        "--upgrade",
        "--no-deps",
        str(build.service),
        "--disable-pip-version-check",
        "--no-cache-dir",
    ]
    assert "--platform" not in service  # the service is pure Python, built here

    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert "main.py" in names
        assert "helper/service.py" in names
        assert "pydantic/__init__.py" in names
        assert "bin/opentelemetry-instrument" in names
        assert not any("__pycache__" in name for name in names)
        assert "pydantic/" in names
        assert "helper/" in names
        for info in archive.infolist():
            mode = info.external_attr >> 16
            if info.is_dir():
                assert stat.S_IMODE(mode) == 0o755, info.filename
            else:
                assert stat.S_IMODE(mode) == 0o644, info.filename
            assert info.create_system == 3
        assert archive.read("main.py").decode() == shim("helper.service")


def test_the_shim_starts_the_service_with_its_agentcore_profile() -> None:
    text = shim("helper.service")
    assert "from helper.service import main\n" in text
    assert "sys.exit(main(['--profile', 'agentcore']))" in text
    compile(text, "main.py", "exec")


def test_build_zip_empties_the_build_folder_first(tmp_path: Path) -> None:
    build = _build(tmp_path)
    stale = build.build / "site" / "stale.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("")
    build_zip(build, run=FakePip(), python="py")
    assert not stale.exists()


def test_build_zip_needs_the_lock_and_the_service(tmp_path: Path) -> None:
    build = _build(tmp_path)
    build.lock.unlink()
    with pytest.raises(CliError, match="the requirements lock was not found"):
        build_zip(build, run=FakePip(), python="py")


def test_run_command_explains_a_failing_pip_with_its_last_lines() -> None:
    with pytest.raises(CliError) as caught:
        run_command(
            [
                sys.executable,
                "-c",
                "import sys; print('no wheel for x', file=sys.stderr); sys.exit(3)",
            ]
        )
    assert "failed (3)" in caught.value.message
    assert caught.value.actual == "no wheel for x"
    assert "cannot go into a code artifact" in (caught.value.fix or "")


def test_upload_needs_boto3_and_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "boto3", None)
    archive = tmp_path / "helper.zip"
    archive.write_bytes(b"")
    with pytest.raises(CliError, match="needs boto3"):
        upload_zip(archive, "bucket", "helper.zip")


def test_main_builds_from_the_arguments_and_uploads_only_when_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    built: list[ZipBuild] = []
    uploads: list[tuple[Path, str, str]] = []

    def fake_build(build: ZipBuild, run: object = None, python: str = "") -> Path:
        built.append(build)
        build.output.parent.mkdir(parents=True, exist_ok=True)
        build.output.write_bytes(b"zip")
        return build.output

    def fake_upload(output: Path, bucket: str, key: str) -> str:
        uploads.append((output, bucket, key))
        return "v1"

    monkeypatch.setattr(zipbuild, "build_zip", fake_build)
    monkeypatch.setattr(zipbuild, "upload_zip", fake_upload)
    arguments = [
        "--service",
        "agents/helper",
        "--lock",
        "deploy/helper/requirements.lock",
        "--entry",
        "helper.service",
        "--output",
        str(tmp_path / "helper.zip"),
    ]
    assert zipbuild.main(arguments) == 0
    assert built[0].entry_module == "helper.service"
    assert built[0].build == tmp_path / "build"
    assert built[0].python_version == "3.13"
    assert uploads == []
    assert "wrote" in capsys.readouterr().out

    assert zipbuild.main([*arguments, "--upload"]) == 1
    assert "--upload needs --bucket" in capsys.readouterr().err
    assert zipbuild.main([*arguments, "--upload", "--bucket", "b", "--python-version", "3.12"]) == 0
    assert uploads == [(tmp_path / "helper.zip", "b", "helper.zip")]
    assert built[-1].python_version == "3.12"
    assert "uploaded s3://b/helper.zip (version v1)" in capsys.readouterr().out
