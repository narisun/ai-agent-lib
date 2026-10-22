"""agentlib deploy: derive a service's deployment from the settings it runs with.

The settings a service runs with in AWS live in ``deploy.env`` in its folder.
From them :mod:`.plan` works out which adapters run, what each needs from IAM
(each adapter says so itself), which sidecars the task needs, and anything a
person must check. :mod:`.hcl` turns the plan into Terraform, :mod:`.files`
writes it beside a reference module that runs the service on ECS Fargate, and
:mod:`.starter` writes the first ``deploy.env``.

Nothing here calls AWS, and no secret is read or written: ``deploy.env`` may
not hold one.
"""

from __future__ import annotations

from ai_agent_lib_cli.deploy.files import (
    DeploymentReport,
    deployment_files,
    maintained_files,
    read_bundle,
    write_deployment,
)
from ai_agent_lib_cli.deploy.hcl import hcl_string
from ai_agent_lib_cli.deploy.plan import (
    DeploymentPlan,
    DistributionResolver,
    ModuleOrigin,
    depends_on_aws,
    installed_extras,
    installed_origin,
    plan_deployment,
    plan_lines,
)
from ai_agent_lib_cli.deploy.starter import next_steps, starter_settings, write_starter
from ai_agent_lib_cli.deploy.target import (
    AWS_DISTRIBUTION,
    DEPLOY_ENV,
    MODULE_FOLDER,
    OPA_FOLDER,
    Target,
    target_of,
)

__all__ = [
    "AWS_DISTRIBUTION",
    "DEPLOY_ENV",
    "MODULE_FOLDER",
    "OPA_FOLDER",
    "DeploymentPlan",
    "DeploymentReport",
    "DistributionResolver",
    "ModuleOrigin",
    "Target",
    "depends_on_aws",
    "deployment_files",
    "hcl_string",
    "installed_extras",
    "installed_origin",
    "maintained_files",
    "next_steps",
    "plan_deployment",
    "plan_lines",
    "read_bundle",
    "starter_settings",
    "target_of",
    "write_deployment",
    "write_starter",
]
