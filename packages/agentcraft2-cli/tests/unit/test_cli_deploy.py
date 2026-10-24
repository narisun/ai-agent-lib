"""'agentcraft2 deploy': a service's deployment, derived from the settings it runs with."""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import uuid
from collections.abc import Mapping
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from agentcraft2_cli.__main__ import main
from agentcraft2_cli.deploy import (
    DistributionResolver,
    ModuleOrigin,
    hcl_string,
    installed_origin,
    plan_deployment,
    target_of,
)
from agentcraft2_cli.testing import toolbox_for_tests
from agentcraft2_cli.workspace import load_answers
from agentcraft2_core.di import ServiceProviders

TOOLS = toolbox_for_tests()


def run(*arguments: str | Path) -> int:
    return main([str(argument) for argument in arguments], toolbox=TOOLS)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    assert run("init", "demo", "--dir", tmp_path, "--owner", "o") == 0
    root = tmp_path / "demo"
    assert run("new", "agent", "helper", "--workspace", root) == 0
    assert run("new", "mcp", "people", "--no-pin", "--workspace", root) == 0
    return root


def _depend_on_aws(root: Path, folder: str, requirement: str = "agentcraft2-aws") -> None:
    pyproject = root / folder / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    pyproject.write_text(
        text.replace("dependencies = [\n", f'dependencies = [\n    "{requirement}",\n', 1),
        encoding="utf-8",
    )


def _settings(root: Path, folder: str = "agents/helper") -> Path:
    return root / folder / "deploy.env"


