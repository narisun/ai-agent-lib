"""Build the code artifact of an AgentCore Runtime deployment: a zip of the service and its wheels.

The runtime unpacks the zip and runs its entry point on Python 3.13 for ARM64,
so the dependencies must be wheels built for that platform: pip is asked for
those and nothing else (``--only-binary=:all:``), and refuses, loudly, a
dependency that has none. The service's own code is installed from its
folder without its dependencies, which the lock already holds, and a
``main.py`` shim starts it with its ``agentcore`` profile.

Every subprocess goes through a runner so that the layout, the shim and the
file modes can be tested without a network. Nothing here needs boto3 unless
the zip is uploaded, and then it is imported only at that point.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agentcraft2_cli.deploy.agentcore import AGENTCORE_PYTHON
from agentcraft2_cli.errors import CliError

__all__ = ["Runner", "ZipBuild", "build_zip", "main", "run_command", "shim", "upload_zip"]

Runner = Callable[[Sequence[str]], None]
"""Runs a command line and raises :class:`CliError` when it fails."""

PLATFORM = "manylinux2014_aarch64"
"""The wheel platform AgentCore Runtime runs: ARM64 Linux."""

ADOT = "aws-opentelemetry-distro>=0.18.0"
"""The distribution whose ``opentelemetry-instrument`` the runtime's entry point runs."""

FILE_MODE = 0o644
DIRECTORY_MODE = 0o755
_UNIX = 3  # ZipInfo.create_system for POSIX modes to be honoured
_DIRECTORY_FLAG = 0x10  # MS-DOS directory attribute, set on a directory entry
_SITE = "site"
_PROFILE = ("--profile", "agentcore")


@dataclass(frozen=True, slots=True)
class ZipBuild:
    """What one build takes.

    Attributes:
        service: The service's folder, with its ``pyproject.toml``.
        lock: The hash-locked requirements of the service.
        entry_module: The module whose ``main`` serves the service.
        build: A folder for the build tree; emptied first.
        output: The zip to write.
        python_version: The Python the wheels are for.
    """

    service: Path
    lock: Path
    entry_module: str
    build: Path
    output: Path
    python_version: str = AGENTCORE_PYTHON


def run_command(command: Sequence[str]) -> None:
    """Run ``command`` and raise :class:`CliError` with its last lines when it fails."""
    done = subprocess.run(  # noqa: S603 - a command line this module composed, no shell
        list(command), check=False, capture_output=True, text=True
    )
    if done.returncode != 0:
        tail = "\n".join((done.stderr or done.stdout).strip().splitlines()[-12:])
        raise CliError(
            f"{Path(command[0]).name} {' '.join(command[1:3])} failed ({done.returncode})",
            expected="every dependency as a wheel for the runtime's platform",
            actual=tail or "no output",
            fix=(
                f"a dependency without a wheel for Python {AGENTCORE_PYTHON} on {PLATFORM} cannot "
                "go into a code artifact; replace it, or deploy a container image instead"
            ),
        )


def _pip(python: str, *arguments: str) -> list[str]:
    return [python, "-m", "pip", *arguments, "--disable-pip-version-check", "--no-cache-dir"]


def _foreign(site: Path, build: ZipBuild) -> list[str]:
    """The flags that make pip install wheels for the runtime's platform into ``site``."""
    return [
        "--target",
        str(site),
        "--platform",
        PLATFORM,
        "--implementation",
        "cp",
        "--python-version",
        build.python_version,
        "--only-binary=:all:",
    ]


def shim(entry_module: str) -> str:
    """Return ``main.py``: what the runtime's entry point runs."""
    return (
        f'"""Starts the service the way AgentCore Runtime expects: its agentcore profile."""\n'
        "\n"
        "import sys\n"
        "\n"
        f"from {entry_module} import main\n"
        "\n"
        'if __name__ == "__main__":\n'
        f"    sys.exit(main({list(_PROFILE)!r}))\n"
    )


