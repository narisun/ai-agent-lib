"""The AWS provider pack: which AWS adapters exist and how each is built."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from agentcraft2_aws.access import (
    agentcore_identity_access,
    agentcore_memory_access,
    bedrock_guardrails_access,
    bedrock_model_access,
    firehose_audit_access,
    postgres_checkpoint_access,
    redshift_data_access,
    s3_registry_access,
    secrets_manager_access,
)
from agentcraft2_aws.audit_firehose import FirehoseAuditOptions, FirehoseAuditSink
from agentcraft2_aws.checkpoint_options import (
    AgentCoreMemoryCheckpointOptions,
    PostgresCheckpointOptions,
)
from agentcraft2_aws.data_redshift import RedshiftDataOptions, RedshiftDataSource
from agentcraft2_aws.guardrails_bedrock import (
    DRAFT_VERSION,
    BedrockGuardrails,
    BedrockGuardrailsOptions,
)
from agentcraft2_aws.identity_agentcore import (
    AgentCoreRuntimeIdentity,
    AgentCoreRuntimeIdentityOptions,
    ExchangingAgentCoreRuntimeIdentity,
)
from agentcraft2_aws.model_bedrock import BedrockChatModelProvider
from agentcraft2_aws.registry_s3 import S3RegistryOptions, S3RegistrySource
from agentcraft2_aws.secrets_manager import SecretsManagerOptions, SecretsManagerProvider
from agentcraft2_aws.session import AwsSessionFactory
from agentcraft2_aws.token_exchange_agentcore import AgentCoreTokenExchanger
from agentcraft2_core.contracts import (
    CheckpointBackend,
    ConfigurationError,
    DeploymentEnv,
    NoOptions,
    Profile,
)
from agentcraft2_core.kit import (
    AUDIT,
    CHECKPOINT,
    DATA,
    GUARDRAILS,
    IDENTITY,
    MODEL,
    REGISTRY,
    SECRETS,
    BuildContext,
    ConnectorContext,
    ServiceProviders,
)

if TYPE_CHECKING:
    from agentcraft2_aws.checkpoint_agentcore_memory import AgentCoreMemoryCheckpointBackend
    from agentcraft2_aws.checkpoint_postgres import PostgresCheckpointBackend
    from agentcraft2_aws.mcp_agentcore import AgentCoreMcpConnector

__all__ = ["register_aws_adapters"]


def _agentcore_mcp_connector() -> type[AgentCoreMcpConnector]:
    # Imported here so that a service which calls no AgentCore runtime needs no MCP SDK.
    try:
        from agentcraft2_aws.mcp_agentcore import AgentCoreMcpConnector
    except ImportError as exc:
        raise ConfigurationError(
            "calling an MCP server on an AgentCore runtime needs the MCP SDK; install it with "
            "'pip install \"agentcraft2-aws[agentcore]\"'"
        ) from exc
    return AgentCoreMcpConnector


def _postgres_backend() -> type[PostgresCheckpointBackend]:
    # Imported here so that a service without this store needs no database driver.
    try:
        from agentcraft2_aws.checkpoint_postgres import PostgresCheckpointBackend
    except ImportError as exc:
        raise ConfigurationError(
            "the postgres checkpoint store needs a database driver; install it with "
            "'pip install \"agentcraft2-aws[postgres]\"'"
        ) from exc
    return PostgresCheckpointBackend


def _agentcore_memory_backend() -> type[AgentCoreMemoryCheckpointBackend]:
    # Imported here so that a service without this store needs no AgentCore checkpointer.
    try:
        from agentcraft2_aws.checkpoint_agentcore_memory import AgentCoreMemoryCheckpointBackend
    except ImportError as exc:
        raise ConfigurationError(
            "the agentcore_memory checkpoint store needs the AgentCore checkpointer; install "
            "it with 'pip install \"agentcraft2-aws[agentcore]\"'"
        ) from exc
    return AgentCoreMemoryCheckpointBackend


def _require_deployed(context: BuildContext) -> None:
    """Refuse the runtime identity in local development: the inverse of ``local_only``."""
    if context.deployment_env is DeploymentEnv.LOCAL:
        raise ConfigurationError(
            "the agentcore_runtime identity trusts the AgentCore runtime to have authenticated "
            "the caller, and there is no runtime in local development",
            expected="a deployed environment: dev or prod",
            actual="the local deployment environment",
            fix="name the deployment environment as dev or prod in the service's configuration; "
            "on a developer's machine select identity=static",
        )


class _Sessions:
    """Resolve a container-owned session factory or borrow a caller-owned one.

    The registry may outlive several containers, so default sessions are cached
    in ``BuildContext.resources``, never on this registry-held helper.
    """

    def __init__(self, sessions: AwsSessionFactory | None) -> None:
        self._sessions = sessions

    def get(self, context: BuildContext | ConnectorContext) -> AwsSessionFactory:
        if self._sessions is not None:
            return self._sessions  # explicitly injected: the caller owns it
        return context.resources.shared(
            self,
            lambda: AwsSessionFactory.from_settings(
                context.external, tls_ca_bundle=context.tls_ca_bundle
            ),
        )


def _agentcore_connector_factory(
    shared: _Sessions,
) -> Callable[[ConnectorContext], AgentCoreMcpConnector]:
    """Return the factory for the connector that calls MCP servers on AgentCore runtimes."""

    def build(context: ConnectorContext) -> AgentCoreMcpConnector:
        return _agentcore_mcp_connector()(
            shared.get(context),
            timeout_seconds=context.timeout_seconds,
            proxy=context.external.https_proxy,
            no_proxy=context.external.no_proxy,
            ca_file=context.external.aws_ca_bundle,
        )

    return build


def register_aws_adapters(
    providers: ServiceProviders,
    *,
    sessions: AwsSessionFactory | None = None,
    replace: bool = False,
) -> ServiceProviders:
    """Add the AWS adapters to a provider registry.

    Args:
        providers: The registry to add to.
        sessions: Optional shared session factory owned and closed by the caller.
            By default each container builds its own on first use and closes it
            after its adapters, even when containers reuse the same registry.
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
        options = context.selection.parse_options(PostgresCheckpointOptions)
        backend = _postgres_backend()(options, shared.get(context))
        await backend.start()
        return backend

    async def agentcore_memory_checkpoint(context: BuildContext) -> CheckpointBackend:
        options = context.selection.parse_options(AgentCoreMemoryCheckpointOptions)
        backend = _agentcore_memory_backend()(options, shared.get(context))
        await backend.start()
        return backend

    def agentcore_runtime_identity(context: BuildContext) -> AgentCoreRuntimeIdentity:
        _require_deployed(context)
        options = context.selection.parse_options(AgentCoreRuntimeIdentityOptions)
        if options.exchange is None:
            return AgentCoreRuntimeIdentity(options)
        exchanger = AgentCoreTokenExchanger(options.exchange, shared.get(context), context.clock)
        return ExchangingAgentCoreRuntimeIdentity(options, exchanger)

    providers.register(
        IDENTITY,
        "agentcore_runtime",
        agentcore_runtime_identity,
        options=AgentCoreRuntimeIdentityOptions,
        access=agentcore_identity_access,
        replace=replace,
        dependencies=(),
        # It trusts a header that only AgentCore Runtime's perimeter sets honestly.
        profiles=(Profile.AGENTCORE,),
    )
    providers.register(
        CHECKPOINT,
        "agentcore_memory",
        agentcore_memory_checkpoint,
        options=AgentCoreMemoryCheckpointOptions,
        access=agentcore_memory_access,
        extra="agentcore",
        replace=replace,
        dependencies=(),
    )
    providers.register(
        MODEL,
        "bedrock",
        bedrock_model,
        options=NoOptions,
        access=bedrock_model_access,
        extra="bedrock",
        replace=replace,
        dependencies=(),
    )
    providers.register(
        CHECKPOINT,
        "postgres",
        postgres_checkpoint,
        options=PostgresCheckpointOptions,
        access=postgres_checkpoint_access,
        extra="postgres",
        replace=replace,
        dependencies=(),
    )
    providers.register(
        GUARDRAILS,
        "bedrock",
        bedrock_guardrails,
        options=BedrockGuardrailsOptions,
        access=bedrock_guardrails_access,
        replace=replace,
        dependencies=(),
    )
    providers.register(
        DATA,
        "redshift_data",
        redshift_data,
        options=RedshiftDataOptions,
        access=redshift_data_access,
        replace=replace,
        dependencies=(),
    )
    providers.register(
        AUDIT,
        "firehose",
        firehose_audit,
        options=FirehoseAuditOptions,
        access=firehose_audit_access,
        replace=replace,
        dependencies=(),
    )
    providers.register(
        REGISTRY,
        "s3_file",
        s3_registry,
        options=S3RegistryOptions,
        access=s3_registry_access,
        replace=replace,
        dependencies=(),
    )
    providers.register(
        SECRETS,
        "secrets_manager",
        secrets_manager,
        options=SecretsManagerOptions,
        access=secrets_manager_access,
        replace=replace,
        dependencies=(),
    )

    providers.register_mcp_connector(
        "agentcore", _agentcore_connector_factory(shared), replace=replace
    )
    return providers
