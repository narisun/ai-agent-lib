"""An audit sink that delivers each record to Amazon Data Firehose."""

from __future__ import annotations

import json
from typing import Any

from botocore import exceptions as aws
from pydantic import Field

from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import (
    AgentLibError,
    AuditOptions,
    AuditRecord,
    ConfigurationError,
    IntegrityError,
    describe,
)

__all__ = ["FirehoseAuditOptions", "FirehoseAuditSink"]

_WHAT = "the firehose audit sink"
_MAX_RECORD_BYTES = 1_000 * 1024  # the service's limit for one record
_ACTIVE = "ACTIVE"


class FirehoseAuditOptions(AuditOptions):
    """Options of the ``firehose`` audit sink.

    Attributes:
        stream: The name of the Firehose stream that receives the records.
    """

    stream: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")


class FirehoseAuditSink:
    """Sends one audit record per call and waits for the service to accept it.

    ``write`` returns only after Firehose has acknowledged the record with an
    ID. Anything else, whether an error, a timeout or a reply without an ID,
    raises ``IntegrityError``, and the call the record describes fails with it.

    Each record is one line of JSON. Delivery is at least once: the SDK may
    send a record again after a timeout, and ``record_id`` tells the copies apart.

    Args:
        options: The stream to write to.
        sessions: Builds the client and makes its calls.
        client: A Firehose client to use instead of building one.
    """

    def __init__(
        self, options: FirehoseAuditOptions, sessions: AwsSessionFactory, *, client: Any = None
    ) -> None:
        self._stream = options.stream
        self._sessions = sessions
        self._client = client if client is not None else sessions.client("firehose")

    def __repr__(self) -> str:
        return f"FirehoseAuditSink(stream={self._stream!r})"

    async def write(self, record: AuditRecord) -> None:
        """Deliver ``record`` and wait for the acknowledgement, or raise ``IntegrityError``."""
        line = json.dumps(record.to_dict(), separators=(",", ":"), ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        if len(data) > _MAX_RECORD_BYTES:
            raise IntegrityError(f"{_WHAT}: the audit record is larger than the service accepts")
        try:
            reply = await self._sessions.invoke(
                _WHAT,
                self._client.put_record,
                DeliveryStreamName=self._stream,
                Record={"Data": data},
            )
        except (AgentLibError, aws.BotoCoreError, aws.ClientError, OSError) as exc:
            raise IntegrityError(
                f"{_WHAT}: the audit record was not accepted by stream {self._stream!r} "
                f"({describe(exc)})"
            ) from exc
        record_id = reply.get("RecordId") if isinstance(reply, dict) else None
        if not isinstance(record_id, str) or not record_id:
            raise IntegrityError(
                f"{_WHAT}: stream {self._stream!r} did not acknowledge the audit record"
            )

    async def validate(self) -> None:
        """Check that the stream exists and is accepting records.

        Raises:
            ConfigurationError: If it does not exist, is not active or cannot be described.
        """
        try:
            reply = await self._sessions.invoke(
                _WHAT, self._client.describe_delivery_stream, DeliveryStreamName=self._stream
            )
        except aws.ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "error")
            raise ConfigurationError(
                f"{_WHAT}: stream {self._stream!r} cannot be used ({code})"
            ) from None
        except AgentLibError as exc:
            raise ConfigurationError.from_error(exc) from exc
        status = reply.get("DeliveryStreamDescription", {}).get("DeliveryStreamStatus")
        if status != _ACTIVE:
            raise ConfigurationError(f"{_WHAT}: stream {self._stream!r} is not active")
