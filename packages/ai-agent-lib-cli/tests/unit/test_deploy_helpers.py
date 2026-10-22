"""The pieces of 'agentlib deploy', each on its own: dependencies, Terraform, files, starter."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from ai_agent_lib_cli.deploy import (
    MODULE_FOLDER,
    OPA_FOLDER,
    DeploymentPlan,
    DistributionResolver,
    ModuleOrigin,
    Target,
    deployment_files,
    installed_extras,
    maintained_files,
    next_steps,
    read_bundle,
    starter_settings,
    write_deployment,
)
from ai_agent_lib_cli.deploy.hcl import permissions_tf, settings_tf
from ai_agent_lib_cli.deploy.plan import missing_dependencies
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.workspace import LibrarySource, WorkspaceAnswers
from ai_agent_lib_core.contracts import Access
from ai_agent_lib_core.contracts.access import ACCOUNT, REGION
from ai_agent_lib_core.di import AccessPlan, AdapterAccess, ServiceProviders

AGENT = Target("helper", "agent", PurePosixPath("agents", "helper"), 8000)
SERVER = Target("people", "mcp", PurePosixPath("mcp-servers", "people"), 8100)
HEADER = "# header\n"

FIREHOSE = Access(
    actions=("firehose:PutRecord", "firehose:DescribeDeliveryStream"),
    resources=(f"arn:aws:firehose:{REGION}:{ACCOUNT}:deliverystream/eap-audit",),
    why="write audit records",
)
BEDROCK = Access(
    actions=("bedrock:InvokeModel",),
    resources=(f"arn:aws:bedrock:{REGION}::foundation-model/*",),
    why="call the model",
    note="replace * with the model ID",
)


def _plan(target: Target = AGENT, *adapters: AdapterAccess, **fields: Any) -> DeploymentPlan:
    environment = fields.pop("environment", (("EAP_PROFILE", "aws"), ("EAP_AUDIT", '{"a": 1}')))
    return DeploymentPlan(target, tuple(environment), AccessPlan(tuple(adapters)), **fields)


def _service(root: Path, target: Target, dependencies: str) -> None:
    folder = root.joinpath(*target.folder.parts)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pyproject.toml").write_text(
        f'[project]\nname = "{target.name}"\ndependencies = [{dependencies}]\n', encoding="utf-8"
    )


# ------------------------------------------------------------- the dependencies


def test_installed_extras_reads_each_requirement_with_its_extras(tmp_path: Path) -> None:
    _service(
        tmp_path,
        AGENT,
        '"ai_agent_lib_core[jwt, otel]>=0.1", "ai-agent-lib-aws", "Ai.Agent.Lib.AWS[bedrock]",'
        ' "requests ; python_version < \'4\'", "",',
    )
    assert installed_extras(tmp_path, AGENT) == {
        "ai-agent-lib-core": {"jwt", "otel"},
        "ai-agent-lib-aws": {"bedrock"},  # the two spellings are one distribution
        "requests": set(),
    }


def test_installed_extras_needs_a_readable_pyproject(tmp_path: Path) -> None:
    with pytest.raises(CliError, match=r"agents/helper/pyproject\.toml could not be read"):
        installed_extras(tmp_path, AGENT)


def _factory(module: str) -> Any:
    def build(context: Any) -> Any:
        return None

    build.__module__ = module
    return build


def _registry() -> ServiceProviders:
    return (
        ServiceProviders()
        .register("audit", "firehose", _factory("pack_adapters.audit"), extra="firehose")
        .register("model", "bedrock", _factory("pack_adapters.model"), extra="bedrock")
        .register("identity", "jwt", _factory("ai_agent_lib_core.adapters.identity_jwt"))
        .register("policy", "own", _factory("helper.policy"))
    )


def _resolver(root: Path) -> DistributionResolver:
    """A process with a pack, core and the service itself (from its folder) imported."""
    origins = {
        "pack_adapters": ModuleOrigin(distributions=frozenset({"Adapter.Pack"})),
        "ai_agent_lib_core": ModuleOrigin(distributions=frozenset({"ai-agent-lib-core"})),
        "helper": ModuleOrigin(location=root / "agents" / "helper" / "src" / "helper" / "x.py"),
    }
    return lambda top: origins.get(top, ModuleOrigin())


def _selected() -> AccessPlan:
    return AccessPlan(
        (
            AdapterAccess("audit", "firehose", access=(FIREHOSE,)),
            AdapterAccess("model", "bedrock"),
            AdapterAccess("identity", "jwt"),
            AdapterAccess("policy", "own"),
        )
    )


def test_missing_dependencies_names_the_distribution_the_adapters_need(tmp_path: Path) -> None:
    _service(tmp_path, AGENT, '"ai-agent-lib-core[otel]"')
    (problem,) = missing_dependencies(
        tmp_path, AGENT, _selected(), _registry(), _resolver(tmp_path)
    )
    assert problem.startswith("audit firehose, model bedrock need adapter-pack, which helper")
    assert 'list "adapter-pack[bedrock,firehose]" in the dependencies' in problem
    assert "agents/helper/pyproject.toml" in problem


def test_missing_dependencies_names_only_the_extras_that_are_missing(tmp_path: Path) -> None:
    _service(tmp_path, AGENT, '"ai-agent-lib-core", "adapter-pack[firehose]"')
    (problem,) = missing_dependencies(
        tmp_path, AGENT, _selected(), _registry(), _resolver(tmp_path)
    )
    assert "need the extras bedrock of adapter-pack" in problem


def test_missing_dependencies_is_empty_when_the_image_has_everything(tmp_path: Path) -> None:
    # Core needs no listing, and the service's own adapter is in its own image.
    _service(tmp_path, AGENT, '"adapter-pack[bedrock,firehose]"')
    assert (
        missing_dependencies(tmp_path, AGENT, _selected(), _registry(), _resolver(tmp_path)) == []
    )


def test_missing_dependencies_without_an_installed_distribution_falls_back_to_the_import_name(
    tmp_path: Path,
) -> None:
    _service(tmp_path, AGENT, "")
    registry = ServiceProviders().register("audit", "x", _factory("Some_Pack.audit"))
    selected = AccessPlan((AdapterAccess("audit", "x"),))
    (problem,) = missing_dependencies(tmp_path, AGENT, selected, registry, _resolver(tmp_path))
    assert "audit x need some-pack, which helper does not install" in problem


# ----------------------------------------------------------------- the terraform


def test_permissions_tf_is_one_statement_per_access_with_its_reason() -> None:
    plan = _plan(
        AGENT,
        AdapterAccess("audit", "firehose", access=(FIREHOSE,)),
        AdapterAccess("model", "bedrock", access=(BEDROCK,)),
        AdapterAccess("identity", "jwt"),
    )
    text = permissions_tf(plan, HEADER)
    assert text.startswith(HEADER + "# Each statement is what one adapter said it needs")
    assert 'data "aws_caller_identity" "current" {}' in text
    assert (
        "  # audit firehose: write audit records\n"
        "  statement {\n"
        '    sid       = "AuditFirehose1"\n'
        '    actions   = ["firehose:PutRecord", "firehose:DescribeDeliveryStream"]\n'
        '    resources = ["arn:aws:firehose:${var.region}:'
        '${data.aws_caller_identity.current.account_id}:deliverystream/eap-audit"]\n'
        "  }\n"
    ) in text
    assert "  # check: replace * with the model ID\n" in text
    assert 'sid       = "ModelBedrock1"' in text
    assert text.endswith(
        "locals {\n  task_policy_json = data.aws_iam_policy_document.task.json\n}\n"
    )
    assert "identity jwt" not in text


def test_permissions_tf_without_an_account_placeholder_asks_for_no_identity() -> None:
    plan = _plan(AGENT, AdapterAccess("model", "bedrock", access=(BEDROCK,)))
    text = permissions_tf(plan, HEADER)
    assert "aws_caller_identity" not in text
    assert "${var.region}" in text


def test_permissions_tf_with_nothing_to_grant_sets_no_policy() -> None:
    text = permissions_tf(_plan(AGENT, AdapterAccess("identity", "jwt")), HEADER)
    assert (
        text
        == HEADER
        + "\n# The adapters need nothing from AWS.\nlocals {\n  task_policy_json = null\n}\n"
    )


def test_settings_tf_holds_the_command_the_sidecars_and_the_environment() -> None:
    plan = _plan(AGENT, opa=True, collector=False)
    assert settings_tf(plan, HEADER) == (
        "# header\n"
        "\n"
        "locals {\n"
        '  command   = ["helper-serve", "--host", "0.0.0.0", "--port", "8000"]\n'
        "  port      = 8000\n"
        "  uses_opa  = true\n"
        "  collector = false\n"
        "\n"
        "  environment = {\n"
        '    EAP_PROFILE = "aws"\n'
        '    EAP_AUDIT   = "{\\"a\\": 1}"\n'
        "  }\n"
        "}\n"
    )


def test_settings_tf_of_a_server_without_settings_has_an_empty_environment() -> None:
    text = settings_tf(_plan(SERVER, environment=(), collector=True), HEADER)
    assert '  command   = ["people", "--host", "0.0.0.0", "--port", "8100"]\n' in text
    assert "  collector = true\n" in text
    assert "  environment = {}\n}\n" in text


# --------------------------------------------------------------------- the files


def test_deployment_files_are_the_module_the_service_and_two_derived_files() -> None:
    files = deployment_files(_plan(AGENT), {})
    assert {path.as_posix() for path in files if not path.is_relative_to(MODULE_FOLDER)} == {
        "deploy/helper/Dockerfile",
        "deploy/helper/Dockerfile.dockerignore",
        "deploy/helper/README.md",
        "deploy/helper/main.tf",
        "deploy/helper/permissions.tf",
        "deploy/helper/settings.tf",
        "deploy/helper/terraform.tfvars.example",
        "deploy/helper/variables.tf",
    }
    assert MODULE_FOLDER / "main.tf" in files
    assert not any(path.is_relative_to(OPA_FOLDER) for path in files)
    header = "# Written by 'agentlib deploy helper' from agents/helper/deploy.env.\n"
    assert files[PurePosixPath("deploy/helper/settings.tf")].startswith(header)
    assert files[PurePosixPath("deploy/helper/permissions.tf")].startswith(header)
    assert (
        'CMD ["helper-serve", "--host", "0.0.0.0", "--port", "8000"]'
        in (files[PurePosixPath("deploy/helper/Dockerfile")])
    )


def test_deployment_files_copy_the_bundle_only_when_the_task_has_the_sidecar() -> None:
    bundle = {PurePosixPath("agentlib/authz/authz.rego"): "package agentlib.authz\n"}
    with_opa = deployment_files(_plan(AGENT, opa=True), bundle)
    assert with_opa[OPA_FOLDER / "bundle/agentlib/authz/authz.rego"] == "package agentlib.authz\n"
    assert OPA_FOLDER / "Dockerfile" in with_opa
    assert OPA_FOLDER / "Dockerfile" not in deployment_files(_plan(AGENT, opa=False), bundle)


def test_maintained_files_are_the_two_derived_from_the_settings() -> None:
    assert maintained_files(AGENT) == {
        PurePosixPath("deploy/helper/settings.tf"),
        PurePosixPath("deploy/helper/permissions.tf"),
    }


def test_write_deployment_keeps_the_developers_files_and_rewrites_its_own(tmp_path: Path) -> None:
    first = write_deployment(tmp_path, _plan(AGENT, opa=True), {PurePosixPath("a.rego"): "a\n"})
    assert first.kept == ()
    assert first.written.updated == ()
    assert tmp_path.joinpath("deploy/helper/main.tf") in first.written.created
    assert tmp_path.joinpath("deploy/opa/bundle/a.rego") in first.written.created

    main_tf = tmp_path / "deploy/helper/main.tf"
    main_tf.write_text("# mine\n", encoding="utf-8")
    module_tf = tmp_path.joinpath(*MODULE_FOLDER.parts, "main.tf")
    module_tf.write_text("# not mine to keep\n", encoding="utf-8")
    second = write_deployment(tmp_path, _plan(AGENT, opa=True, collector=True), {})

    # Every file of the service's own root module is the developer's once written.
    own: tuple[str, ...] = ("Dockerfile", "Dockerfile.dockerignore", "README.md", "main.tf")
    own += ("terraform.tfvars.example", "variables.tf")
    assert second.kept == tuple(PurePosixPath("deploy/helper", name) for name in own)
    assert main_tf.read_text(encoding="utf-8") == "# mine\n"
    assert module_tf in second.written.updated
    assert tmp_path.joinpath("deploy/helper/settings.tf") in second.written.updated
    assert tmp_path.joinpath("deploy/helper/permissions.tf") in second.written.unchanged
    assert tmp_path.joinpath("deploy/opa/Dockerfile") in second.written.unchanged


def test_read_bundle_takes_the_given_folder_and_leaves_the_tests_out(tmp_path: Path) -> None:
    folder = tmp_path / "bundle"
    (folder / "agentlib").mkdir(parents=True)
    (folder / "agentlib" / "authz.rego").write_text("package agentlib\n", encoding="utf-8")
    (folder / "agentlib" / "authz_test.rego").write_text("package agentlib_test\n", "utf-8")
    (folder / "data.json").write_text("{}\n", encoding="utf-8")
    answers = WorkspaceAnswers("demo", "team", LibrarySource(kind="index", version=">=0.1"))

    assert read_bundle(answers, folder) == {
        PurePosixPath("agentlib/authz.rego"): "package agentlib\n",
        PurePosixPath("data.json"): "{}\n",
    }


def test_read_bundle_defaults_to_the_library_checkout_and_says_when_there_is_none(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "lib"
    (checkout / "policies" / "bundle").mkdir(parents=True)
    (checkout / "policies" / "bundle" / "a.rego").write_text("a\n", encoding="utf-8")
    from_checkout = WorkspaceAnswers("demo", "team", LibrarySource.from_path(checkout))
    assert read_bundle(from_checkout, None) == {PurePosixPath("a.rego"): "a\n"}

    from_index = WorkspaceAnswers("demo", "team", LibrarySource(kind="index", version=">=0.1"))
    with pytest.raises(CliError) as caught:
        read_bundle(from_index, None)
    assert caught.value.actual == "a workspace with no library checkout"
    assert caught.value.fix == "give its folder with --bundle"
    with pytest.raises(CliError) as caught:
        read_bundle(from_index, tmp_path / "missing")
    assert str(tmp_path / "missing") in (caught.value.actual or "")


# ------------------------------------------------------------------- the starter


def test_starter_settings_of_an_agent_select_bedrock_and_postgres() -> None:
    text = starter_settings(AGENT)
    assert "How helper runs in AWS." in text
    assert "never put a secret here" in text
    assert "EAP_PROFILE=aws\n" in text
    assert "EAP_DEPLOYMENT_ENV=dev\n" in text
    assert "EAP_TELEMETRY=opentelemetry\n" in text
    assert "EAP_MODEL_ID=eu.anthropic.claude-sonnet-4-5-20250929-v1:0\n" in text
    assert '"user": "helper"' in text  # the checkpoint database user, a valid identifier
    assert '"prefix": "eap/helper/"' in text
    assert '"audience": "api://helper"' in text
    assert "EAP_CHECKPOINT_PROVIDER" not in text
    assert "EAP_SECRET_" not in text  # example values, never a secret


def test_starter_settings_of_an_mcp_server_keep_no_conversations_and_name_no_model() -> None:
    text = starter_settings(SERVER)
    assert "EAP_CHECKPOINT_PROVIDER=none\n" in text
    assert "EAP_MODEL_ID" not in text
    assert "EAP_CHECKPOINT_OPTIONS" not in text
    assert "'agentlib deploy people --plan'" in text


def test_next_steps_ask_for_the_aws_adapters_only_when_the_service_lacks_them(
    tmp_path: Path,
) -> None:
    _service(tmp_path, AGENT, '"ai-agent-lib-core"')
    steps = next_steps(tmp_path, AGENT)
    assert steps[0].startswith('add "ai-agent-lib-aws[bedrock,postgres]" to the dependencies')
    assert steps[1:] == [
        "fill in agents/helper/deploy.env: its values are examples",
        "run 'agentlib deploy helper --plan'",
    ]

    _service(tmp_path, SERVER, '"ai-agent-lib-core"')
    assert next_steps(tmp_path, SERVER)[0].startswith('add "ai-agent-lib-aws" to')

    _service(tmp_path, AGENT, '"ai-agent-lib-aws[bedrock]"')
    assert len(next_steps(tmp_path, AGENT)) == 2
