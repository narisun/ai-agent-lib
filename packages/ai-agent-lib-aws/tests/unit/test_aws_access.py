"""What each AWS adapter needs from IAM, derived from its options alone."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

import ai_agent_lib_aws
from ai_agent_lib_aws.access import (
    bedrock_guardrails_access,
    bedrock_model_access,
    firehose_audit_access,
    postgres_checkpoint_access,
    redshift_data_access,
    s3_registry_access,
    secrets_manager_access,
)
from ai_agent_lib_core.config import ConfigResolver, MappingConfigSource
from ai_agent_lib_core.contracts import Access, AccessQuery, ConfigurationError, ProviderSelection
from ai_agent_lib_core.di import access_plan

_AWS_OPTIONS = {
    "EAP_AUDIT_OPTIONS": '{"stream": "eap-audit"}',
    "EAP_REGISTRY_OPTIONS": '{"bucket": "eap-registry"}',
    "EAP_GUARDRAILS_OPTIONS": '{"guardrail_id": "g1", "guardrail_version": "3"}',
    "EAP_CHECKPOINT_OPTIONS": '{"host": "db.internal", "database": "eap", "user": "eap_app"}',
    "EAP_SECRETS_OPTIONS": '{"prefix": "eap/helper/"}',
}


def _query(provider: str, *, model_ids: tuple[str, ...] = (), **options: Any) -> AccessQuery:
    return AccessQuery(ProviderSelection(provider, options), model_ids=model_ids, instance="ledger")


def _resources(found: Any) -> list[str]:
    return [resource for access in found for resource in access.resources]


def test_a_foundation_model_is_invoked_by_its_own_arn_and_nothing_else() -> None:
    found = bedrock_model_access(_query("bedrock", model_ids=("anthropic.claude-x",)))

    assert found == [
        Access(
            ("bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"),
            ("arn:aws:bedrock:{region}::foundation-model/anthropic.claude-x",),
            "call the configured models",
        )
    ]


def test_an_inference_profile_also_reaches_its_model_in_every_region_and_is_looked_up() -> None:
    found = bedrock_model_access(_query("bedrock", model_ids=("eu.anthropic.claude-x",)))

    profile = "arn:aws:bedrock:{region}:{account}:inference-profile/eu.anthropic.claude-x"
    assert found[0].resources == (profile, "arn:aws:bedrock:*::foundation-model/anthropic.claude-x")
    assert found[1] == Access(
        ("bedrock:GetInferenceProfile",), (profile,), "find the model behind an inference profile"
    )


def test_a_model_given_as_an_arn_is_used_exactly() -> None:
    arn = "arn:aws:bedrock:eu-west-1:123456789012:provisioned-model/abc"

    assert _resources(bedrock_model_access(_query("bedrock", model_ids=(arn,)))) == [arn]


def test_with_no_model_configured_the_wide_grant_carries_a_note() -> None:
    (found,) = bedrock_model_access(_query("bedrock"))

    assert found.resources == ("arn:aws:bedrock:{region}::foundation-model/*",)
    assert "configure the model" in found.note


def test_a_guardrail_is_applied_by_id_or_by_its_arn() -> None:
    by_id = bedrock_guardrails_access(_query("bedrock", guardrail_id="g1", guardrail_version="3"))
    arn = "arn:aws:bedrock:eu-west-1:123456789012:guardrail/g1"
    by_arn = bedrock_guardrails_access(_query("bedrock", guardrail_id=arn, guardrail_version="3"))

    assert _resources(by_id) == ["arn:aws:bedrock:{region}:{account}:guardrail/g1"]
    assert _resources(by_arn) == [arn]


def test_secrets_are_read_under_the_prefix_and_no_prefix_is_flagged() -> None:
    (scoped,) = secrets_manager_access(_query("secrets_manager", prefix="eap/helper/"))
    (wide,) = secrets_manager_access(_query("secrets_manager"))

    assert scoped.resources == ("arn:aws:secretsmanager:{region}:{account}:secret:eap/helper/*",)
    assert scoped.note == ""
    assert "every secret in the account" in wide.note


def test_audit_records_go_to_the_one_stream() -> None:
    found = firehose_audit_access(_query("firehose", stream="eap-audit"))

    assert found[0].actions == ("firehose:PutRecord", "firehose:DescribeDeliveryStream")
    assert _resources(found) == ["arn:aws:firehose:{region}:{account}:deliverystream/eap-audit"]


def test_the_registry_reads_only_its_two_files() -> None:
    found = s3_registry_access(_query("s3_file", bucket="eap-registry"))

    assert _resources(found) == [
        "arn:aws:s3:::eap-registry/registry/agents.yaml",
        "arn:aws:s3:::eap-registry/registry/mcp-tools.yaml",
    ]


def test_a_cluster_with_a_database_user_signs_in_as_that_user_only() -> None:
    found = redshift_data_access(
        _query(
            "redshift_data",
            queries_dir="queries",
            database="sales",
            cluster_id="c1",
            db_user="reader",
        )
    )

    assert [access.actions[0] for access in found] == [
        "redshift-data:ExecuteStatement",
        "redshift:GetClusterCredentials",
        "redshift-data:DescribeStatement",
    ]
    assert found[1].resources == (
        "arn:aws:redshift:{region}:{account}:dbuser:c1/reader",
        "arn:aws:redshift:{region}:{account}:dbname:c1/sales",
    )
    assert "ledger" in found[0].why


def test_a_workgroup_signs_in_as_the_role_and_notes_the_id_to_fill_in() -> None:
    found = redshift_data_access(
        _query("redshift_data", queries_dir="q", database="sales", workgroup="analytics")
    )

    assert [access.actions[0] for access in found][:2] == [
        "redshift-data:ExecuteStatement",
        "redshift-serverless:GetCredentials",
    ]
    assert "workgroup analytics" in found[0].note


def test_a_cluster_with_a_secret_reads_that_secret_only() -> None:
    secret = "arn:aws:secretsmanager:eu-west-1:123456789012:secret:db-AbCdEf"
    found = redshift_data_access(
        _query("redshift_data", queries_dir="q", database="s", cluster_id="c1", secret_arn=secret)
    )

    assert ("secretsmanager:GetSecretValue",) in [access.actions for access in found]
    assert secret in _resources(found)
    assert not any("GetClusterCredentials" in a.actions[0] for a in found)


def test_the_checkpoint_database_is_reached_as_its_user() -> None:
    (found,) = postgres_checkpoint_access(
        _query("postgres", host="db.internal", database="eap", user="eap_app")
    )

    assert found.resources == ("arn:aws:rds-db:{region}:{account}:dbuser:*/eap_app",)
    assert "resource ID" in found.note


def test_bad_options_fail_as_configuration_errors() -> None:
    with pytest.raises(ConfigurationError):
        firehose_audit_access(_query("firehose", stream="no spaces allowed"))


def test_the_aws_profile_derives_a_complete_plan() -> None:
    config = ConfigResolver(
        MappingConfigSource(
            {
                "EAP_PROFILE": "aws",
                "EAP_DEPLOYMENT_ENV": "prod",
                "EAP_MODEL_ID": "us.anthropic.claude-x",
                **_AWS_OPTIONS,
            }
        )
    ).resolve()

    plan = access_plan(config)

    assert plan.local_only == ()
    assert plan.undeclared == ()
    assert {adapter.label for adapter in plan.adapters} >= {"model bedrock", "audit firehose"}
    assert "arn:aws:s3:::eap-registry/registry/agents.yaml" in _resources(plan.access)


def test_a_missing_option_says_which_adapter_was_being_worked_out() -> None:
    values = {"EAP_PROFILE": "aws", **_AWS_OPTIONS, "EAP_AUDIT_OPTIONS": "{}"}
    config = ConfigResolver(MappingConfigSource(values)).resolve()

    with pytest.raises(ConfigurationError) as caught:
        access_plan(config)

    assert "stream" in str(caught.value)
    assert caught.value.__notes__ == [
        "while working out what the audit adapter (firehose) needs from the cloud"
    ]


_SDK_CALL = re.compile(r"self\._client\.([a-z_]+)")
_ADAPTERS = [
    # module, IAM service prefix, the rule, options that reach every call
    ("audit_firehose", "firehose", firehose_audit_access, {"stream": "s"}),
    ("registry_s3", "s3", s3_registry_access, {"bucket": "eap-registry"}),
    ("secrets_manager", "secretsmanager", secrets_manager_access, {"prefix": "p/"}),
    (
        "guardrails_bedrock",
        "bedrock",
        bedrock_guardrails_access,
        {"guardrail_id": "g1", "guardrail_version": "3"},
    ),
    (
        "data_redshift",
        "redshift-data",
        redshift_data_access,
        {"queries_dir": "q", "database": "d", "workgroup": "w"},
    ),
]


@pytest.mark.parametrize(("module", "prefix", "rule", "options"), _ADAPTERS)
def test_every_sdk_call_an_adapter_makes_is_declared(
    module: str, prefix: str, rule: Any, options: dict[str, Any]
) -> None:
    # Startup checks count: a readiness probe that is refused keeps a task from serving.
    source = (Path(ai_agent_lib_aws.__file__).parent / f"{module}.py").read_text(encoding="utf-8")
    called = {
        f"{prefix}:{''.join(part.title() for part in name.split('_'))}"
        for name in _SDK_CALL.findall(source)
    }
    declared = {action for access in rule(_query("x", **options)) for action in access.actions}

    assert called
    assert called <= declared, f"{module} calls {sorted(called - declared)} but does not declare it"
