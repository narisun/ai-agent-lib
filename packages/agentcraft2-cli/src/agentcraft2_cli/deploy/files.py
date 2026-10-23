"""The files a deployment is made of, and writing them without losing the developer's work.

A deployment is a Terraform root module for the service. On Fargate it sits
beside a reference module that runs the service as an ECS service and, when
the task needs the OPA sidecar, the sidecar's Dockerfile with the platform's
Rego bundle. On AgentCore Runtime the root module stands alone: the runtime,
its role, its artifact store and, when asked for, a Memory and a Gateway.
Two files are derived from the settings and rewritten on every run;
everything else is written once and is the developer's from then on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentcraft2_cli.deploy.agentcore import AGENTCORE_PYTHON
from agentcraft2_cli.deploy.hcl import agentcore_settings_tf, permissions_tf, settings_tf
from agentcraft2_cli.deploy.plan import DeploymentPlan
from agentcraft2_cli.deploy.target import MODULE_FOLDER, OPA_FOLDER, Target
from agentcraft2_cli.errors import CliError
from agentcraft2_cli.render import Report, TemplateRenderer, write_files
from agentcraft2_cli.workspace import WorkspaceAnswers

__all__ = [
    "DeploymentReport",
    "deployment_files",
    "maintained_files",
    "read_bundle",
    "write_deployment",
]

_HEADER = (
    "# Written by 'agentcraft2 deploy {name}{target}' from {source}.\n"
    "# Do not edit: change that file and run the command again.\n"
)
_REGO_TEST_SUFFIX = "_test.rego"
_IMAGE_FILES = frozenset({"Dockerfile", "Dockerfile.dockerignore"})
_ZIP_FILES = frozenset({"build_zip.py"})


@dataclass(frozen=True, slots=True)
class DeploymentReport:
    """What writing a deployment did.

    Attributes:
        written: The files written: created, updated or already as they should be.
        kept: Files the deployment produces that already existed and were left
            as they are, because they are the developer's from their first run.
    """

    written: Report
    kept: tuple[PurePosixPath, ...] = ()


def maintained_files(target: Target) -> frozenset[PurePosixPath]:
    """The files ``agentcraft2 deploy`` rewrites every time; the rest are written once."""
    folder = target.deployment
    return frozenset({folder / "settings.tf", folder / "permissions.tf"})


def _context(target: Target) -> dict[str, object]:
    return {
        "name": target.name,
        "kind": target.kind,
        "folder": target.folder.as_posix(),
        "package": target.package,
        "entry_module": target.entry_module,
        "protocol": target.protocol,
        "port": target.port,
        "command_json": json.dumps(list(target.command)),
        "python": AGENTCORE_PYTHON,
    }


def _fargate_files(
    plan: DeploymentPlan, bundle: Mapping[PurePosixPath, str], renderer: TemplateRenderer
) -> dict[PurePosixPath, str]:
    target = plan.target
    render = renderer.render
    files = {MODULE_FOLDER / path: text for path, text in render("deploy-module", {}).items()}
    folder = target.deployment
    context = _context(target)
    files.update({folder / path: text for path, text in render("deploy-service", context).items()})
    header = _HEADER.format(name=target.name, target="", source=target.settings)
    files[folder / "settings.tf"] = settings_tf(plan, header)
    files[folder / "permissions.tf"] = permissions_tf(plan, header)
    if plan.opa:
        files.update({OPA_FOLDER / path: text for path, text in render("deploy-opa", {}).items()})
        files.update({OPA_FOLDER / "bundle" / path: text for path, text in bundle.items()})
    return files


def _agentcore_files(plan: DeploymentPlan, renderer: TemplateRenderer) -> dict[PurePosixPath, str]:
    target = plan.target
    settings = plan.agentcore
    if settings is None:
        raise CliError(f"{target.name} is planned for {target.platform} without AgentCore settings")
    context = _context(target) | {"artifact": settings.artifact, "jwt": settings.jwt}
    # The image and the zip are two ways to ship the same code; only the chosen one is written.
    unused = _ZIP_FILES if settings.artifact == "container" else _IMAGE_FILES
    folder = target.deployment
    files = {
        folder / path: text
        for path, text in renderer.render("deploy-agentcore", context).items()
        if path.name not in unused
    }
    header = _HEADER.format(name=target.name, target=" --target agentcore", source=target.settings)
    files[folder / "settings.tf"] = agentcore_settings_tf(plan, header)
    files[folder / "permissions.tf"] = permissions_tf(plan, header)
    return files


def deployment_files(
    plan: DeploymentPlan,
    bundle: Mapping[PurePosixPath, str],
    renderer: TemplateRenderer | None = None,
) -> dict[PurePosixPath, str]:
    """Return every file a deployment of ``plan`` needs, by path from the workspace root.

    Args:
        plan: The plan, with no problems left.
        bundle: The platform's Rego bundle, by path inside it. Copied only when
            a Fargate task needs the OPA sidecar.
        renderer: Renders the templates.
    """
    chosen = renderer or TemplateRenderer()
    if plan.agentcore is not None:
        return _agentcore_files(plan, chosen)
    return _fargate_files(plan, bundle, chosen)


def read_bundle(answers: WorkspaceAnswers, given: Path | None) -> dict[PurePosixPath, str]:
    """Return the platform's Rego bundle, without its tests, by path inside it.

    Args:
        answers: The workspace's answers; a library checkout they name holds the bundle.
        given: A bundle folder named on the command line, which comes first.

    Raises:
        CliError: If neither names a folder that exists.
    """
    checkout = Path(answers.library.path) if answers.library.path else None
    folder = given or (checkout / "policies" / "bundle" if checkout else None)
    if folder is None or not folder.is_dir():
        raise CliError(
            "the platform's Rego bundle was not found",
            expected="the bundle the OPA sidecar decides with",
            actual=f"no folder at {folder}" if folder else "a workspace with no library checkout",
            fix="give its folder with --bundle",
        )
    return {
        PurePosixPath(path.relative_to(folder).as_posix()): path.read_text(encoding="utf-8")
        for path in sorted(folder.rglob("*"))
        if path.is_file() and not path.name.endswith(_REGO_TEST_SUFFIX)
    }


def write_deployment(
    root: Path,
    plan: DeploymentPlan,
    bundle: Mapping[PurePosixPath, str],
    renderer: TemplateRenderer | None = None,
) -> DeploymentReport:
    """Write the deployment of ``plan`` under ``root``.

    The reference module, the OPA sidecar and the two files derived from the
    settings are the tool's: they are rewritten. A file of the service's own
    root module that already exists is the developer's and is kept, which is
    why a service that changes platform starts from an empty folder.

    Args:
        root: The workspace.
        plan: The plan, with no problems left.
        bundle: The platform's Rego bundle, by path inside it; needed only when
            a Fargate task has the OPA sidecar.
        renderer: Renders the templates.
    """
    files = deployment_files(plan, bundle, renderer)
    ours = maintained_files(plan.target) | {
        path
        for path in files
        if path.is_relative_to(MODULE_FOLDER) or path.is_relative_to(OPA_FOLDER)
    }
    kept = tuple(
        sorted(path for path in files if path not in ours and root.joinpath(*path.parts).is_file())
    )
    written = write_files(
        root, {path: text for path, text in files.items() if path not in kept}, replace=ours
    )
    return DeploymentReport(written, kept)
