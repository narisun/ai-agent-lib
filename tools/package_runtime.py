"""Run incompatible service dependencies with one existing Python and its pip.

Packages live in each component's .venv folder. The caller's default Python
loads them directly; local Python and pip executables are never selected.
"""

from __future__ import annotations

import importlib.util
import json
import os
import runpy
import shutil
import site
import subprocess
import sys
import sysconfig
from collections.abc import Sequence
from pathlib import Path
from uuid import uuid4

ENVIRONMENTS = {
    "agent": Path(".venv"),
    "ui": Path("packages/ai-agent-lib-ui/.venv"),
    "phoenix": Path(".agentlib/phoenix/.venv"),
}
MANIFEST = ".python-runtime.json"


def runtime_identity() -> dict[str, object]:
    """Identify the interpreter ABI that installed the service's wheels."""
    return {
        "implementation": sys.implementation.name,
        "version": list(sys.version_info[:2]),
        "platform": sysconfig.get_platform(),
    }


def package_folder(root: Path, component: str) -> Path:
    """Return the dependency folder owned by one supported local service."""
    if component not in ENVIRONMENTS:
        raise ValueError(f"Unknown package runtime: {component}")
    packages = (
        Path("Lib/site-packages")
        if sys.platform == "win32"
        else Path(f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    )
    return root / ENVIRONMENTS[component] / packages


def validate_packages(folder: Path) -> None:
    """Reuse compatible existing environments without executing their binaries."""
    if not folder.is_dir():
        raise ValueError(
            f"Service packages are missing: {folder}. "
            "Run setup-local.bat, or start-phoenix.bat setup for Phoenix."
        )
    manifest = folder / MANIFEST
    if manifest.is_file():
        compatible = json.loads(manifest.read_text("utf-8")) == runtime_identity()
    else:
        # Standard venv and uv-created environments use different version keys.
        environment = folder.parents[1 if sys.platform == "win32" else 2]
        config = environment / "pyvenv.cfg"
        values = {}
        if config.is_file():
            values = {
                key.strip(): value.strip()
                for line in config.read_text("utf-8").splitlines()
                for key, separator, value in [line.partition("=")]
                if separator
            }
        version = values.get("version", values.get("version_info", ""))
        # Reuse requires the same base installation, which also establishes ABI
        # and architecture. Never run or copy a binary from the old environment.
        home = values.get("home", "")
        compatible = (
            version.split(".")[:2] == [str(part) for part in sys.version_info[:2]]
            and bool(home)
            and Path(home).resolve() == Path(sys.base_prefix).resolve()
        )
    if not compatible:
        raise ValueError(
            "Service packages were prepared for a different Python or need setup. "
            "Rerun setup-local.bat or start-phoenix.bat setup with the default Python."
        )


def command(root: Path, component: str, *arguments: str) -> list[str]:
    """Use the current interpreter with only the service's package search paths."""
    return [
        sys.executable,
        "-I",
        "-S",
        "-u",
        # Isolated mode ignores PYTHONUTF8. Keep redirected service logs and
        # first-run database migration output readable on Windows as UTF-8.
        "-X",
        "utf8",
        str(root / "tools/package_runtime.py"),
        "--packages",
        str(package_folder(root, component)),
        *arguments,
    ]


def wait_for_child(arguments: Sequence[str]) -> int:
    """Let the child finish graceful cleanup after a shared terminal interrupt."""
    child = subprocess.Popen(arguments)  # noqa: S603 - caller supplies an explicit Python command
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            # Ctrl+C also reaches the child in this console process group.
            # subprocess.call would kill it as it unwinds its service lifespans.
            continue


def _remove_packages(path: Path, parent: Path) -> None:
    """Remove only a staging/backup folder inside this setup's dependency directory."""
    if path.resolve().parent != parent.resolve() or path.is_symlink():
        raise ValueError("Refusing to remove a package folder outside its setup directory")
    if path.exists():
        shutil.rmtree(path)


def install(root: Path, component: str, requirements: Sequence[str]) -> None:
    """Build and check a fresh package set, replacing the old one only after success."""
    destination = package_folder(root, component)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f".{component}-{uuid4().hex}")
    previous = destination.with_name(f".{component}-previous-{uuid4().hex}")
    pip_spec = importlib.util.find_spec("pip")
    if pip_spec is None or pip_spec.origin is None:
        raise ValueError("The selected Python needs the enterprise-provided pip package.")
    try:
        subprocess.run(  # noqa: S603 - current interpreter and declared requirements, no shell
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--no-user",  # target folders also work when pip config has user=true
                "--target",
                str(staging),
                *requirements,
            ],
            cwd=root,
            check=True,
        )
        (staging / MANIFEST).write_text(json.dumps(runtime_identity()), encoding="utf-8")
        subprocess.run(  # noqa: S603 - check using the existing pip module, without copying it
            [
                sys.executable,
                "-I",
                "-S",
                str(root / "tools/package_runtime.py"),
                "--packages",
                str(staging),
                "--check",
                pip_spec.origin,
            ],
            cwd=root,
            check=True,
        )
        if destination.exists():
            destination.rename(previous)
        try:
            staging.rename(destination)
        except OSError:
            if previous.exists():
                previous.rename(destination)
            raise
    finally:
        _remove_packages(staging, destination.parent)
    _remove_packages(previous, destination.parent)


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch a service command, or bootstrap it after isolated, no-site startup."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 2:
        raise ValueError("Usage: python tools/package_runtime.py agent|ui|phoenix -m MODULE [args]")
    if arguments[0] != "--packages":
        component = arguments.pop(0)
        root = Path(__file__).resolve().parents[1]
        if arguments[0] == "--install":
            if len(arguments) == 1:
                raise ValueError("--install needs pip requirements, for example -e PACKAGE[chat]")
            install(root, component, arguments[1:])
            return 0
        if arguments == ["--check"]:
            pip_spec = importlib.util.find_spec("pip")
            if pip_spec is None or pip_spec.origin is None:
                raise ValueError("The selected Python needs its enterprise-provided pip package.")
            arguments.append(pip_spec.origin)
        return wait_for_child(command(root, component, *arguments))
    if not sys.flags.isolated or not sys.flags.no_site:
        raise ValueError("The package bootstrap requires Python's -I -S options.")
    arguments.pop(0)
    folder = Path(arguments.pop(0)).resolve()
    validate_packages(folder)
    # -I -S excludes global/user site-packages and PYTHONPATH. addsitedir also
    # handles the .pth files created by pip's editable project installations.
    before = set(sys.path)
    site.addsitedir(str(folder))
    # CLI tools spawn this same Python to inspect generated graphs and MCP
    # servers. Carry the resolved package and editable-source paths to those
    # children. Do not restore a caller's unrelated PYTHONPATH.
    os.environ["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path not in before)
    if arguments[0] == "--check":
        # Import only the existing pip package, without exposing its parent
        # site-packages directory to the isolated dependency check.
        spec = importlib.util.spec_from_file_location("pip", arguments[1])
        if spec is None or spec.loader is None:
            raise ValueError("Could not load the selected Python's pip module.")
        module = importlib.util.module_from_spec(spec)
        sys.modules["pip"] = module
        spec.loader.exec_module(module)
        arguments = ["-m", "pip", "check"]
    if arguments[0] == "-m":
        # Match python -m's lookup of workspace modules (for example tools/),
        # after the package set has been isolated from global site-packages.
        sys.path.insert(0, str(Path.cwd()))
        sys.argv = arguments[1:]
        runpy.run_module(arguments[1], run_name="__main__", alter_sys=True)
    else:
        sys.argv = arguments
        runpy.run_path(arguments[0], run_name="__main__")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.stderr.write(f"Package setup: {exc}\n")
        raise SystemExit(1) from None
