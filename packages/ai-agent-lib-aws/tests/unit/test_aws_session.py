"""The AWS session factory: configuration in, clients and translated errors out."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from botocore import exceptions as aws
from botocore.stub import Stubber

from ai_agent_lib_aws.session import AwsSessionFactory, classify_aws_error
from ai_agent_lib_aws.testing import offline_session, offline_sessions
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    CredentialsExpiredError,
    ExternalSettings,
    TransientError,
)


def factory(**settings: Any) -> AwsSessionFactory:
    return offline_sessions(**settings)


def client_error(code: str, status: int = 400, message: str = "the prompt was: secret") -> Any:
    return aws.ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "Converse",
    )


# ------------------------------------------------------------------- clients


def test_a_client_is_built_once_for_the_configured_region() -> None:
    sessions = factory()
    first = sessions.client("secretsmanager")
    assert sessions.client("secretsmanager") is first
    assert first.meta.region_name == "eu-west-1"
    assert sessions.region == "eu-west-1"


def test_the_enterprise_ca_file_reaches_the_bedrock_clients_and_no_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The host's own trust settings would otherwise decide what "default" means.
    for variable in ("AWS_CA_BUNDLE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-config"))  # no ca_bundle there
    enterprise, aws_wide = tmp_path / "enterprise.pem", tmp_path / "aws.pem"
    only_enterprise = factory(bedrock_ca_bundle=enterprise)
    assert only_enterprise.client("bedrock-runtime")._endpoint.http_session._verify == str(
        enterprise
    )
    assert only_enterprise.client("bedrock")._endpoint.http_session._verify == str(enterprise)
    # Every other client verifies against the default trust store.
    assert only_enterprise.client("secretsmanager")._endpoint.http_session._verify is True

    both = factory(bedrock_ca_bundle=enterprise, ca_bundle=aws_wide)
    assert both.client("bedrock-runtime")._endpoint.http_session._verify == str(enterprise)
    assert both.client("firehose")._endpoint.http_session._verify == str(aws_wide)


def test_the_proxy_is_used_unless_the_host_is_listed_as_direct() -> None:
    proxied = factory(proxy="http://proxy.corp:8080")
    assert proxied.client("bedrock-runtime").meta.config.proxies == {
        "https": "http://proxy.corp:8080"
    }
    direct = factory(proxy="http://proxy.corp:8080", no_proxy="localhost, .amazonaws.com")
    assert not direct.client("bedrock-runtime").meta.config.proxies
    partly = factory(proxy="http://proxy.corp:8080", no_proxy="firehose.eu-west-1.amazonaws.com")
    assert not partly.client("firehose").meta.config.proxies
    assert partly.client("s3").meta.config.proxies


async def test_proxy_discovery_and_cached_clients_are_all_closed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = factory(proxy="http://proxy.corp:8080")
    built: list[Any] = []
    closed: list[Any] = []
    original = sessions._create

    def create(service: str, attempts: int, *, proxied: bool) -> Any:
        client = original(service, attempts, proxied=proxied)
        close = client.close

        def record_close() -> None:
            closed.append(client)
            close()

        monkeypatch.setattr(client, "close", record_close)
        built.append(client)
        return client

    monkeypatch.setattr(sessions, "_create", create)
    sessions.client("bedrock-runtime")
    sessions.client("s3")
    assert closed == built[::2]  # only the temporary endpoint-discovery clients
    await sessions.aclose()
    await sessions.aclose()
    assert len(closed) == len(built) == 4
    assert {id(client) for client in closed} == {id(client) for client in built}


async def test_a_client_close_failure_does_not_leak_remaining_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = factory()
    first, second = sessions.client("s3"), sessions.client("secretsmanager")
    closed: list[bool] = []

    def fail() -> None:
        raise RuntimeError("close failed")

    monkeypatch.setattr(first, "close", fail)
    monkeypatch.setattr(second, "close", lambda: closed.append(True))
    with pytest.raises(ExceptionGroup, match="closing AWS clients"):
        await sessions.aclose()
    await sessions.aclose()
    assert closed == [True]


def test_timeouts_and_bounded_retries_are_set_on_every_client() -> None:
    config = factory(connect_timeout=2, read_timeout=9, max_attempts=1).client("s3").meta.config
    assert (config.connect_timeout, config.read_timeout) == (2, 9)
    assert config.retries["total_max_attempts"] == 1


def test_the_shared_settings_supply_everything() -> None:
    external = ExternalSettings(
        aws_region="us-east-2", aws_ca_bundle=Path("/etc/aws.pem"), https_proxy="http://p:1"
    )
    sessions = AwsSessionFactory.from_settings(
        external, tls_ca_bundle=Path("/etc/corp.pem"), session_factory=offline_session
    )
    assert sessions.region == "us-east-2"
    assert sessions.client("bedrock-runtime")._endpoint.http_session._verify == str(
        Path("/etc/corp.pem")
    )
    assert sessions.client("s3")._endpoint.http_session._verify == str(Path("/etc/aws.pem"))
    assert sessions.sign_in is None
    assert "http://p:1" not in repr(sessions)


def test_a_profile_that_does_not_exist_or_a_missing_region_stops_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    config = tmp_path / "aws-config"
    config.write_text("")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    with pytest.raises(ConfigurationError, match="profile or region is not valid"):
        AwsSessionFactory(profile="no-such-profile", region="eu-west-1")

    def no_region(**arguments: Any) -> Any:
        import boto3

        session = boto3.Session(aws_access_key_id="a", aws_secret_access_key="b")
        session._session.set_config_variable("region", None)
        return session

    with pytest.raises(ConfigurationError, match="profile or region is not valid"):
        AwsSessionFactory(session_factory=no_region).client("secretsmanager")


# -------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    "error",
    [
        aws.UnauthorizedSSOTokenError(),
        aws.SSOTokenLoadError(error_msg="the cache file is missing"),
        aws.TokenRetrievalError(provider="sso", error_msg="expired"),
        client_error("ExpiredTokenException", 403),
    ],
)
def test_an_expired_sign_in_names_the_command_that_renews_it(error: Exception) -> None:
    mapped = classify_aws_error(error, what="bedrock", sign_in="aws sso login --profile dev-sso")
    assert isinstance(mapped, CredentialsExpiredError)
    assert mapped.fix_command == "aws sso login --profile dev-sso"
    assert "aws sso login --profile dev-sso" in str(mapped)
    assert not mapped.retryable


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (client_error("ThrottlingException", 429), TransientError),
        (client_error("ModelTimeoutException", 408), TransientError),
        (client_error("SomethingNew", 503), TransientError),
        (client_error("AccessDeniedException", 403), ConfigurationError),
        (client_error("UnrecognizedClientException", 403), ConfigurationError),
        (aws.NoCredentialsError(), ConfigurationError),
        (aws.EndpointConnectionError(endpoint_url="https://x"), TransientError),
        (aws.ReadTimeoutError(endpoint_url="https://x"), TransientError),
        (aws.SSLError(endpoint_url="https://x", error="self-signed"), ConfigurationError),
    ],
)
def test_sdk_errors_map_to_the_taxonomy_without_the_sdks_message(
    error: Exception, expected: type[Exception]
) -> None:
    mapped = classify_aws_error(error, what="the bedrock model provider")
    assert type(mapped) is expected
    assert "secret" not in str(mapped)
    assert "the bedrock model provider" in str(mapped)


def test_errors_that_are_about_the_request_are_left_alone() -> None:
    assert classify_aws_error(client_error("ValidationException"), what="x") is None
    assert classify_aws_error(ValueError("not from the SDK"), what="x") is None


# --------------------------------------------------------------------- calls


async def test_a_call_runs_off_the_event_loop_and_its_errors_are_translated() -> None:
    sessions = factory()
    client = sessions.client("secretsmanager")
    with Stubber(client) as stub:
        stub.add_response("get_secret_value", {"SecretString": "v"}, {"SecretId": "a"})
        stub.add_client_error("get_secret_value", "ThrottlingException", http_status_code=400)
        stub.add_client_error("get_secret_value", "ResourceNotFoundException")
        reply = await sessions.invoke("secrets", client.get_secret_value, SecretId="a")
        assert reply["SecretString"] == "v"
        with pytest.raises(TransientError, match="secrets"):
            await sessions.invoke("secrets", client.get_secret_value, SecretId="a")
        # An error the taxonomy has no place for is the adapter's to interpret.
        with pytest.raises(aws.ClientError):
            await sessions.invoke("secrets", client.get_secret_value, SecretId="a")
