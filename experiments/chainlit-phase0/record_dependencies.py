"""Record installed dependency versions, declared licenses and local wheel hashes.

Run with the sample's Python after installing requirements-checks.txt. This is an
inventory of declared metadata, not a security scan or a license approval.
"""

import hashlib
import importlib.metadata
import json
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parent


def closure(roots: list[str]) -> dict:
    """Resolve the installed graph, evaluating markers and requested extras."""
    pending = [Requirement(value) for value in roots]
    seen, result = set(), {}
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        key = (name, tuple(sorted(requirement.extras)))
        if key in seen:
            continue
        seen.add(key)
        dist = importlib.metadata.distribution(name)
        result[name] = dist
        for value in dist.requires or []:
            child = Requirement(value)
            if child.marker is None or any(
                child.marker.evaluate({"extra": extra}) for extra in ["", *requirement.extras]
            ):
                pending.append(child)
    return result


def record() -> None:
    """Write reproducible pins and an inventory without reading credentials."""
    runtime = closure(["chainlit", "httpx"])
    checks = closure(
        ["chainlit", "httpx", "pytest", "pytest-asyncio", "python-socketio[asyncio_client]"]
    )
    evidence = ROOT / "evidence"
    evidence.mkdir(exist_ok=True)
    header = "# Generated on Windows / Python 3.11; regenerate when dependencies change.\n"
    (ROOT / "requirements.lock").write_text(
        header + "".join(f"{name}=={dist.version}\n" for name, dist in sorted(runtime.items())),
        encoding="utf-8",
    )
    (ROOT / "requirements-checks.lock").write_text(
        header
        + "-r requirements.lock\n"
        + "".join(
            f"{name}=={dist.version}\n"
            for name, dist in sorted(checks.items())
            if name not in runtime
        )
        + f"python-socketio[asyncio_client]=={checks['python-socketio'].version}\n",
        encoding="utf-8",
    )
    rows = []
    for name, dist in sorted(checks.items()):
        metadata = dist.metadata
        rows.append(
            {
                "name": name,
                "version": dist.version,
                "scope": "runtime" if name in runtime else "checks",
                "license_expression": metadata.get("License-Expression"),
                "license_summary": (metadata.get("License") or "").splitlines()[0:1],
                "license_classifiers": [
                    s for s in metadata.get_all("Classifier", []) if s.startswith("License")
                ],
                "license_files": metadata.get_all("License-File", []),
            }
        )
    (evidence / "dependencies.json").write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    wheels = []
    for path in sorted((ROOT / ".agentlib/wheelhouse").glob("*.whl")):
        name, version, _, _ = parse_wheel_filename(path.name)
        if name in checks and str(version) == checks[name].version:
            wheels.append(
                {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            )
    (evidence / "wheels.json").write_text(json.dumps(wheels, indent=2) + "\n", encoding="utf-8")
    print(  # noqa: T201 - command-line inventory summary
        f"Recorded {len(runtime)} runtime packages, {len(checks)} total packages "
        f"and {len(wheels)} wheels."
    )


if __name__ == "__main__":
    record()
