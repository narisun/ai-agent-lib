"""'agentlib deploy': a service's deployment, derived from the settings it runs with."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from ai_agent_lib_cli.__main__ import main
from ai_agent_lib_cli.deploy import hcl_string
from ai_agent_lib_cli.testing import toolbox_for_tests

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


def _depend_on_aws(root: Path, folder: str) -> None:
    pyproject = root / folder / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    pyproject.write_text(
        text.replace("dependencies = [\n", 'dependencies = [\n    "ai-agent-lib-aws",\n', 1),
        encoding="utf-8",
    )


def _settings(root: Path, folder: str = "agents/helper") -> Path:
    return root / folder / "deploy.env"


@pytest.fixture
def ready(workspace: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    """A workspace whose agent has a starting deploy.env and depends on the AWS adapters."""
    assert run("deploy", "helper", "--workspace", workspace) == 0
    _depend_on_aws(workspace, "agents/helper")
    capsys.readouterr()
    return workspace


def test_the_first_run_writes_a_starting_settings_file_and_nothing_else(
    workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("deploy", "helper", "--plan", "--workspace", workspace) == 1
    err = capsys.readouterr().err
    assert "agents/helper/deploy.env does not exist" in err
    assert "fix: run 'agentlib deploy helper' to write a starting one" in err

    assert run("deploy", "helper", "--workspace", workspace) == 0
    out = capsys.readouterr().out
    assert "created  agents/helper/deploy.env" in out
    assert 'add "ai-agent-lib-aws" to the dependencies in agents/helper/pyproject.toml' in out
    text = _settings(workspace).read_text(encoding="utf-8")
    assert "EAP_PROFILE=aws" in text
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
    assert "which helper does not depend on" in captured.out
    assert 'add "ai-agent-lib-aws" to the dependencies in agents/helper/pyproject.toml' in (
        captured.out
    )
    assert "1 problem stop the deployment" in captured.err
    assert not (workspace / "deploy").exists()


def test_deploy_writes_the_module_and_the_files_derived_from_the_settings(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("deploy", "helper", "--workspace", ready) == 0
    out = capsys.readouterr().out
    assert "created  deploy/helper/permissions.tf" in out
    assert "created  deploy/modules/agentlib-service/main.tf" in out
    assert "created  deploy/opa/bundle/agentlib/authz/authz.rego" in out
    assert not (ready / "deploy/opa/bundle/agentlib/authz/authz_test.rego").exists()

    permissions = (ready / "deploy/helper/permissions.tf").read_text(encoding="utf-8")
    assert "  # audit firehose: write audit records, and check at startup" in permissions
    assert 'sid       = "AuditFirehose1"' in permissions
    assert (
        '"arn:aws:firehose:${var.region}:${data.aws_caller_identity.current.account_id}'
        ':deliverystream/eap-audit"'
    ) in permissions
    settings = (ready / "deploy/helper/settings.tf").read_text(encoding="utf-8")
    assert "  uses_opa  = true\n" in settings
    assert '    EAP_AUDIT_OPTIONS      = "{\\"stream\\": \\"eap-audit\\"}"\n' in settings
    dockerfile = (ready / "deploy/helper/Dockerfile").read_text(encoding="utf-8")
    assert "uv sync --frozen --no-dev --package helper" in dockerfile
    assert 'CMD ["helper-serve", "--host", "0.0.0.0", "--port", "8000"]' in dockerfile


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


def test_a_secret_in_the_settings_is_refused_and_its_value_never_shown(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _settings(ready).open("a", encoding="utf-8") as file:
        file.write("EAP_SECRET_RATES_TOKEN=s3cr3t-value\n")

    assert run("deploy", "helper", "--plan", "--workspace", ready) == 1
    captured = capsys.readouterr()
    assert "holds secrets" in captured.err
    assert "EAP_SECRET_RATES_TOKEN" in captured.err
    assert "s3cr3t-value" not in captured.out + captured.err


def test_settings_for_a_developers_machine_are_refused(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings(ready)
    settings.write_text(
        settings.read_text(encoding="utf-8").replace(
            "EAP_DEPLOYMENT_ENV=dev", "EAP_DEPLOYMENT_ENV=local"
        ),
        encoding="utf-8",
    )

    assert run("deploy", "helper", "--plan", "--workspace", ready) == 1
    err = capsys.readouterr().err
    assert "expected: EAP_DEPLOYMENT_ENV=dev or EAP_DEPLOYMENT_ENV=prod" in err
    assert "got: EAP_DEPLOYMENT_ENV is local" in err


def test_a_local_only_adapter_is_a_problem_that_names_itself(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _settings(ready).open("a", encoding="utf-8") as file:
        file.write("EAP_AUDIT_PROVIDER=jsonl\nEAP_AUDIT_OPTIONS={}\n")

    assert run("deploy", "helper", "--plan", "--workspace", ready) == 1
    out = capsys.readouterr().out
    assert "audit jsonl runs only on a developer's machine" in out


def test_a_bad_option_says_which_adapter_and_which_file(
    ready: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with _settings(ready).open("a", encoding="utf-8") as file:
        file.write('EAP_AUDIT_OPTIONS={"stream": "has spaces"}\n')

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
    assert "EAP_CHECKPOINT_PROVIDER=none" in settings.read_text(encoding="utf-8")
    # An OPA server elsewhere needs no sidecar.
    with settings.open("a", encoding="utf-8") as file:
        file.write('EAP_POLICY_OPTIONS={"url": "https://opa.internal:8181"}\n')
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
    for folder in (deploy / "helper", deploy / "modules" / "agentlib-service"):
        terraform("init", "-backend=false", "-input=false", folder=folder)
        terraform("validate", folder=folder)
    terraform("test", folder=deploy / "modules" / "agentlib-service")