def _write_zip(site: Path, output: Path) -> int:
    """Zip ``site`` with POSIX modes the runtime can read; return the number of entries."""
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(site.rglob("*")):
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(site).as_posix()
            if path.is_dir():
                info = zipfile.ZipInfo(relative + "/")
                info.create_system = _UNIX
                info.external_attr = (DIRECTORY_MODE << 16) | _DIRECTORY_FLAG
                archive.writestr(info, b"")
            else:
                info = zipfile.ZipInfo.from_file(path, relative)
                info.create_system = _UNIX
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = FILE_MODE << 16
                with path.open("rb") as source, archive.open(info, "w") as member:
                    shutil.copyfileobj(source, member)
            entries += 1
    return entries


def build_zip(build: ZipBuild, run: Runner = run_command, python: str = sys.executable) -> Path:
    """Build the zip and return its path.

    Args:
        build: What to build.
        run: Runs pip.
        python: The interpreter whose pip runs. Any Python with pip will do:
            the wheels are chosen for the runtime's platform, not this one.

    Raises:
        CliError: If the lock or the service folder is missing, or pip fails.
    """
    for what, path in (("the requirements lock", build.lock), ("the service", build.service)):
        if not path.exists():
            raise CliError(f"{what} was not found at {path}")
    if build.build.exists():
        shutil.rmtree(build.build)
    site = build.build / _SITE
    site.mkdir(parents=True)
    foreign = _foreign(site, build)
    run(_pip(python, "install", *foreign, "--require-hashes", "-r", str(build.lock)))
    # The entry point runs the service under ADOT, whatever the telemetry setting.
    run(_pip(python, "install", *foreign, "--upgrade", ADOT))
    # The service itself: pure Python, so this Python builds it; its dependencies are above.
    run(
        _pip(python, "install", "--target", str(site), "--upgrade", "--no-deps", str(build.service))
    )
    (site / "main.py").write_text(shim(build.entry_module), encoding="utf-8", newline="\n")
    _write_zip(site, build.output)
    return build.output


def upload_zip(output: Path, bucket: str, key: str) -> str:
    """Upload the zip and return the version ID S3 gave it.

    Raises:
        CliError: If boto3 is not installed.
    """
    try:
        import boto3
    except ImportError as exc:
        raise CliError(
            "uploading needs boto3, which this Python does not have",
            fix="install boto3, or upload the zip with the AWS CLI: aws s3 cp",
        ) from exc
    response = boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=output.read_bytes())
    version = response.get("VersionId", "")
    return str(version)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_zip.py", description="Build the code artifact of an AgentCore deployment."
    )
    parser.add_argument("--service", required=True, type=Path, help="the service's folder")
    parser.add_argument("--lock", required=True, type=Path, help="its requirements.lock")
    parser.add_argument("--entry", required=True, help="the module whose main serves it")
    parser.add_argument("--output", required=True, type=Path, help="the zip to write")
    parser.add_argument("--build", type=Path, default=None, help="the build folder")
    parser.add_argument("--python-version", default=AGENTCORE_PYTHON, help="the runtime's Python")
    parser.add_argument("--upload", action="store_true", help="also upload the zip to S3")
    parser.add_argument("--bucket", help="the bucket to upload to")
    parser.add_argument("--key", help="the object key; default: the zip's name")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build, and on request upload, the code artifact. Returns the exit code."""
    arguments = _parser().parse_args(argv)
    build = ZipBuild(
        service=arguments.service,
        lock=arguments.lock,
        entry_module=arguments.entry,
        build=arguments.build or arguments.output.parent / "build",
        output=arguments.output,
        python_version=arguments.python_version,
    )
    try:
        output = build_zip(build)
        sys.stdout.write(f"wrote {output} ({output.stat().st_size} bytes)\n")
        if arguments.upload:
            if not arguments.bucket:
                raise CliError("--upload needs --bucket")
            key = arguments.key or output.name
            version = upload_zip(output, arguments.bucket, key)
            sys.stdout.write(f"uploaded s3://{arguments.bucket}/{key} (version {version})\n")
    except CliError as problem:
        sys.stderr.write(f"build_zip: error: {problem}\n")
        return 1
    return 0
