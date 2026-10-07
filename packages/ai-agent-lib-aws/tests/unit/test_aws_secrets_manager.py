"""The Secrets Manager provider: named secrets, read through the real client's shapes."""

from __future__ import annotations

import pytest
from botocore.stub import Stubber

from ai_agent_lib_aws.pack import register_aws_adapters
from ai_agent_lib_aws.secrets_manager import SecretsManagerOptions, SecretsManagerProvider
from ai_agent_lib_aws.testing import FakeSecretsManager, offline_sessions
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    ProviderSelection,
    Section,
    ServiceConfig,
    TransientError,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.testing import Fakes, FrozenClock


def provider(
    fake: FakeSecretsManager, clock: FrozenClock | None = None, **options: object
) -> SecretsManagerProvider:
    return SecretsManagerProvider(
        SecretsManagerOptions.model_validate(options),
        offline_sessions(),
        clock or FrozenClock(),
        client=fake,
    )


async def test_the_request_and_the_reply_have_the_shapes_of_the_real_service() -> None:
    sessions = offline_sessions()
    secrets = SecretsManagerProvider(
        SecretsManagerOptions(prefix="eap/agent/"), sessions, FrozenClock()
    )
    with Stubber(sessions.client("secretsmanager")) as service:
        service.add_response(
            "get_secret_value",
            {"Name": "eap/agent/entra_client_secret", "SecretString": "s3cret"},
            {"SecretId": "eap/agent/entra_client_secret"},
        )
        secret = await secrets.get_secret("entra_client_secret")
        service.assert_no_pending_responses()
    assert secret.get_secret_value() == "s3cret"
    assert "s3cret" not in repr(secret)


async def test_a_secret_is_kept_for_a_while_so_a_rotation_needs_no_restart() -> None:
    clock = FrozenClock()
    fake = FakeSecretsManager({"token": "first"})
    secrets = provider(fake, clock, cache_seconds=60)

    assert (await secrets.get_secret("token")).get_secret_value() == "first"
    fake.put("token", "rotated")
    clock.advance(59)
    assert (await secrets.get_secret("token")).get_secret_value() == "first"
    clock.advance(1)
    assert (await secrets.get_secret("token")).get_secret_value() == "rotated"
    assert fake.reads == ["token", "token"]


async def test_a_secret_that_is_missing_binary_or_badly_named_is_a_configuration_error() -> None:
    secrets = provider(FakeSecretsManager({"blob": b"\x00\x01"}))
    with pytest.raises(ConfigurationError, match="'absent' is not configured"):
        await secrets.get_secret("absent")
    with pytest.raises(ConfigurationError, match="'blob' is not text"):
        await secrets.get_secret("blob")
    for name in ("", "has space", "semi;colon", "x" * 201):
        with pytest.raises(ConfigurationError, match="cannot be the name of a secret"):
            await secrets.get_secret(name)


@pytest.mark.parametrize(
    ("code", "status", "expected", "said"),
    [
        ("ThrottlingException", 400, TransientError, "throttling"),
        ("AccessDeniedException", 400, ConfigurationError, "refused"),
        ("DecryptionFailure", 400, ConfigurationError, "could not be read"),
    ],
)
async def test_a_failed_read_never_repeats_what_the_service_said(
    code: str, status: int, expected: type[Exception], said: str
) -> None:
    sessions = offline_sessions(max_attempts=1)
    secrets = SecretsManagerProvider(SecretsManagerOptions(), sessions, FrozenClock())
    with Stubber(sessions.client("secretsmanager")) as service:
        service.add_client_error(
            "get_secret_value", code, "arn:aws:kms:key/1234 denied", http_status_code=status
        )
        with pytest.raises(expected, match=said) as caught:
            await secrets.get_secret("token")
    assert "arn:aws" not in str(caught.value)


def test_the_options_are_checked() -> None:
    with pytest.raises(ValueError, match="prefix"):
        SecretsManagerOptions(prefix="has space/")
    with pytest.raises(ValueError, match="cache_seconds"):
        SecretsManagerOptions(cache_seconds=-1)


async def test_the_container_builds_it_from_its_own_section() -> None:
    fakes = Fakes()
    sessions = offline_sessions()
    config = ServiceConfig.for_testing(
        sections={Section.SECRETS: ProviderSelection("secrets_manager", {"prefix": "eap/x/"})}
    )
    providers = register_aws_adapters(fakes.providers(), sessions=sessions)
    with Stubber(sessions.client("secretsmanager")) as service:
        service.add_response(
            "get_secret_value", {"SecretString": "v"}, {"SecretId": "eap/x/rates_token"}
        )
        async with ServiceContainer(config, providers, clock=fakes.clock) as services:
            assert isinstance(services.secrets, SecretsManagerProvider)
            assert (await services.secrets.get_secret("rates_token")).get_secret_value() == "v"
