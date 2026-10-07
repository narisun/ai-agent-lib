"""The AWS provider pack: which AWS adapters exist and how each is built."""

from __future__ import annotations

from ai_agent_lib_aws.audit_firehose import FirehoseAuditOptions, FirehoseAuditSink
from ai_agent_lib_aws.data_redshift import RedshiftDataOptions, RedshiftDataSource
from ai_agent_lib_aws.guardrails_bedrock import (
    DRAFT_VERSION,
    BedrockGuardrails,
    BedrockGuardrailsOptions,
)
from ai_agent_lib_aws.model_bedrock import BedrockChatModelProvider
from ai_agent_lib_aws.registry_s3 import S3RegistryOptions, S3RegistrySource
from ai_agent_lib_aws.secrets_manager import SecretsManagerOptions, SecretsManagerProvider
from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import (
    CheckpointBackend,
    ConfigurationError,
    DeploymentEnv,
    Section,
)
from ai_agent_lib_core.kit import DATA_PORT, MODEL_PORT, BuildContext, ServiceProviders

__all__ = ["register_aws_adapters"]


class _Sessions:
    """Hands every adapter of one service the same AWS session factory."""

    def __init__(self, sessions: AwsSessionFactory | None) -> None:
        self._sessions = sessions

    def get(self, context: BuildContext) -> AwsSessionFactory:
        if self._sessions is None:
            self._sessions = AwsSessionFactory.from_settings(
                context.external, tls_ca_bundle=context.tls_ca_bundle
            )
        return self._sessions


def register_aws_adapters(
    providers: ServiceProviders,
    *,
    sessions: AwsSessionFactory | None = None,
    replace: bool = False,
) -> ServiceProviders:
    """Add the AWS adapters to a provider registry.

    Args:
        providers: The registry to add to.
        sessions: The session factory every adapter uses. By default one is
            built from the resolved configuration when the first adapter needs it.
        replace: Allow overwriting adapters that are already registered. A
            test uses this to register the adapters again over stubbed clients.
    """
    shared = _Sessions(sessions)

    def bedrock_model(context: BuildContext) -> BedrockChatModelProvider:
        return BedrockChatModelProvider(shared.get(context))

    def secrets_manager(context: BuildContext) -> SecretsManagerProvider:
        options = context.selection.parse_options(SecretsManagerOptions)
        return SecretsManagerProvider(options, shared.get(context), context.clock)

    def firehose_audit(context: BuildContext) -> FirehoseAuditSink:
        options = context.selection.parse_options(FirehoseAuditOptions)
        return FirehoseAuditSink(options, shared.get(context))

    async def redshift_data(context: BuildContext) -> RedshiftDataSource:
        options = context.selection.parse_options(RedshiftDataOptions)
        source = RedshiftDataSource(context.instance, options, shared.get(context), context.clock)
        await source.start()
        return source

    async def s3_registry(context: BuildContext) -> S3RegistrySource:
        options = context.selection.parse_options(S3RegistryOptions)
        source = S3RegistrySource(options, shared.get(context))
        await source.start()
        return source

    def bedrock_guardrails(context: BuildContext) -> BedrockGuardrails:
        options = context.selection.parse_options(BedrockGuardrailsOptions)
        if (
            options.guardrail_version == DRAFT_VERSION
            and context.deployment_env is DeploymentEnv.PROD
        ):
            raise ConfigurationError(
                "the bedrock guardrails need a published guardrail version in production: "
                "the draft changes without a record of what was in force"
            )
        return BedrockGuardrails(options, shared.get(context))

    async def postgres_checkpoint(context: BuildContext) -> CheckpointBackend:
        try:
            # Imported here so that a service without this store needs no database driver.
            from ai_agent_lib_aws.checkpoint_postgres import (
                PostgresCheckpointBackend,
                PostgresCheckpointOptions,
            )
        except ImportError as exc:
            raise ConfigurationError(
                "the postgres checkpoint store needs a database driver; install it with "
                "'pip install \"ai-agent-lib-aws[postgres]\"'"
            ) from exc
        options = context.selection.parse_options(PostgresCheckpointOptions)
        backend = PostgresCheckpointBackend(options, shared.get(context))
        await backend.start()
        return backend

    providers.register(MODEL_PORT, "bedrock", bedrock_model, replace=replace)
    providers.register(Section.CHECKPOINT, "postgres", postgres_checkpoint, replace=replace)
    providers.register(Section.GUARDRAILS, "bedrock", bedrock_guardrails, replace=replace)
    providers.register(DATA_PORT, "redshift_data", redshift_data, replace=replace)
    providers.register(Section.AUDIT, "firehose", firehose_audit, replace=replace)
    providers.register(Section.REGISTRY, "s3_file", s3_registry, replace=replace)
    providers.register(Section.SECRETS, "secrets_manager", secrets_manager, replace=replace)
    return providers
