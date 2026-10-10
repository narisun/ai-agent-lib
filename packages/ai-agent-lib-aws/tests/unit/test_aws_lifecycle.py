"""A reusable registry does not share container-owned AWS resources."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from botocore.stub import Stubber

from ai_agent_lib_aws.pack import register_aws_adapters
from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_aws.testing import offline_session
from ai_agent_lib_core.contracts import ExternalSettings, ProviderSelection, Section
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.testing import Fakes


async def test_one_session_per_container_with_a_reused_provider_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[AwsSessionFactory] = []
    closed: list[str | None] = []

    class TrackedSession(AwsSessionFactory):
        async def aclose(self) -> None:
            closed.append(self.region)
            await super().aclose()

    def create(external: ExternalSettings, *, tls_ca_bundle: Path | None) -> AwsSessionFactory:
        session = TrackedSession(
            region=external.aws_region,
            bedrock_ca_bundle=tls_ca_bundle,
            session_factory=offline_session,
        )
        built.append(session)
        return session

    monkeypatch.setattr(AwsSessionFactory, "from_settings", staticmethod(create))
    fakes = Fakes()
    providers = register_aws_adapters(fakes.providers())
    config = fakes.config().with_section(Section.SECRETS, ProviderSelection("secrets_manager"))
    config = config.with_section(Section.AUDIT, ProviderSelection("firehose", {"stream": "audit"}))
    for region in ("us-east-1", "eu-west-1"):
        async with ServiceContainer(
            replace(config, external=ExternalSettings(aws_region=region)), providers
        ) as services:
            session = built[-1]
            with Stubber(session.client("secretsmanager")) as stub:
                stub.add_response(
                    "get_secret_value", {"SecretString": region}, {"SecretId": "region"}
                )
                assert (await services.secrets.get_secret("region")).get_secret_value() == region
    assert [session.region for session in built] == ["us-east-1", "eu-west-1"]
    assert closed == ["us-east-1", "eu-west-1"]


async def test_explicitly_injected_session_is_owned_by_its_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = AwsSessionFactory(region="eu-west-1", session_factory=offline_session)
    closes: list[bool] = []

    async def close() -> None:
        closes.append(True)

    monkeypatch.setattr(session, "aclose", close)
    fakes = Fakes()
    providers = register_aws_adapters(fakes.providers(), sessions=session)
    config = fakes.config().with_section(Section.SECRETS, ProviderSelection("secrets_manager"))
    async with ServiceContainer(config, providers):
        pass
    assert closes == []