@pytest.fixture
def ready(workspace: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A workspace whose agent has a starting deploy.env and depends on the AWS adapters."""
    assert run("deploy", "helper", "--workspace", workspace) == 0
    _depend_on_aws(workspace, "agents/helper", "agentcraft2-aws[bedrock,postgres]")
    capsys.readouterr()
    return workspace


def test_the_first_run_writes_a_starting_settings_file_and_nothing_else(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("deploy", "helper", "--plan", "--workspace", workspace) == 1
    err = capsys.readouterr().err
    assert "agents/helper/deploy.env does not exist" in err
    assert "fix: run 'agentcraft2 deploy helper' to write a starting one" in err

    assert run("deploy", "helper", "--workspace", workspace) == 0
    out = capsys.readouterr().out
    assert "created  agents/helper/deploy.env" in out
    assert 'add "agentcraft2-aws[bedrock,postgres]" to the dependencies' in out
    text = _settings(workspace).read_text(encoding="utf-8")
    assert "AC2_PROFILE=aws" in text
    assert "never put a secret here" in text
    assert not (workspace / "deploy").exists()


def test_the_plan_names_every_permission_with_its_reason_and_writes_nothing(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("deploy", "helper", "--plan", "--workspace", ready) == 0
    out = capsys.readouterr().out
    assert (
        "  audit firehose:\n    firehose:PutRecord, firehose:DescribeDeliveryStream  "
        "(write audit records, and check at startup that the stream is active)\n"
    ) in out
    assert "      on arn:aws:firehose:{region}:{account}:deliverystream/eap-audit\n" in out
    assert "  identity jwt: nothing\n" in out
    assert "check: replace * with the resource ID" in out
    assert "  opa: decides policy on the task's loopback address" in out
    assert "  collector: sends traces" in out
    assert "  service: helper-serve --host 0.0.0.0 --port 8000" in out
    assert not (ready / "deploy").exists()


def test_adapters_from_a_distribution_the_service_does_not_depend_on_stop_it(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("deploy", "helper", "--workspace", workspace) == 0
    capsys.readouterr()

    assert run("deploy", "helper", "--workspace", workspace) == 1
    captured = capsys.readouterr()
    assert "need agentcraft2-aws, which helper does not install" in captured.out
    assert '"agentcraft2-aws[bedrock,postgres]" in the dependencies' in captured.out
    assert "1 problem stop the deployment" in captured.err
    assert not (workspace / "deploy").exists()


def test_deploy_writes_the_module_and_the_files_derived_from_the_settings(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("deploy", "helper", "--workspace", ready) == 0
    out = capsys.readouterr().out
    assert "created  deploy/helper/permissions.tf" in out
    assert "created  deploy/modules/agentcraft2-service/main.tf" in out
    assert "created  deploy/opa/bundle/agentcraft2/authz/authz.rego" in out
    assert not (ready / "deploy/opa/bundle/agentcraft2/authz/authz_test.rego").exists()

    permissions = (ready / "deploy/helper/permissions.tf").read_text(encoding="utf-8")
    assert "  # audit firehose: write audit records, and check at startup" in permissions
    assert 'sid       = "AuditFirehose1"' in permissions
    assert (
        '"arn:aws:firehose:${var.region}:${data.aws_caller_identity.current.account_id}'
        ':deliverystream/eap-audit"'
    ) in permissions
    settings = (ready / "deploy/helper/settings.tf").read_text(encoding="utf-8")
    assert "  uses_opa  = true\n" in settings
    assert '    AC2_AUDIT_OPTIONS      = "{\\"stream\\": \\"eap-audit\\"}"\n' in settings
    dockerfile = (ready / "deploy/helper/Dockerfile").read_text(encoding="utf-8")
    assert "python -m pip install --no-cache-dir --require-hashes" in dockerfile
    assert "COPY agents/helper agents/helper" in dockerfile
    assert "COPY agents agents" not in dockerfile
    assert 'CMD ["helper-serve", "--host", "0.0.0.0", "--port", "8000"]' in dockerfile
    rules = (ready / "deploy/helper/Dockerfile.dockerignore").read_text()
    assert rules.splitlines()[1] == "**"
    assert "!agents/helper/src/**" in rules
    assert "!agents/**" not in rules
    assert rules.index("**/.env") > rules.index("!agents/helper/src/**")
    assert "**/*.sqlite*" in rules
    assert "**/*.jsonl" in rules


@pytest.mark.container
def test_no_secret_sentinel_reaches_any_image_layer(ready: Path) -> None:
    assert run("deploy", "helper", "--workspace", ready) == 0
    sentinel = "MUST-NOT-ENTER-IMAGE-" + uuid.uuid4().hex
    excluded = (
        ".env",
        "agents/helper/.env",
        "agents/helper/src/helper/.env",
        "agents/helper/src/helper/cache.sqlite",
        "agents/helper/src/helper/audit.jsonl",
        "mcp-servers/people/.env",
        "agents/another/src/another/secret.txt",
    )
    for relative in excluded:
        path = ready / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(sentinel)
    # A minimal installable service isolates the context test from package indexes.
    (ready / "agents/helper/src/helper/__init__.py").write_text('RUNTIME_MARKER = "included"\n')
    (ready / "agents/helper/pyproject.toml").write_text(
        '[project]\nname="helper"\nversion="0.1.0"\n'
        '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
    )
    (ready / "deploy/helper/requirements.lock").write_text("")
    # Inspect the actual filtered build context as well as the saved image.
    # An excluded sibling can otherwise be uploaded without a COPY using it.
    dockerfile = ready / "deploy/helper/Dockerfile"
    probe = (
        "from pathlib import Path; root = Path('/context'); "
        f"assert not any((root / name).exists() for name in {excluded!r})"
    )
    dockerfile.write_text(
        dockerfile.read_text().replace(
            "WORKDIR /app\n",
            f'WORKDIR /app\nRUN --mount=type=bind,target=/context python -c "{probe}"\n',
            1,
        )
    )
    docker = shutil.which("docker")
    assert docker is not None, "run this opt-in gate on a host with Docker"
    tag = "agentcraft2-context-test:" + uuid.uuid4().hex

    def command(*arguments: str) -> None:
        subprocess.run([docker, *arguments], cwd=ready, check=True, capture_output=True)  # noqa: S603

    try:
        command("build", "-f", "deploy/helper/Dockerfile", "-t", tag, ".")
        command(
            "run",
            "--rm",
            tag,
            "python",
            "-c",
            "import helper; assert helper.RUNTIME_MARKER == 'included'",
        )
        image = ready / "image.tar"
        command("save", "-o", str(image), tag)
        with tarfile.open(image) as archive:
            layers = [member for member in archive if member.isfile()]
            assert layers
            for member in layers:
                content = archive.extractfile(member)
                assert content is not None
                data = content.read()
                assert sentinel.encode() not in data
                # Docker archives can contain compressed OCI layers; inspect
                # every archived layer, including files deleted by later layers.
                try:
                    with tarfile.open(fileobj=BytesIO(data), mode="r:*") as layer:
                        for entry in layer:
                            if entry.isfile():
                                payload = layer.extractfile(entry)
                                assert payload is not None
                                assert sentinel.encode() not in payload.read()
                except tarfile.ReadError:
                    continue  # image metadata, not a layer
    finally:
        subprocess.run([docker, "image", "rm", "--force", tag], check=False, capture_output=True)  # noqa: S603


def test_running_again_rewrites_what_is_derived_and_keeps_what_is_yours(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("deploy", "helper", "--workspace", ready) == 0
    main_tf = ready / "deploy/helper/main.tf"
    main_tf.write_text(main_tf.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    settings = _settings(ready)
    settings.write_text(
        settings.read_text(encoding="utf-8").replace("eap-audit", "other-audit"), encoding="utf-8"
    )
    capsys.readouterr()

    assert run("deploy", "helper", "--workspace", ready) == 0
    out = capsys.readouterr().out
    assert "kept     deploy/helper/main.tf (yours)" in out
    assert "updated  deploy/helper/permissions.tf" in out
    assert main_tf.read_text(encoding="utf-8").endswith("# mine\n")
    assert "other-audit" in (ready / "deploy/helper/permissions.tf").read_text(encoding="utf-8")


def test_legacy_spellings_in_the_settings_are_flagged_in_the_plan_and_passed_through_verbatim(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings(ready)
    text = settings.read_text(encoding="utf-8")
    settings.write_text(text.replace("AC2_AUDIT_OPTIONS", "EAP_AUDIT_OPTIONS"), encoding="utf-8")

    assert run("deploy", "helper", "--plan", "--workspace", ready) == 0
    out = capsys.readouterr().out
    assert "Deprecated spellings in agents/helper/deploy.env" in out
    assert "  EAP_AUDIT_OPTIONS -> AC2_AUDIT_OPTIONS" in out
    assert "stops working after one minor release" in out
    assert "  EAP_AUDIT_OPTIONS\n" in out  # the environment lists the name as written
    assert "AC2_PROFILE\n" in out
    assert "  AC2_PROFILE ->" not in out

    assert run("deploy", "helper", "--workspace", ready) == 0
    capsys.readouterr()
    tasks = (ready / "deploy/helper/settings.tf").read_text(encoding="utf-8")
    assert '    EAP_AUDIT_OPTIONS      = "{\\"stream\\": \\"eap-audit\\"}"\n' in tasks
    assert "AC2_AUDIT_OPTIONS" not in tasks


def test_canonical_settings_have_no_deprecated_spellings_section(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("deploy", "helper", "--plan", "--workspace", ready) == 0
    assert "Deprecated spellings" not in capsys.readouterr().out


def test_a_secret_in_the_settings_is_refused_and_its_value_never_shown(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _settings(ready).open("a", encoding="utf-8") as file:
        file.write("AC2_SECRET_RATES_TOKEN=s3cr3t-value\n")

    assert run("deploy", "helper", "--plan", "--workspace", ready) == 1
    captured = capsys.readouterr()
    assert "holds secrets" in captured.err
    assert "AC2_SECRET_RATES_TOKEN" in captured.err
    assert "s3cr3t-value" not in captured.out + captured.err


def test_settings_for_a_developers_machine_are_refused(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings(ready)
    settings.write_text(
        settings.read_text(encoding="utf-8").replace(
            "AC2_DEPLOYMENT_ENV=dev", "AC2_DEPLOYMENT_ENV=local"
        ),
        encoding="utf-8",
    )

    assert run("deploy", "helper", "--plan", "--workspace", ready) == 1
    err = capsys.readouterr().err
    assert "expected: AC2_DEPLOYMENT_ENV=dev or AC2_DEPLOYMENT_ENV=prod" in err
    assert "got: AC2_DEPLOYMENT_ENV is local" in err


def test_a_local_only_adapter_is_a_problem_that_names_itself(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _settings(ready).open("a", encoding="utf-8") as file:
        file.write("AC2_AUDIT_PROVIDER=jsonl\nAC2_AUDIT_OPTIONS={}\n")

    assert run("deploy", "helper", "--plan", "--workspace", ready) == 1
    out = capsys.readouterr().out
    assert "audit jsonl runs only on a developer's machine" in out


def test_a_bad_option_says_which_adapter_and_which_file(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _settings(ready).open("a", encoding="utf-8") as file:
        file.write('AC2_AUDIT_OPTIONS={"stream": "has spaces"}\n')

    assert run("deploy", "helper", "--plan", "--workspace", ready) == 1
    err = capsys.readouterr().err
    assert "the options of provider 'firehose' are not valid" in err
    assert "while working out what the audit adapter (firehose) needs from the cloud" in err


def test_an_mcp_server_keeps_no_conversations_and_is_served_by_its_own_command(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("deploy", "people", "--workspace", workspace) == 0
    _depend_on_aws(workspace, "mcp-servers/people")
    settings = _settings(workspace, "mcp-servers/people")
    assert "AC2_CHECKPOINT_PROVIDER=none" in settings.read_text(encoding="utf-8")
    # An OPA server elsewhere needs no sidecar.
    with settings.open("a", encoding="utf-8") as file:
        file.write('AC2_POLICY_OPTIONS={"url": "https://opa.internal:8181"}\n')
    capsys.readouterr()

    assert run("deploy", "people", "--workspace", workspace) == 0
    out = capsys.readouterr().out
    assert "  service: people --host 0.0.0.0 --port" in out
    # Its sample data source is configured locally only, and the plan says so.
    assert "reads the data sources people locally" in out
    assert "  checkpoint none: nothing" in out
    assert "  opa:" not in out
    assert not (workspace / "deploy/opa").exists()


def test_a_terraform_string_is_never_interpolated() -> None:
    assert hcl_string('a "b" ${c} %{d} \\e\n') == '"a \\"b\\" $${c} %%{d} \\\\e\\n"'


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("terraform") is None, reason="needs terraform on PATH")
def test_the_written_terraform_is_formatted_valid_and_its_module_tests_pass(ready: Path) -> None:
    assert run("deploy", "helper", "--workspace", ready) == 0
    deploy = ready / "deploy"

    def terraform(*arguments: str, folder: Path = deploy) -> None:
        done = subprocess.run(  # noqa: S603 - a fixed command line, no shell
            ["terraform", *arguments],  # noqa: S607 - terraform from PATH, as a developer runs it
            cwd=folder,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, f"terraform {' '.join(arguments)}:\n{done.stdout}{done.stderr}"

    terraform("fmt", "-check", "-recursive")
    for folder in (deploy / "helper", deploy / "modules" / "agentcraft2-service"):
        terraform("init", "-backend=false", "-input=false", folder=folder)
        terraform("validate", folder=folder)
    terraform("test", folder=deploy / "modules" / "agentcraft2-service")


def test_r20_an_extra_an_adapter_needs_is_checked_too(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("deploy", "helper", "--workspace", workspace) == 0
    _depend_on_aws(workspace, "agents/helper")  # the distribution, without its extras
    capsys.readouterr()

    assert run("deploy", "helper", "--plan", "--workspace", workspace) == 1
    out = capsys.readouterr().out
    assert "need the extras bedrock, postgres of agentcraft2-aws" in out


def test_r21_telemetry_on_needs_the_opentelemetry_sdk_in_the_image(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = ready / "agents/helper/pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    pyproject.write_text(text.replace(",otel", ""), encoding="utf-8")

    assert run("deploy", "helper", "--plan", "--workspace", ready) == 1
    assert "does not install the OpenTelemetry SDK" in capsys.readouterr().out


# ------------- F6: an adapter is checked against the distribution that ships it


def _resolver(origins: Mapping[str, ModuleOrigin]) -> DistributionResolver:
    """A process that gets these import names from here, and the rest as the test process does."""
    return lambda top: origins.get(top, installed_origin(top))


def _plan_with(
    root: Path, factory: Any, *, extra: str | None = None, resolve: DistributionResolver
) -> Any:
    with _settings(root).open("a", encoding="utf-8") as file:
        file.write("AC2_AUDIT_PROVIDER=own_audit\nAC2_AUDIT_OPTIONS={}\n")
    registry = ServiceProviders.default().register("audit", "own_audit", factory, extra=extra)
    answers = load_answers(root)
    target = target_of(answers, "helper")
    return plan_deployment(root, answers, target, providers=registry, resolve=resolve)


def _factory_in(module_name: str) -> Any:
    def factory(context: Any) -> Any:
        return None

    factory.__module__ = module_name
    return factory


def test_f6_the_services_own_adapter_needs_no_dependency_on_itself(ready: Path) -> None:
    own = ModuleOrigin(location=ready / "agents/helper/src/helper/__init__.py")
    resolve = _resolver({"helper": own})

    plan = _plan_with(ready, _factory_in("helper.providers"), resolve=resolve)
    assert plan.problems == ()


def test_f6_a_pack_is_checked_by_its_distribution_not_its_import_name(ready: Path) -> None:
    pack = ModuleOrigin(distributions=frozenset({"review-provider-pack"}))
    resolve = _resolver({"review_adapters": pack})
    factory = _factory_in("review_adapters.audit")

    (problem,) = _plan_with(ready, factory, extra="fast", resolve=resolve).problems
    assert "need review-provider-pack, which helper does not install" in problem
    assert 'list "review-provider-pack[fast]"' in problem
    assert "review-adapters" not in problem

    _depend_on_aws(ready, "agents/helper", "review-provider-pack[fast]")
    assert _plan_with(ready, factory, extra="fast", resolve=resolve).problems == ()


def test_f6_without_a_resolver_the_running_process_answers() -> None:
    """The default looks at what this process has imported and installed."""
    origin = installed_origin("click")  # imported by the command line, installed as 'click'
    assert origin.distributions == frozenset({"click"})
    assert origin.location is not None
    assert origin.location.parent.name == "click"
    assert installed_origin("no_such_import_name") == ModuleOrigin()


@pytest.mark.parametrize(
    ("telemetry", "tracing", "on"),
    [
        ("off", False, False),
        ("off", True, True),  # the older audit option still turns telemetry on
        ("opentelemetry", False, True),
        ("opentelemetry", True, True),
    ],
)
def test_f7_the_collector_and_the_sdk_follow_the_same_switch_as_the_service(
    ready: Path, capsys: pytest.CaptureFixture[str], telemetry: str, tracing: bool, on: bool
) -> None:
    settings = _settings(ready)
    text = settings.read_text(encoding="utf-8")
    text = text.replace("AC2_TELEMETRY=opentelemetry", f"AC2_TELEMETRY={telemetry}")
    text = text.replace(
        'AC2_AUDIT_OPTIONS={"stream": "eap-audit"}',
        f'AC2_AUDIT_OPTIONS={{"stream": "eap-audit", "tracing": {str(tracing).lower()}}}',
    )
    settings.write_text(text, encoding="utf-8")
    pyproject = ready / "agents/helper/pyproject.toml"
    pyproject.write_text(pyproject.read_text(encoding="utf-8").replace(",otel", ""), "utf-8")

    code = run("deploy", "helper", "--plan", "--workspace", ready)
    out = capsys.readouterr().out
    assert ("collector:" in out) is on
    assert ("does not install the OpenTelemetry SDK" in out) is on
    assert code == (1 if on else 0)


def test_a_corrupt_service_pyproject_is_named_instead_of_misread(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (ready / "agents/helper/pyproject.toml").write_text("[project\nname = ", encoding="utf-8")
    assert run("deploy", "helper", "--plan", "--workspace", ready) == 1
    err = capsys.readouterr().err
    assert "agents/helper/pyproject.toml could not be read" in err
    assert "TOMLDecodeError" in err
    assert "does not install" not in err


# ------------------------------------------------ the AgentCore Runtime target


def _edit_settings(
    root: Path,
    replacements: Mapping[str, str] | None = None,
    append: str = "",
    folder: str = "agents/helper",
) -> None:
    """Change lines of a deploy.env: each replacement must find its text."""
    settings = _settings(root, folder)
    text = settings.read_text(encoding="utf-8")
    for old, new in (replacements or {}).items():
        assert old in text, old
        text = text.replace(old, new)
    settings.write_text(text + append, encoding="utf-8")


@pytest.fixture
def agentcore_ready(workspace: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A workspace whose agent has the AgentCore starting deploy.env and the adapters it selects."""
    assert run("deploy", "helper", "--target", "agentcore", "--workspace", workspace) == 0
    _depend_on_aws(workspace, "agents/helper", "agentcraft2-aws[bedrock,agentcore]")
    capsys.readouterr()
    return workspace


def _agentcore(root: Path, *arguments: str, service: str = "helper") -> int:
    return run("deploy", service, "--target", "agentcore", *arguments, "--workspace", root)


def test_the_agentcore_starter_selects_ambient_telemetry_a_memory_and_the_deployment_block(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _agentcore(workspace) == 0
    out = capsys.readouterr().out
    assert "created  agents/helper/deploy.env" in out
    assert 'add "agentcraft2-aws[bedrock,agentcore]" to the dependencies' in out
    assert "run 'agentcraft2 deploy helper --target agentcore --plan'" in out
    text = _settings(workspace).read_text(encoding="utf-8")
    assert "AC2_TELEMETRY=ambient\n" in text
    assert "AC2_CHECKPOINT_PROVIDER=agentcore_memory\n" in text
    assert 'AC2_CHECKPOINT_OPTIONS={"memory_id": "created_by_terraform"}\n' in text
    assert "AC2_AGENTCORE_AUTH=iam\n" in text
    assert "# AC2_AGENTCORE_DISCOVERY_URL=" in text
    assert "AC2_AGENTCORE_NETWORK=PUBLIC\n" in text
    assert "AC2_AGENTCORE_ARTIFACT=container\n" in text
    assert "AC2_AGENTCORE_MEMORY=create\n" in text
    assert "AC2_AGENTCORE_GATEWAY=none\n" in text
    assert "never reach the container" in text
    assert "Postgres" not in text


def test_the_agentcore_plan_shows_the_runtime_before_the_adapters_and_no_sidecar(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _agentcore(agentcore_ready, "--plan") == 0
    out = capsys.readouterr().out
    assert out.startswith(
        "Deploying helper to AgentCore Runtime with the settings in agents/helper/deploy.env.\n"
        "\nRuntime:\n"
    )
    assert "  protocol      HTTP on port 8080\n" in out
    assert "  inbound auth  iam (signed requests)\n" in out
    assert "  network       PUBLIC\n" in out
    assert "  artifact      container\n" in out
    assert "  memory        created by Terraform\n" in out
    assert "  gateway       none\n" in out
    assert "  telemetry     ambient (ADOT" in out
    assert out.index("Runtime:") < out.index("Adapters and what each needs from AWS:")
    assert (
        "\nContainer:\n"
        "  service: opentelemetry-instrument python -m helper.service --profile agentcore\n" in out
    )
    assert "collector:" not in out
    assert "  opa:" not in out
    assert "AC2_AGENTCORE_MEMORY=create: Terraform creates the memory" in out
    assert "  AC2_AGENTCORE_" not in out  # the block is the deployment's, not the environment's
    assert not (agentcore_ready / "deploy").exists()


def test_agentcore_refuses_the_services_own_telemetry_providers(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(agentcore_ready, {"AC2_TELEMETRY=ambient": "AC2_TELEMETRY=opentelemetry"})
    assert _agentcore(agentcore_ready, "--plan") == 1
    out = capsys.readouterr().out
    assert "collide with the ones ADOT installs" in out
    assert "set AC2_TELEMETRY=ambient (or off) in agents/helper/deploy.env" in out


def test_agentcore_refuses_the_older_audit_tracing_switch_with_telemetry_off(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(
        agentcore_ready,
        {
            "AC2_TELEMETRY=ambient": "AC2_TELEMETRY=off",
            'AC2_AUDIT_OPTIONS={"stream": "eap-audit"}': (
                'AC2_AUDIT_OPTIONS={"stream": "eap-audit", "tracing": true}'
            ),
        },
    )
    assert _agentcore(agentcore_ready, "--plan") == 1
    assert "the audit option tracing turns telemetry on" in capsys.readouterr().out


def test_agentcore_refuses_opa_on_the_loopback_address(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(
        agentcore_ready,
        {'{"url": "https://opa.internal:8181"}': '{"url": "http://localhost:8181"}'},
    )
    assert _agentcore(agentcore_ready, "--plan") == 1
    out = capsys.readouterr().out
    assert "nothing runs beside a container on AgentCore Runtime" in out
    assert "the in-process rules engine is for a developer's machine only" in out
    assert "Point url in AC2_POLICY_OPTIONS" in out


def test_agentcore_refuses_a_checkpoint_store_on_the_sessions_disk(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(
        agentcore_ready,
        {
            "AC2_CHECKPOINT_PROVIDER=agentcore_memory": "AC2_CHECKPOINT_PROVIDER=sqlite",
            'AC2_CHECKPOINT_OPTIONS={"memory_id": "created_by_terraform"}': (
                "AC2_CHECKPOINT_OPTIONS={}"
            ),
        },
    )
    assert _agentcore(agentcore_ready, "--plan") == 1
    out = capsys.readouterr().out
    assert "checkpoint sqlite runs only on a developer's machine" in out
    assert "checkpoint sqlite has nowhere durable to write" in out
    assert "set AC2_CHECKPOINT_PROVIDER to one of postgres, agentcore_memory, none" in out
    assert (
        "AC2_AGENTCORE_MEMORY=create while checkpoint sqlite does not use AgentCore Memory" in out
    )


def test_agentcore_refuses_a_python_the_runtime_does_not_offer(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pyproject = agentcore_ready / "agents/helper/pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    pyproject.write_text(
        text.replace('requires-python = ">=3.11"', 'requires-python = ">=3.11,<3.13"'), "utf-8"
    )
    assert _agentcore(agentcore_ready, "--plan") == 1
    out = capsys.readouterr().out
    assert 'requires-python = ">=3.11,<3.13" in agents/helper/pyproject.toml excludes it' in out
    assert "the runtime offers Python 3.13" in out


def test_agentcore_memory_named_in_the_block_must_be_the_checkpoints_memory(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(
        agentcore_ready,
        {"AC2_AGENTCORE_MEMORY=create": "AC2_AGENTCORE_MEMORY=helper_mem-0123456789"},
    )
    assert _agentcore(agentcore_ready, "--plan") == 1
    out = capsys.readouterr().out
    assert "AC2_AGENTCORE_MEMORY names the memory helper_mem-0123456789" in out
    assert "memory_id is 'created_by_terraform'" in out
    assert "make them agree" in out

    _edit_settings(
        agentcore_ready,
        {'"memory_id": "created_by_terraform"': '"memory_id": "helper_mem-0123456789"'},
    )
    assert _agentcore(agentcore_ready, "--plan") == 0
    out = capsys.readouterr().out
    assert "  memory        existing helper_mem-0123456789\n" in out
    assert "placeholder" not in out


def test_agentcore_memory_without_the_memory_checkpoint_is_a_warning(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(
        agentcore_ready,
        {
            "AC2_CHECKPOINT_PROVIDER=agentcore_memory": "AC2_CHECKPOINT_PROVIDER=none",
            'AC2_CHECKPOINT_OPTIONS={"memory_id": "created_by_terraform"}': "",
        },
    )
    assert _agentcore(agentcore_ready, "--plan") == 0
    out = capsys.readouterr().out
    assert "Warnings:" in out
    assert "AC2_AGENTCORE_MEMORY=create while checkpoint none does not use AgentCore Memory" in out
    assert "set AC2_CHECKPOINT_PROVIDER=agentcore_memory" in out


def test_agentcore_code_artifact_warns_about_wheels_and_writes_the_zip_builder(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(
        agentcore_ready, {"AC2_AGENTCORE_ARTIFACT=container": "AC2_AGENTCORE_ARTIFACT=code"}
    )
    assert _agentcore(agentcore_ready) == 0
    out = capsys.readouterr().out
    assert "  artifact      code\n" in out
    assert "AC2_AGENTCORE_ARTIFACT=code: every dependency needs a wheel for Python 3.13" in out
    assert "build_zip.py enforces that" in out
    assert "created  deploy/helper/build_zip.py" in out
    assert "Next: read deploy/helper/README.md, build the zip" in out
    folder = agentcore_ready / "deploy/helper"
    assert not (folder / "Dockerfile").exists()
    builder = (folder / "build_zip.py").read_text(encoding="utf-8")
    assert '"--entry", "helper.service",' in builder
    assert '"--lock", "deploy/helper/requirements.lock",' in builder
    assert "from agentcraft2_cli.deploy.zipbuild import main" in builder
    settings = (folder / "settings.tf").read_text(encoding="utf-8")
    assert '  artifact               = "code"\n' in settings


@pytest.mark.parametrize(
    ("replacements", "append", "named", "expected"),
    [
        (
            {"AC2_AGENTCORE_AUTH=iam": "AC2_AGENTCORE_AUTH=jwt"},
            "",
            "AC2_AGENTCORE_DISCOVERY_URL",
            "not set",
        ),
        (
            {"AC2_AGENTCORE_AUTH=iam": "AC2_AGENTCORE_AUTH=jwt"},
            "AC2_AGENTCORE_DISCOVERY_URL=https://issuer.example/openid\n",
            "AC2_AGENTCORE_DISCOVERY_URL",
            ".well-known/openid-configuration",
        ),
        (
            {"AC2_AGENTCORE_AUTH=iam": "AC2_AGENTCORE_AUTH=jwt"},
            "AC2_AGENTCORE_DISCOVERY_URL=https://issuer.example/.well-known/openid-configuration\n",
            "AC2_AGENTCORE_ALLOWED_CLIENTS",
            "neither is set",
        ),
        (
            {},
            "AC2_AGENTCORE_ALLOWED_AUDIENCE=api://helper\n",
            "AC2_AGENTCORE_AUTH",
            "AC2_AGENTCORE_AUTH=jwt when",
        ),
        (
            {"AC2_AGENTCORE_AUTH=iam": "AC2_AGENTCORE_AUTH=sigv4"},
            "",
            "AC2_AGENTCORE_AUTH",
            "iam or jwt",
        ),
        (
            {"AC2_AGENTCORE_NETWORK=PUBLIC": "AC2_AGENTCORE_NETWORK=VPC"},
            "",
            "AC2_AGENTCORE_SUBNETS",
            "not set",
        ),
        (
            {},
            "AC2_AGENTCORE_SUBNETS=subnet-1\n",
            "AC2_AGENTCORE_NETWORK",
            "AC2_AGENTCORE_NETWORK=VPC when",
        ),
        (
            {"AC2_AGENTCORE_IDLE_TIMEOUT_SECONDS=900": "AC2_AGENTCORE_IDLE_TIMEOUT_SECONDS=soon"},
            "",
            "AC2_AGENTCORE_IDLE_TIMEOUT_SECONDS",
            "a whole number of seconds from 60 to 28800",
        ),
        (
            {"AC2_AGENTCORE_MAX_LIFETIME_SECONDS=28800": "AC2_AGENTCORE_MAX_LIFETIME_SECONDS=600"},
            "",
            "AC2_AGENTCORE_IDLE_TIMEOUT_SECONDS",
            "no more than AC2_AGENTCORE_MAX_LIFETIME_SECONDS (600)",
        ),
        (
            {"AC2_AGENTCORE_ARTIFACT=container": "AC2_AGENTCORE_ARTIFACT=zip"},
            "",
            "AC2_AGENTCORE_ARTIFACT",
            "container or code",
        ),
        (
            {"AC2_AGENTCORE_GATEWAY=none": "AC2_AGENTCORE_GATEWAY=my-gateway"},
            "",
            "AC2_AGENTCORE_GATEWAY",
            "the ARN of a gateway",
        ),
        (
            {"AC2_AGENTCORE_MEMORY=create": "AC2_AGENTCORE_MEMORY=1-not-an-id"},
            "",
            "AC2_AGENTCORE_MEMORY",
            "the ID of a memory",
        ),
    ],
)
def test_a_bad_agentcore_value_names_the_variable_to_change(
    agentcore_ready: Path,
    capsys: pytest.CaptureFixture[str],
    replacements: dict[str, str],
    append: str,
    named: str,
    expected: str,
) -> None:
    _edit_settings(agentcore_ready, replacements, append)
    assert _agentcore(agentcore_ready, "--plan") == 1
    err = capsys.readouterr().err
    assert f"{named} in agents/helper/deploy.env is not valid" in err
    assert expected in err
    assert f"fix: change {named} in agents/helper/deploy.env" in err


def test_agentcore_jwt_and_vpc_settings_reach_settings_tf(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(
        agentcore_ready,
        {
            "AC2_AGENTCORE_AUTH=iam": "AC2_AGENTCORE_AUTH=jwt",
            "# AC2_AGENTCORE_DISCOVERY_URL=https://login.microsoftonline.com/<tenant>/v2.0/": (
                "AC2_AGENTCORE_DISCOVERY_URL=https://login.microsoftonline.com/t/v2.0/"
            ),
            "# AC2_AGENTCORE_ALLOWED_AUDIENCE=api://helper": (
                "AC2_AGENTCORE_ALLOWED_AUDIENCE=api://helper, api://two"
            ),
            "AC2_AGENTCORE_NETWORK=PUBLIC": "AC2_AGENTCORE_NETWORK=VPC",
            "# AC2_AGENTCORE_SUBNETS=": "AC2_AGENTCORE_SUBNETS=",
            "# AC2_AGENTCORE_SECURITY_GROUPS=": "AC2_AGENTCORE_SECURITY_GROUPS=",
            "AC2_AGENTCORE_GATEWAY=none": "AC2_AGENTCORE_GATEWAY=create",
        },
    )
    assert _agentcore(agentcore_ready) == 0
    out = capsys.readouterr().out
    assert "inbound auth  jwt (tokens from https://login.microsoftonline.com/t/v2.0/" in out
    assert (
        "  network       VPC (subnet-0123456789abcdef0, subnet-0fedcba9876543210; "
        "sg-0123456789abcdef0)\n" in out
    )
    assert "  gateway       created by Terraform, one target per MCP server\n" in out
    settings = (agentcore_ready / "deploy/helper/settings.tf").read_text(encoding="utf-8")
    assert '  auth                   = "jwt"\n' in settings
    assert (
        '  discovery_url          = "https://login.microsoftonline.com/t/v2.0/.well-known/openid-configuration"\n'
    ) in settings
    assert "  allowed_clients        = []\n" in settings
    assert '  allowed_audience       = ["api://helper", "api://two"]\n' in settings
    assert '  network_mode           = "VPC"\n' in settings
    assert (
        '  subnets                = ["subnet-0123456789abcdef0", "subnet-0fedcba9876543210"]\n'
        in settings
    )
    assert '  security_groups        = ["sg-0123456789abcdef0"]\n' in settings
    assert '  gateway                = "create"\n' in settings


def test_agentcore_environment_leaves_the_block_out_and_keeps_an_empty_value(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(agentcore_ready, append="AC2_TLS_CA_BUNDLE=\n")
    assert _agentcore(agentcore_ready) == 0
    capsys.readouterr()
    settings = (agentcore_ready / "deploy/helper/settings.tf").read_text(encoding="utf-8")
    environment = settings[settings.index("environment = {") :]
    assert "AGENTCORE_" not in environment
    assert '    AC2_TLS_CA_BUNDLE       = ""\n' in environment
    assert '  memory_option_variable = "AC2_CHECKPOINT_OPTIONS"\n' in settings
    assert (
        '    AC2_CHECKPOINT_OPTIONS  = "{\\"memory_id\\": \\"created_by_terraform\\"}"\n'
        in settings
    )


def test_agentcore_writes_a_root_module_alone_and_an_arm64_image_per_kind(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _agentcore(agentcore_ready) == 0
    out = capsys.readouterr().out
    assert "created  deploy/helper/main.tf" in out
    assert "created  deploy/helper/permissions.tf" in out
    assert "Next: read deploy/helper/README.md, build and push the ARM64 image" in out
    assert not (agentcore_ready / "deploy/modules").exists()
    assert not (agentcore_ready / "deploy/opa").exists()
    assert not (agentcore_ready / "deploy/helper/build_zip.py").exists()
    dockerfile = (agentcore_ready / "deploy/helper/Dockerfile").read_text(encoding="utf-8")
    assert "FROM --platform=linux/arm64 python:3.13-slim\n" in dockerfile
    assert "EXPOSE 8080\n" in dockerfile
    assert (
        'CMD ["opentelemetry-instrument", "python", "-m", "helper.service", '
        '"--profile", "agentcore"]' in dockerfile
    )
    # ADOT comes from the hash-locked requirements, never from an install of its own.
    assert 'pip install --no-cache-dir "aws-opentelemetry-distro' not in dockerfile
    assert "command -v opentelemetry-instrument" in dockerfile
    assert "--require-hashes" in dockerfile
    wanted = (agentcore_ready / "deploy/helper/requirements.in").read_text(encoding="utf-8")
    assert "aws-opentelemetry-distro>=0.18.0" in wanted.splitlines()
    assert "USER 10001" in dockerfile
    rules = (agentcore_ready / "deploy/helper/Dockerfile.dockerignore").read_text(encoding="utf-8")
    assert rules.splitlines()[1] == "**"
    assert "!agents/helper/src/**" in rules
    main_tf = (agentcore_ready / "deploy/helper/main.tf").read_text(encoding="utf-8")
    assert 'version = ">= 6.20"' in main_tf
    assert 'resource "aws_bedrockagentcore_agent_runtime" "this"' in main_tf
    assert '"bedrock-agentcore.amazonaws.com"' in main_tf
    permissions = (agentcore_ready / "deploy/helper/permissions.tf").read_text(encoding="utf-8")
    assert permissions.startswith(
        "# Written by 'agentcraft2 deploy helper --target agentcore' "
        "from agents/helper/deploy.env.\n"
    )
    assert 'sid       = "CheckpointAgentcoreMemory1"' in permissions

    # An MCP server listens on the runtime's MCP port, served by its own package.
    assert _agentcore(agentcore_ready, service="people") == 0
    _depend_on_aws(agentcore_ready, "mcp-servers/people")
    _edit_settings(agentcore_ready, folder="mcp-servers/people", append="AC2_DATA_SOURCES={}\n")
    capsys.readouterr()
    assert _agentcore(agentcore_ready, service="people") == 0
    out = capsys.readouterr().out
    assert "  protocol      MCP on port 8000\n" in out
    assert "  memory        none\n" in out
    dockerfile = (agentcore_ready / "deploy/people/Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE 8000\n" in dockerfile
    assert (
        'CMD ["opentelemetry-instrument", "python", "-m", "people", "--profile", "agentcore"]'
        in dockerfile
    )


def test_agentcore_running_again_rewrites_what_is_derived_and_keeps_what_is_yours(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _agentcore(agentcore_ready) == 0
    main_tf = agentcore_ready / "deploy/helper/main.tf"
    main_tf.write_text(main_tf.read_text(encoding="utf-8") + "# mine\n", encoding="utf-8")
    _edit_settings(
        agentcore_ready,
        {"AC2_AGENTCORE_IDLE_TIMEOUT_SECONDS=900": "AC2_AGENTCORE_IDLE_TIMEOUT_SECONDS=600"},
    )
    capsys.readouterr()

    assert _agentcore(agentcore_ready) == 0
    out = capsys.readouterr().out
    assert "kept     deploy/helper/main.tf (yours)" in out
    assert "updated  deploy/helper/settings.tf" in out
    assert main_tf.read_text(encoding="utf-8").endswith("# mine\n")
    assert (
        "  idle_timeout_seconds   = 600\n"
        in (agentcore_ready / "deploy/helper/settings.tf").read_text()
    )


def test_the_earlier_spelling_of_a_deployment_variable_still_works_and_is_reported(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(agentcore_ready, {"AC2_AGENTCORE_AUTH=iam": "AGENTCORE_AUTH=iam"})
    assert _agentcore(agentcore_ready, "--plan") == 0
    out = capsys.readouterr().out
    assert "  AGENTCORE_AUTH -> AC2_AGENTCORE_AUTH" in out
    assert "inbound auth  iam" in out
    # Under either spelling the variable stays out of the container.
    assert "  AGENTCORE_AUTH  " not in out


def test_a_deployment_variable_that_nothing_reads_is_refused_with_a_suggestion(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(agentcore_ready, append="AC2_AGENTCORE_NETWROK=VPC\n")
    assert _agentcore(agentcore_ready, "--plan") == 1
    err = capsys.readouterr().err
    assert "AC2_AGENTCORE_NETWROK in agents/helper/deploy.env is not a deployment variable" in err
    assert "did you mean AC2_AGENTCORE_NETWORK?" in err


def test_both_spellings_of_a_deployment_variable_must_agree(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(agentcore_ready, append="AGENTCORE_AUTH=jwt\n")
    assert _agentcore(agentcore_ready, "--plan") == 1
    assert "AC2_AGENTCORE_AUTH is set twice" in capsys.readouterr().err


def test_a_profile_for_another_platform_is_a_problem(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _edit_settings(agentcore_ready, {"AC2_PROFILE=agentcore": "AC2_PROFILE=aws"})
    assert _agentcore(agentcore_ready, "--plan") == 1
    out = capsys.readouterr().out
    assert "AC2_PROFILE is aws in agents/helper/deploy.env" in out
    assert "set AC2_PROFILE=agentcore" in out


def test_the_fargate_target_is_the_default_and_ignores_the_agentcore_block(
    agentcore_ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same deploy.env, deployed to Fargate: the block is not the service's environment."""
    _edit_settings(
        agentcore_ready,
        {"AC2_TELEMETRY=ambient": "AC2_TELEMETRY=off", "AC2_PROFILE=agentcore": "AC2_PROFILE=aws"},
    )
    assert run("deploy", "helper", "--plan", "--workspace", agentcore_ready) == 0
    out = capsys.readouterr().out
    assert out.startswith("Deploying helper with the settings in agents/helper/deploy.env.\n")
    assert "Containers in the task:" in out
    assert "  service: helper-serve --host 0.0.0.0 --port 8000" in out
    assert "  AC2_AGENTCORE_" not in out


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("terraform") is None, reason="needs terraform on PATH")
@pytest.mark.parametrize(
    "replacements",
    [
        {},
        {
            "AC2_AGENTCORE_ARTIFACT=container": "AC2_AGENTCORE_ARTIFACT=code",
            "AC2_AGENTCORE_AUTH=iam": "AC2_AGENTCORE_AUTH=jwt",
            "# AC2_AGENTCORE_DISCOVERY_URL=https://login.microsoftonline.com/<tenant>/v2.0/": (
                "AC2_AGENTCORE_DISCOVERY_URL=https://login.microsoftonline.com/t/v2.0/"
            ),
            "# AC2_AGENTCORE_ALLOWED_AUDIENCE=api://helper": "AC2_AGENTCORE_ALLOWED_AUDIENCE=api://helper",
            "AC2_AGENTCORE_GATEWAY=none": "AC2_AGENTCORE_GATEWAY=create",
            "AC2_AGENTCORE_MEMORY=create": "AC2_AGENTCORE_MEMORY=none",
            'AC2_CHECKPOINT_OPTIONS={"memory_id": "created_by_terraform"}': (
                'AC2_CHECKPOINT_OPTIONS={"memory_id": "helper_mem-0123456789"}'
            ),
        },
    ],
    ids=["container-iam-memory", "code-jwt-gateway"],
)
def test_the_written_agentcore_terraform_is_formatted_and_valid(
    agentcore_ready: Path, replacements: dict[str, str]
) -> None:
    _edit_settings(agentcore_ready, replacements)
    assert _agentcore(agentcore_ready) == 0
    folder = agentcore_ready / "deploy" / "helper"

    def terraform(*arguments: str) -> None:
        done = subprocess.run(  # noqa: S603 - a fixed command line, no shell
            ["terraform", *arguments],  # noqa: S607 - terraform from PATH, as a developer runs it
            cwd=folder,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0, f"terraform {' '.join(arguments)}:\n{done.stdout}{done.stderr}"

    terraform("fmt", "-check", "-recursive")
    terraform("init", "-backend=false", "-input=false")
    terraform("validate")
