"""The S3 registry: two objects read once, a missing one stops startup."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from botocore import exceptions as aws
from botocore.response import StreamingBody
from botocore.stub import Stubber

from ai_agent_lib_aws.pack import register_aws_adapters
from ai_agent_lib_aws.registry_s3 import S3RegistryOptions, S3RegistrySource
from ai_agent_lib_aws.testing import FakeS3, client_error, offline_sessions
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    ProviderSelection,
    Section,
    ServiceConfig,
    TransientError,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.kit import agents_document, content_revision, tools_document
from ai_agent_lib_core.testing import Fakes
from ai_agent_lib_core.testing.contracts import RegistrySourceContract

BUCKET = "eap-registry"
AGENTS = json.dumps(agents_document(RegistrySourceContract.agents))
TOOLS = json.dumps(tools_document(RegistrySourceContract.servers))
OPTIONS = S3RegistryOptions(bucket=BUCKET, agents_key="agents.json", tools_key="tools.json")


def storage(agents: str | None = AGENTS, tools: str | None = TOOLS, **settings: Any) -> FakeS3:
    held = FakeS3(**settings)
    held.put(BUCKET, "unrelated.txt", "keeps the bucket in existence")
    if agents is not None:
        held.put(BUCKET, "agents.json", agents)
    if tools is not None:
        held.put(BUCKET, "tools.json", tools)
    return held


def source(held: FakeS3, options: S3RegistryOptions = OPTIONS) -> S3RegistrySource:
    return S3RegistrySource(options, offline_sessions(), client=held)


def body(text: str) -> StreamingBody:
    data = text.encode("utf-8")
    return StreamingBody(io.BytesIO(data), len(data))


async def test_the_requests_and_the_replies_have_the_shapes_of_the_real_service() -> None:
    sessions = offline_sessions()
    options = OPTIONS.model_copy(update={"expected_bucket_owner": "111122223333"})
    registry = S3RegistrySource(options, sessions)
    with Stubber(sessions.client("s3")) as service:
        service.add_response(
            "get_object",
            {
                "Body": body(TOOLS),
                "ContentLength": len(TOOLS),
                "VersionId": "3HL4kqtJlcpXroDTDmJ.r",
            },
            {"Bucket": BUCKET, "Key": "tools.json", "ExpectedBucketOwner": "111122223333"},
        )
        service.add_response(
            "get_object",
            {"Body": body(AGENTS), "ContentLength": len(AGENTS), "VersionId": "null"},
            {"Bucket": BUCKET, "Key": "agents.json", "ExpectedBucketOwner": "111122223333"},
        )
        await registry.start()
        service.assert_no_pending_responses()
    await registry.validate()
    assert registry.tools.revision == "s3-version:3HL4kqtJlcpXroDTDmJ.r"
    assert registry.agents.revision == content_revision(AGENTS.encode("utf-8"))
    assert registry.agents.get("accounts-agent") == RegistrySourceContract.agents[0]


async def test_a_new_version_of_a_document_is_a_new_revision() -> None:
    held = storage()
    first = source(held)
    await first.start()
    held.put(BUCKET, "tools.json", TOOLS)
    second = source(held)
    await second.start()
    assert (first.tools.revision, second.tools.revision) == ("s3-version:v1", "s3-version:v2")
    assert first.agents.revision == second.agents.revision == "s3-version:v1"


@pytest.mark.parametrize(("agents", "tools"), [(None, TOOLS), (AGENTS, None)])
async def test_a_missing_document_stops_startup(agents: str | None, tools: str | None) -> None:
    registry = source(storage(agents, tools))
    with pytest.raises(ConfigurationError, match="does not exist") as caught:
        await registry.start()
    assert f"s3://{BUCKET}/" in str(caught.value)
    with pytest.raises(ConfigurationError, match="not started"):
        await registry.validate()
    with pytest.raises(RuntimeError, match="not started"):
        _ = registry.agents


async def test_a_missing_bucket_stops_startup() -> None:
    with pytest.raises(ConfigurationError, match="does not exist"):
        await source(FakeS3()).start()


async def test_a_bucket_in_another_account_is_refused() -> None:
    options = OPTIONS.model_copy(update={"expected_bucket_owner": "999999999999"})
    held = storage()
    with pytest.raises(ConfigurationError):
        await source(held, options).start()
    assert held.reads[0]["ExpectedBucketOwner"] == "999999999999"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (client_error("AccessDenied", "GetObject", status=403), ConfigurationError),
        (client_error("InvalidObjectState", "GetObject", status=403), ConfigurationError),
        (client_error("SlowDown", "GetObject", status=503), TransientError),
        (aws.EndpointConnectionError(endpoint_url="https://s3"), TransientError),
        (aws.ParamValidationError(report="bad"), ConfigurationError),
    ],
)
async def test_an_object_that_cannot_be_read_stops_startup(
    failure: BaseException, expected: type[Exception]
) -> None:
    held = storage()
    held.fail_with = failure
    with pytest.raises(expected) as caught:
        await source(held).start()
    assert "the service's own message" not in str(caught.value)


@pytest.mark.parametrize(
    ("agents", "tools", "message"),
    [
        ("{not json", TOOLS, "not valid JSON"),
        (AGENTS, json.dumps({"schema": "something/else"}), "tool registry"),
        (AGENTS, json.dumps(tools_document(())), "the tool registry lacks"),
        (AGENTS, "x" * (5 * 1024 * 1024 + 1), "larger than"),
    ],
)
async def test_an_invalid_document_stops_startup(agents: str, tools: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        await source(storage(agents, tools)).start()


async def test_a_document_that_is_not_text_stops_startup() -> None:
    held = storage()
    held.put(BUCKET, "tools.json", b"\xff\xfe\x00")
    with pytest.raises(ConfigurationError, match="could not be read as text"):
        await source(held).start()


async def test_the_key_must_say_whether_the_document_is_yaml_or_json() -> None:
    held = storage()
    held.put(BUCKET, "tools", TOOLS)
    options = OPTIONS.model_copy(update={"tools_key": "tools"})
    with pytest.raises(ConfigurationError, match=r"\.yaml, \.yml or \.json"):
        await source(held, options).start()


async def test_the_registry_is_read_once() -> None:
    held = storage()
    registry = source(held)
    await registry.start()
    with pytest.raises(RuntimeError, match="already started"):
        await registry.start()
    assert [read["Key"] for read in held.reads] == ["tools.json", "agents.json"]
    assert BUCKET in repr(registry)


@pytest.mark.parametrize(
    "settings",
    [
        {},
        {"bucket": "Has_Capitals"},
        {"bucket": BUCKET, "agents_key": ""},
        {"bucket": BUCKET, "expected_bucket_owner": "1234"},
        {"bucket": BUCKET, "region": "eu-west-1"},
    ],
)
def test_options_are_checked(settings: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="S3RegistryOptions"):
        S3RegistryOptions.model_validate(settings)


async def test_a_service_gets_the_registry_from_configuration() -> None:
    fakes = Fakes()
    sessions = offline_sessions()
    config = ServiceConfig.for_testing(
        sections={
            Section.REGISTRY: ProviderSelection(
                "s3_file",
                {"bucket": BUCKET, "agents_key": "agents.json", "tools_key": "tools.json"},
            )
        }
    )
    providers = register_aws_adapters(fakes.providers(), sessions=sessions)
    with Stubber(sessions.client("s3")) as service:
        service.add_response("get_object", {"Body": body(TOOLS), "VersionId": "t1"})
        service.add_response("get_object", {"Body": body(AGENTS), "VersionId": "a1"})
        async with ServiceContainer(config, providers, clock=fakes.clock) as services:
            assert isinstance(services.registry, S3RegistrySource)
            assert services.registry.agents.revision == "s3-version:a1"
            assert services.registry.tools.get("accounts") is not None
