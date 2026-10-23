"""Serialize model exchanges at the framework boundary for opt-in diagnostics."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pydantic import SecretBytes, SecretStr

from ai_agent_lib_core.contracts import Clock, IdGenerator, ModelTraceSink
from ai_agent_lib_core.contracts.redaction import REDACTED, redact
from ai_agent_lib_core.pipeline import ModelCall

_LOG = logging.getLogger(__name__)
_CREDENTIAL_KEYS = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "api_key",
        "password",
        "passwd",
        "secret",
        "client_secret",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "credentials",
        "credential",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
    }
)


def diagnostic_content(value: object) -> object:
    """Keep JSON structure and full text, masking recognizable credentials.

    Unknown objects are identified by type, never by a repr that could expose a
    client or credential. Redaction is best effort; conversation data remains.
    """
    if isinstance(value, SecretStr | SecretBytes):
        return REDACTED
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "<non-finite float>"
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED
            if str(key).lower().replace("-", "_") in _CREDENTIAL_KEYS
            else diagnostic_content(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [diagnostic_content(item) for item in value]
    return f"<{type(value).__name__}>"


@dataclass(slots=True)
class TraceCall:
    """Correlation for one model call and its individual provider attempts."""

    call_id: str
    attempt: int = 0


class ModelTrace:
    """Write model diagnostics through an injected sink, independently of audit.

    Serialization and sink errors disable this recorder and emit a metadata-only
    warning. They do not fail or retry a model operation. Cancellation propagates.
    """

    def __init__(self, sink: ModelTraceSink, clock: Clock, ids: IdGenerator) -> None:
        self._sink = sink
        self._clock = clock
        self._ids = ids
        self._failed = False

    def new_call(self) -> TraceCall:
        """Allocate correlation once, so retries share the same call identifier."""
        return TraceCall(self._ids.new_id())

    async def write(
        self,
        event: str,
        call: ModelCall,
        trace: TraceCall,
        payload: Callable[[], Mapping[str, object]],
    ) -> None:
        """Write one credential-masked event without changing the model's outcome."""
        if self._failed or call.context is None:
            return
        context = call.context
        try:
            record = {
                "schema": "agentcraft2.model-trace/v1",
                "time": self._clock.now().isoformat(),
                "event": event,
                "call_id": trace.call_id,
                "attempt": trace.attempt,
                "request_id": context.request_id,
                "thread_id": context.thread_id,
                "application": context.application,
                "tenant": context.principal.tenant,
                "subject": context.principal.subject,
                "model_alias": call.alias,
                "model_provider": call.provider,
                "model_id": call.model_id,
                **payload(),
            }
            await self._sink.write(
                {key: diagnostic_content(value) for key, value in record.items()}
            )
        except Exception as error:  # noqa: BLE001 - diagnostics must not fail model operations
            self._failed = True
            _LOG.warning(
                "model_trace.disabled",
                extra={"error_type": type(error).__name__, "request_id": context.request_id},
            )
