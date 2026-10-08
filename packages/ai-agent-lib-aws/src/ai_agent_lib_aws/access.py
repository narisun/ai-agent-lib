"""What each AWS adapter needs from IAM, derived from its options.

Each rule names the API calls its adapter makes and narrows the resources as
far as the options allow. Where a resource cannot be narrowed from the
options alone (an identifier AWS assigns, for instance) the access carries a
note that tells the reviewer what to check.

The rules only describe. They make no AWS call and need no credentials.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from ai_agent_lib_aws.audit_firehose import FirehoseAuditOptions
from ai_agent_lib_aws.data_redshift import RedshiftDataOptions
from ai_agent_lib_aws.guardrails_bedrock import BedrockGuardrailsOptions
from ai_agent_lib_aws.registry_s3 import S3RegistryOptions
from ai_agent_lib_aws.secrets_manager import SecretsManagerOptions
from ai_agent_lib_core.contracts import Access, AccessQuery
from ai_agent_lib_core.contracts.access import ACCOUNT, REGION

__all__ = [
    "bedrock_guardrails_access",
    "bedrock_model_access",
    "firehose_audit_access",
    "postgres_checkpoint_access",
    "redshift_data_access",
    "s3_registry_access",
    "secrets_manager_access",
]

# A cross-region inference profile is a geography and a model, such as "us.anthropic...".
_PROFILE = re.compile(r"^(?P<geo>us|us-gov|eu|apac|jp|au|ca|global)\.(?P<model>.+)$")
_INVOKE = ("bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream")


@dataclass(frozen=True, slots=True)
class _ModelGrant:
    invoke: tuple[str, ...]
    look_up: tuple[str, ...] = ()
    note: str = ""


_ANY_MODEL = "arn:aws:bedrock:*::foundation-model/*"


def _model_resources(model_id: str) -> _ModelGrant:
    """What invoking one model takes: the profile, and the models behind it."""
    if model_id.startswith("arn:"):
        resource = model_id.split(":", 5)[-1]
        if resource.startswith("application-inference-profile/"):
            # Its models are chosen when the profile is made and cannot be read
            # from its ARN, but AWS checks the call against them too.
            return _ModelGrant(
                invoke=(model_id, _ANY_MODEL),
                look_up=(model_id,),
                note="an application inference profile's models are not in its ARN; replace "
                "foundation-model/* with the model ARNs the profile copies",
            )
        if resource.startswith("inference-profile/"):
            profile = _PROFILE.match(resource.removeprefix("inference-profile/"))
            model = profile["model"] if profile else "*"
            return _ModelGrant(
                invoke=(model_id, f"arn:aws:bedrock:*::foundation-model/{model}"),
                look_up=(model_id,),
            )
        # A foundation model or a provisioned model, named exactly.
        return _ModelGrant(invoke=(model_id,))
    profile = _PROFILE.match(model_id)
    if profile is None:
        return _ModelGrant(invoke=(f"arn:aws:bedrock:{REGION}::foundation-model/{model_id}",))
    arn = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:inference-profile/{model_id}"
    # The profile routes to the model in any region of its geography.
    return _ModelGrant(
        invoke=(arn, f"arn:aws:bedrock:*::foundation-model/{profile['model']}"), look_up=(arn,)
    )


def bedrock_model_access(query: AccessQuery) -> Sequence[Access]:
    """Bedrock models: invoke each configured model, and look up its inference profile."""
    if not query.model_ids:
        return (
            Access(
                actions=_INVOKE,
                resources=(_ANY_MODEL.replace("*::", f"{REGION}::", 1),),
                why="call a Bedrock model",
                note="no model is configured for bedrock, so every foundation model is allowed; "
                "configure the model to narrow this",
            ),
        )
    invoked: dict[str, None] = {}
    profiles: dict[str, None] = {}
    notes: dict[str, None] = {}
    for model_id in query.model_ids:
        grant = _model_resources(model_id)
        invoked.update(dict.fromkeys(grant.invoke))
        profiles.update(dict.fromkeys(grant.look_up))
        if grant.note:
            notes[grant.note] = None
    found = [
        Access(
            actions=_INVOKE,
            resources=tuple(invoked),
            why="call the configured models",
            note="; ".join(notes),
        )
    ]
    if profiles:
        found.append(
            Access(
                actions=("bedrock:GetInferenceProfile",),
                resources=tuple(profiles),
                why="find the model behind an inference profile",
            )
        )
    return found


def bedrock_guardrails_access(query: AccessQuery) -> Sequence[Access]:
    """Bedrock guardrails: apply the configured guardrail."""
    options = query.selection.parse_options(BedrockGuardrailsOptions)
    guardrail = options.guardrail_id
    if not guardrail.startswith("arn:"):
        guardrail = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail/{guardrail}"
    return (
        Access(
            actions=("bedrock:ApplyGuardrail",),
            resources=(guardrail,),
            why="check content with the configured guardrail",
        ),
    )


def secrets_manager_access(query: AccessQuery) -> Sequence[Access]:
    """Secrets Manager: read the secrets under the configured prefix."""
    options = query.selection.parse_options(SecretsManagerOptions)
    note = (
        ""
        if options.prefix
        else "no prefix is configured, so every secret in the account can be read; "
        "set the prefix option to the folder of this service's secrets"
    )
    return (
        Access(
            actions=("secretsmanager:GetSecretValue",),
            # Secrets Manager adds six random characters to every secret's ARN.
            resources=(f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:{options.prefix}*",),
            why="read the service's secrets",
            note=note,
        ),
    )


def firehose_audit_access(query: AccessQuery) -> Sequence[Access]:
    """Firehose: write audit records to the configured delivery stream."""
    options = query.selection.parse_options(FirehoseAuditOptions)
    return (
        Access(
            actions=("firehose:PutRecord", "firehose:DescribeDeliveryStream"),
            resources=(f"arn:aws:firehose:{REGION}:{ACCOUNT}:deliverystream/{options.stream}",),
            why="write audit records, and check at startup that the stream is active",
        ),
    )


def s3_registry_access(query: AccessQuery) -> Sequence[Access]:
    """S3 registry: read the two registry files."""
    options = query.selection.parse_options(S3RegistryOptions)
    return (
        Access(
            actions=("s3:GetObject",),
            resources=(
                f"arn:aws:s3:::{options.bucket}/{options.agents_key}",
                f"arn:aws:s3:::{options.bucket}/{options.tools_key}",
            ),
            why="read the agent and MCP tool registries",
        ),
    )


def redshift_data_access(query: AccessQuery) -> Sequence[Access]:
    """Redshift Data API: run the source's queries as its configured identity."""
    options = query.selection.parse_options(RedshiftDataOptions)
    what = f"run the queries of data source {query.instance or 'redshift'}"
    found: list[Access] = []
    if options.workgroup is not None:
        workgroup = f"arn:aws:redshift-serverless:{REGION}:{ACCOUNT}:workgroup/*"
        id_note = (
            f"a workgroup's ARN holds its ID, not its name; replace * with the ID of "
            f"workgroup {options.workgroup}"
        )
        found.append(
            Access(
                actions=("redshift-data:ExecuteStatement",),
                resources=(workgroup,),
                why=what,
                note=id_note,
            )
        )
        if options.secret_arn is None:
            found.append(
                Access(
                    actions=("redshift-serverless:GetCredentials",),
                    resources=(workgroup,),
                    why="sign in to the workgroup as the service's role",
                    note=id_note,
                )
            )
    else:
        cluster = options.cluster_id
        found.append(
            Access(
                actions=("redshift-data:ExecuteStatement",),
                resources=(f"arn:aws:redshift:{REGION}:{ACCOUNT}:cluster:{cluster}",),
                why=what,
            )
        )
        database = f"arn:aws:redshift:{REGION}:{ACCOUNT}:dbname:{cluster}/{options.database}"
        if options.db_user is not None:
            found.append(
                Access(
                    actions=("redshift:GetClusterCredentials",),
                    resources=(
                        f"arn:aws:redshift:{REGION}:{ACCOUNT}:dbuser:{cluster}/{options.db_user}",
                        database,
                    ),
                    why="sign in to the cluster as the configured database user",
                )
            )
        elif options.secret_arn is None:
            found.append(
                Access(
                    actions=("redshift:GetClusterCredentialsWithIAM",),
                    resources=(database,),
                    why="sign in to the cluster as the service's role",
                )
            )
    if options.secret_arn is not None:
        found.append(
            Access(
                actions=("secretsmanager:GetSecretValue",),
                resources=(options.secret_arn,),
                why="read the database credentials of the data source",
            )
        )
    found.append(
        Access(
            # These calls take a statement ID, which AWS scopes to its caller.
            actions=(
                "redshift-data:DescribeStatement",
                "redshift-data:GetStatementResult",
                "redshift-data:CancelStatement",
            ),
            resources=("*",),
            why="wait for a query, read its rows, and stop it at its deadline",
            note="these actions take no resource; AWS lets a caller see only its own statements",
        )
    )
    return found


def _postgres_user(query: AccessQuery) -> str:
    try:
        from ai_agent_lib_aws.checkpoint_postgres import PostgresCheckpointOptions
    except ImportError:
        # Without the driver the options model is not importable; the user is enough.
        user = query.selection.options.get("user")
        return user if isinstance(user, str) and user else "*"
    return query.selection.parse_options(PostgresCheckpointOptions).user


def postgres_checkpoint_access(query: AccessQuery) -> Sequence[Access]:
    """Postgres checkpoints: sign in to the database with an IAM token."""
    user = _postgres_user(query)
    return (
        Access(
            actions=("rds-db:connect",),
            resources=(f"arn:aws:rds-db:{REGION}:{ACCOUNT}:dbuser:*/{user}",),
            why="sign in to the checkpoint database as the configured user",
            note="replace * with the resource ID of the database cluster or instance "
            "(it starts with cluster- or db-)",
        ),
    )
