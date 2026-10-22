"""The files a deployment is made of, and writing them without losing the developer's work.

A deployment is a Terraform root module for the service beside a reference
module that runs it on ECS Fargate, and, when the task needs the OPA sidecar,
the sidecar's Dockerfile with the platform's Rego bundle. Two files are
derived from the settings and rewritten on every run; everything else is
written once and is the developer's from then on.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ai_agent_lib_cli.deploy.hcl import permissions_tf, settings_tf
from ai_agent_lib_cli.deploy.plan import DeploymentPlan
from ai_agent_lib_cli.deploy.target import MODULE_FOLDER, OPA_FOLDER, Target
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.render import Report, TemplateRenderer, write_files
from ai_agent_lib_cli.workspace import WorkspaceAnswers

__all__ = [
    "DeploymentReport",
    "deployment_files",
    "maintained_files",
    "read_bundle",
    "write_deployment",
]

_HEADER = (
    "# Written by 'agentlib deploy {name}' from {source}.\n"
    "# Do not edit: change that file and run the command again.\n"
)
_REGO_TEST_SUFFIX = "_test.rego"


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
    """The files ``agentlib deploy`` rewrites every time; the rest are written once."""
    folder = target.deployment
    return frozenset({folder / "settings.tf", folder / "permissions.tf"})


def deployment_files(
    plan: DeploymentPlan,
    bundle: Mapping[PurePosixPath, str],
    renderer: TemplateRenderer | None = None,
) -> dict[PurePosixPath, str]:
    """Return every file a deployment of ``plan`` needs, by path from the workspace root.

    Args:
        plan: The plan, with no problems left.
        bundle: The platform's Rego bundle, by path inside it. Copied only when
            the task needs the OPA sidecar.
        renderer: Renders the templates.
    """
    render = (renderer or TemplateRenderer()).render
    target = plan.target
    context: dict[str, object] = {
        "name": target.name,
        "folder": target.folder.as_posix(),
        "port": target.port,
        "command_json": json.dumps(list(target.command)),
    }
    files = {MODULE_FOLDER / path: text for path, text in render("deploy-module", {}).items()}
    folder = target.deployment
    files.update({folder / path: text for path, text in render("deploy-service", context).items()})
    header = _HEADER.format(name=target.name, source=target.settings)
    files[folder / "settings.tf"] = settings_tf(plan, header)
    files[folder / "permissions.tf"] = permissions_tf(plan, header)
    if plan.opa:
        files.update({OPA_FOLDER / path: text for path, text in render("deploy-opa", {}).items()})
        files.update({OPA_FOLDER / "bundle" / path: text for path, text in bundle.items()})
    return files


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
    root module that already exists is the developer's and is kept.

    Args:
        root: The workspace.
        plan: The plan, with no problems left.
        bundle: The platform's Rego bundle, by path inside it; needed only when
            the task has the OPA sidecar.
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
