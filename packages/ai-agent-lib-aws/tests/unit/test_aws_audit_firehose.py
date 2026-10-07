"""The Firehose audit sink: one acknowledged put per record, or the call fails."""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore import exceptions as aws
from botocore.stub import Stubber

from ai_agent_lib_aws.audit_firehose import FirehoseAuditOptions, FirehoseAuditSink
from ai_agent_lib_aws.pack import register_aws_adapters
from ai_agent_lib_aws.testing import FakeFirehose, client_error, offline_sessions
from ai_agent_lib_core import Principal, RequestContext, bind_request_context
from ai_agent_lib_core.adapters import FakeChatModelProvider
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    IntegrityError,
    ProviderSelection,
    Section,
    ServiceConfig,
)
from ai_agent_lib_core.di import ServiceContainer
from ai_agent_lib_core.testing import Fakes
from ai_agent_lib_core.testing.contracts import make_audit_record


def sink(firehose: FakeFirehose, stream: str = "audit") -> FirehoseAuditSink:
    return FirehoseAuditSink(
        FirehoseAuditOptions(stream=stream), offline_sessions(), client=firehose
    )


async def test_the_request_and_the_reply_have_the_shapes_of_the_real_service() -> None:
    sessions = offline_sessions()
    audit = FirehoseAuditSink(FirehoseAuditOptions(stream="eap-audit"), sessions)
    record = make_audit_record()
    line = json.dumps(record.to_dict(), separators=(",", ":"), ensure_ascii=False) + "\n"
    with Stubber(sessions.client("firehose")) as service:
        service.add_response(
            "put_record",
            {"RecordId": "abc", "Encrypted": True},
            {"DeliveryStreamName": "eap-audit", "Record": {"Data": line.encode("utf-8")}},
        )
        service.add_response(
            "describe_delivery_stream",
            {
                "DeliveryStreamDescription": {
                    "DeliveryStreamName": "eap-audit",
                    "DeliveryStreamARN": "arn:aws:firehose:eu-west-1:1:deliverystream/eap-audit",
                    "DeliveryStreamStatus": "ACTIVE",
                    "DeliveryStreamType": "DirectPut",
                    "VersionId": "1",
                    "Destinations": [],
                    "HasMoreDestinations": False,
                }
            },
            {"DeliveryStreamName": "eap-audit"},
        )
        await audit.write(record)
        await audit.validate()
        service.assert_no_pending_responses()


@pytest.mark.parametrize(
    "failure",
    [
        client_error("ServiceUnavailableException", "PutRecord", status=503),
        client_error("AccessDeniedException", "PutRecord", status=403),
        client_error("ResourceNotFoundException", "PutRecord"),
        aws.ReadTimeoutError(endpoint_url="https://firehose"),
        aws.UnauthorizedSSOTokenError(),
    ],
)
async def test_every_way_a_put_can_fail_is_an_integrity_error(failure: BaseException) -> None:
    firehose = FakeFirehose()
    firehose.fail_with = failure
    with pytest.raises(IntegrityError, match="was not accepted") as caught:
        await sink(firehose).write(make_audit_record())
    assert not caught.value.retryable
    assert firehose.records["audit"] == []


async def test_a_put_that_is_not_acknowledged_is_an_integrity_error() -> None:
    firehose = FakeFirehose()
    firehose.acknowledge = False
    with pytest.raises(IntegrityError, match="did not acknowledge"):
        await sink(firehose).write(make_audit_record())


async def test_validation_needs_an_active_stream() -> None:
    firehose = FakeFirehose()
    await sink(firehose).validate()
    with pytest.raises(ConfigurationError, match="ResourceNotFoundException"):
        await sink(firehose, stream="no-such-stream").validate()
    firehose.status = "CREATING"
    with pytest.raises(ConfigurationError, match="not active"):
        await sink(firehose).validate()
    firehose.fail_with = aws.EndpointConnectionError(endpoint_url="https://firehose")
    with pytest.raises(ConfigurationError, match="could not be reached"):
        await sink(firehose).validate()


def test_the_stream_name_is_required_and_checked() -> None:
    with pytest.raises(ValueError, match="stream"):
        FirehoseAuditOptions.model_validate({})
    with pytest.raises(ValueError, match="stream"):
        FirehoseAuditOptions(stream="has space")


async def test_a_governed_call_fails_when_its_record_cannot_be_delivered() -> None:
    """The fail-closed rule, end to end: no evidence, no answer."""
    fakes = Fakes(model=FakeChatModelProvider(["the answer"]))
    sessions = offline_sessions(max_attempts=1)
    config = ServiceConfig.for_testing(
        sections={Section.AUDIT: ProviderSelection("firehose", {"stream": "eap-audit"})}
    )
    providers = register_aws_adapters(fakes.providers(), sessions=sessions)
    context = RequestContext(
        principal=Principal(subject="u-1", tenant="t-9"),
        application="accounts-agent",
        request_id="r-1",
        thread_id="th-1",
    )
    accepted: dict[str, Any] = {"RecordId": "1"}
    with Stubber(sessions.client("firehose")) as service:
        service.add_response("put_record", accepted)
        service.add_client_error("put_record", "ServiceUnavailableException", http_status_code=503)
        async with ServiceContainer(config, providers, clock=fakes.clock) as services:
            assert isinstance(services.audit, FirehoseAuditSink)
            with bind_request_context(context):
                await services.model().ainvoke("first question")
                with pytest.raises(IntegrityError):
                    await services.model().ainvoke("second question")
