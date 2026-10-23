"""Bounded question-answer HTTP calls with no automatic retries or redirects.

An agent on AgentCore Runtime is the one exception to "no retries": the service
answers ``RetryableConflictException`` while a session operation is still in
progress, before the agent ran, so the same request is sent again after a
short backoff. Every other outcome follows the loopback rules.
"""

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from agentcraft2_ui.adapters.events import terminal_body
from agentcraft2_ui.application.question_answer import QuestionAnswerAdapter
from agentcraft2_ui.contracts.catalog import AgentBinding, AgentCoreRuntime
from agentcraft2_ui.contracts.chat import StoredTurn
from agentcraft2_ui.contracts.conversations import FailureCode, RunRecord, TurnAdmission
from agentcraft2_ui.contracts.queries import ContractError, ErrorCode, ViewContext
from agentcraft2_ui.contracts.traces import SESSION_ID, console_link, tolerant_trace

# Operational records only: a status, a path and identifiers. Never a body.
logger = logging.getLogger(__name__)

_TERMINAL_FAILURES = frozenset({"invalid", "denied", "unauthorized", "budget_exceeded", "paused"})

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"
REQUEST_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Request-Id"
ERROR_TYPE_HEADER = "x-amzn-ErrorType"
RETRY_BACKOFF = (0.5, 1.0, 2.0)
_SESSION_ID = re.compile(SESSION_ID)
_TOKEN = re.compile(r"^[A-Za-z0-9._~+/=-]{1,8192}$")
# The service, not the agent, answered: the request may or may not have run.
_RUNTIME_UNKNOWN = frozenset(
    {
        "RetryableConflictException",
        "ThrottlingException",
        "InternalServerException",
        "ServiceQuotaExceededException",
    }
)
# The service refused the caller; the agent never saw the request.
_RUNTIME_REFUSALS: dict[str, FailureCode] = {
    "UnauthorizedException": "unauthorized",
    "AccessDeniedException": "denied",
}


class AwsCredentials(Protocol):
    """What the SigV4 signer needs; botocore's frozen credentials satisfy it."""

    @property
    def access_key(self) -> str:
        """The access key id."""
        ...

    @property
    def secret_key(self) -> str:
        """The secret key; only ever handed to the signer."""
        ...

    @property
    def token(self) -> str | None:
        """The session token of temporary credentials, when there is one."""
        ...


CredentialSource = Callable[[], AwsCredentials | None]
Sleep = Callable[[float], Awaitable[None]]


def default_credentials() -> AwsCredentials | None:
    """Resolve the desktop's AWS credentials through boto3 for one request.

    boto3 is the UI's optional ``aws`` extra; without it an IAM runtime binding
    cannot be used, which is reported as ``unavailable`` and logged by name.
    """
    try:
        import boto3
    except ImportError as error:
        logger.error(
            "AgentCore Runtime requests need the aws extra: pip install agentcraft2-ui[aws]",
            extra={"missing": error.name},
        )
        raise ContractError(ErrorCode.UNAVAILABLE) from error
    credentials = boto3.Session().get_credentials()
    if credentials is None:
        return None
    frozen: AwsCredentials = credentials.get_frozen_credentials()
    return frozen


def session_id(thread_id: str) -> str:
    """The runtime session id for a conversation; the agent sees it as its thread id.

    A thread id that already satisfies the platform's rule (33 to 128 characters
    of ``[A-Za-z0-9._:-]``) is used as is. A shorter or longer one is extended
    deterministically with the first 32 hex digits of its SHA-256, so the same
    conversation always maps to the same session.
    """
    if _SESSION_ID.fullmatch(thread_id):
        return thread_id
    digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:32]
    return f"{thread_id[:64]}-{digest}"


class HttpDispatcher:
    """Use a dedicated client; no proxy, browser headers or browser URLs are accepted.

    ``credentials`` supplies AWS credentials for IAM-authenticated runtime
    bindings and ``sleep`` paces the retryable-conflict backoff; both exist so
    tests can inject them.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        timeout_seconds: float,
        *,
        credentials: CredentialSource = default_credentials,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.client, self.timeout = client, timeout_seconds
        self.credentials, self.sleep = credentials, sleep

    async def outcome(
        self, view: ViewContext, binding: AgentBinding, stored: StoredTurn
    ) -> RunRecord | None:
        """Fetch only the terminal journal entry; never POST or execute agent work."""
        run = stored.run
        if run.scope != view.scope or run.state != "unknown":
            raise ContractError(ErrorCode.NOT_FOUND)
        if binding.runtime is not None:
            # Only /invocations is proxied by the runtime; the agent's outcome
            # route is unreachable, so an unknown outcome stays unknown.
            logger.info(
                "agent outcome cannot be reconciled through AgentCore Runtime",
                extra={
                    "agent_id": binding.manifest.agent_id,
                    "thread_id": run.thread_id,
                    "request_id": run.request_id,
                },
            )
            return None
        async with asyncio.timeout(min(self.timeout, 10)):
            async with self.client.stream(
                "GET",
                binding.base_url + "/requests/" + quote(run.request_id, safe=""),
                params={"thread_id": run.thread_id},
                headers={"x-request-id": run.request_id},
                timeout=min(self.timeout, 10),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > binding.manifest.max_response_bytes:
                        raise ContractError(ErrorCode.INVALID_RESPONSE)
        envelope = json.loads(body)
        if envelope == {"state": "unknown"}:
            return None
        if (
            not isinstance(envelope, dict)
            or envelope.get("thread_id") != run.thread_id
            or envelope.get("request_id") != run.request_id
        ):
            raise ContractError(ErrorCode.INVALID_RESPONSE)
        if "output" in envelope and "error" not in envelope:
            turn = TurnAdmission(
                scope=view.scope,
                thread_id=run.thread_id,
                idempotency_key=run.idempotency_key,
                question=stored.question,
            )
            answer = QuestionAnswerAdapter(binding.manifest).response(
                view,
                turn,
                bytes(body),
                request_id=run.request_id,
            )
            result = run.model_copy(
                update={
                    "state": "completed",
                    "answer": answer.answer,
                    "artifacts": answer.artifacts,
                    "artifact_status": answer.artifact_status,
                    "artifact_warning": answer.artifact_warning,
                    "usage": answer.usage,
                    "model_calls": answer.model_calls,
                    "trace": answer.trace,
                    "updated_at": datetime.now(UTC),
                }
            )
        elif envelope.get("error") in _TERMINAL_FAILURES and "output" not in envelope:
            result = _failed_run(run, time.monotonic(), bytes(body))
        else:
            return None
        # Invalid optional timing cannot invalidate an authoritative outcome.
        elapsed = envelope.get("elapsed_ms")
        if type(elapsed) in (float, int):
            try:
                return RunRecord.model_validate({**result.model_dump(), "elapsed_ms": elapsed})
            except ValidationError:
                pass
        return result.model_copy(update={"elapsed_ms": run.elapsed_ms})

    async def dispatch(
        self, view: ViewContext, binding: AgentBinding, turn: TurnAdmission, run: RunRecord
    ) -> RunRecord:
        """Send only the new question and require its original request/thread correlation."""
        # A wall-clock deadline also bounds a server that sends a slow trickle of
        # bytes; HTTPX's read timeout alone resets for each received chunk.
        async with asyncio.timeout(self.timeout):
            return await self._send(view, binding, turn, run)

    async def _send(
        self, view: ViewContext, binding: AgentBinding, turn: TurnAdmission, run: RunRecord
    ) -> RunRecord:
        adapter = QuestionAnswerAdapter(binding.manifest)
        started = time.monotonic()
        runtime = binding.runtime
        # The runtime hands the session id to the agent as its thread id, so the
        # body and every correlation check use that id; the run keeps the UI's.
        agent_turn = (
            turn
            if runtime is None
            else turn.model_copy(update={"thread_id": session_id(turn.thread_id)})
        )
        url = binding.base_url + "/invoke"
        if runtime is not None:
            url = binding.base_url + "?qualifier=" + quote(runtime.qualifier, safe="")
        body = json.dumps(adapter.request(view, agent_turn)).encode("utf-8")
        result: RunRecord | None = None
        for pause in (*RETRY_BACKOFF, None):
            try:
                headers = self._headers(binding, run, agent_turn, url, body)
            except ContractError as error:
                # Nothing was sent: a known failure, not an unknown outcome. The
                # signer or token reader logged the reason; this keeps the code.
                _log_degraded("not sent: " + error.code.value, 0, binding, run)
                return _runtime_failure(run, started, "unauthorized")
            async with self.client.stream(
                "POST",
                url,
                content=body,
                headers=headers,
                timeout=self.timeout,
                follow_redirects=False,
            ) as response:
                if (
                    runtime is None
                    or pause is None
                    or _error_type(response) != "RetryableConflictException"
                ):
                    result = await self._conclude(
                        view, binding, adapter, agent_turn, run, response, started
                    )
            if result is not None or pause is None:
                break
            _log_degraded("retried after a retryable conflict", response.status_code, binding, run)
            await self.sleep(pause)
        if result is None:  # The last attempt always concludes or raises.
            raise ContractError(ErrorCode.INVALID_RESPONSE)
        if runtime is not None and result.trace is None:
            # The runtime's own observability covers completed and failed runs alike.
            result = result.model_copy(
                update={"trace": console_link(runtime.aws_region, agent_turn.thread_id)}
            )
        return result

    def _headers(
        self,
        binding: AgentBinding,
        run: RunRecord,
        agent_turn: TurnAdmission,
        url: str,
        body: bytes,
    ) -> dict[str, str]:
        headers = {
            "x-request-id": run.request_id,
            "accept": "text/event-stream, application/json",
            "content-type": "application/json",
        }
        runtime = binding.runtime
        if runtime is None:
            return headers
        headers[SESSION_HEADER] = agent_turn.thread_id
        headers[REQUEST_HEADER] = run.request_id
        if runtime.auth == "bearer":
            headers["authorization"] = "Bearer " + _read_token(runtime)
            return headers
        return _sign(url, body, headers, runtime.aws_region, self.credentials())

    async def _conclude(
        self,
        view: ViewContext,
        binding: AgentBinding,
        adapter: QuestionAnswerAdapter,
        agent_turn: TurnAdmission,
        run: RunRecord,
        response: httpx.Response,
        started: float,
    ) -> RunRecord:
        limit = binding.manifest.max_response_bytes
        if binding.runtime is not None:
            error_type = _error_type(response)
            if error_type in _RUNTIME_UNKNOWN:
                _log_degraded("outcome unknown: " + error_type, response.status_code, binding, run)
                raise ContractError(ErrorCode.INVALID_RESPONSE)
            if error_type in _RUNTIME_REFUSALS:
                _log_degraded("refused: " + error_type, response.status_code, binding, run)
                return _runtime_failure(run, started, _RUNTIME_REFUSALS[error_type])
            if error_type == "RuntimeClientError":
                # The container answered with an error; its envelope, when it
                # survived the proxy, is the agent's own refusal.
                body = await _bounded_body(response, limit)
                if _terminal_envelope(body, agent_turn.thread_id, run.request_id):
                    return _failed_run(run, started, body)
                _log_degraded("failed in the agent container", response.status_code, binding, run)
                return _runtime_failure(run, started, "runtime_client_error")
        # Redirects, timeouts, malformed envelopes and oversized responses
        # leave the outcome unknown. None are safe evidence for replay.
        if (
            300 <= response.status_code < 400
            or response.status_code == 408
            or response.status_code >= 500
        ):
            _log_degraded("outcome unknown", response.status_code, binding, run)
            raise ContractError(ErrorCode.INVALID_RESPONSE)
        if response.status_code >= 400:
            body = await _bounded_body(response, limit)
            if _terminal_envelope(body, agent_turn.thread_id, run.request_id):
                return _failed_run(run, started, body)
            # A refusal the agent did not describe in its envelope: the user sees
            # "failed"; the log keeps the status so an operator can find the cause.
            _log_degraded("failed without an envelope", response.status_code, binding, run)
            return run.model_copy(
                update={
                    "state": "failed",
                    "updated_at": datetime.now(UTC),
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                }
            )
        if response.headers.get("content-type", "").split(";", 1)[0] == "text/event-stream":
            terminal = await terminal_body(
                response, limit, thread_id=agent_turn.thread_id, request_id=run.request_id
            )
            if terminal.failed:
                return _failed_run(run, started, terminal.body)
            body = terminal.body
        else:
            body = await _bounded_body(response, limit)
        answer = adapter.response(
            view, agent_turn, body, status_code=response.status_code, request_id=run.request_id
        )
        artifacts = answer.artifacts
        if agent_turn.thread_id != run.thread_id:
            # Correlation was checked against the session id; the stored run and
            # its documents keep the conversation's own thread id.
            artifacts = tuple(
                item.model_copy(
                    update={
                        "artifact": item.artifact.model_copy(update={"thread_id": run.thread_id})
                    }
                )
                for item in artifacts
            )
        return run.model_copy(
            update={
                "state": "completed",
                "answer": answer.answer,
                "artifacts": artifacts,
                "artifact_status": answer.artifact_status,
                "artifact_warning": answer.artifact_warning,
                "request_id": answer.request_id,
                "updated_at": datetime.now(UTC),
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "usage": answer.usage,
                "model_calls": answer.model_calls,
                "trace": answer.trace,
            }
        )


def _error_type(response: httpx.Response) -> str:
    """The AgentCore exception name; the service may suffix it with a type URI."""
    value: str = response.headers.get(ERROR_TYPE_HEADER, "")
    return value.split(":", 1)[0].strip()


def _read_token(runtime: AgentCoreRuntime) -> str:
    """The first line of the operator's token file, read for this request only."""
    path = runtime.token_file
    if path is None:
        raise ContractError(ErrorCode.UNAVAILABLE)
    try:
        with path.open(encoding="utf-8") as source:
            token = source.readline().strip()
    except OSError as error:
        logger.error("the AgentCore bearer token file could not be read")
        raise ContractError(ErrorCode.UNAVAILABLE) from error
    if not _TOKEN.fullmatch(token):
        logger.error("the AgentCore bearer token file does not start with a token")
        raise ContractError(ErrorCode.UNAVAILABLE)
    return token


def _sign(
    url: str,
    body: bytes,
    headers: dict[str, str],
    region: str,
    credentials: AwsCredentials | None,
) -> dict[str, str]:
    """Sign the exact bytes and headers that will be sent, as service bedrock-agentcore."""
    try:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
    except ImportError as error:
        logger.error(
            "AgentCore Runtime requests need the aws extra: pip install agentcraft2-ui[aws]",
            extra={"missing": error.name},
        )
        raise ContractError(ErrorCode.UNAVAILABLE) from error
    if credentials is None:
        logger.error("no AWS credentials were found for the AgentCore Runtime request")
        raise ContractError(ErrorCode.UNAVAILABLE)
    request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    SigV4Auth(
        Credentials(credentials.access_key, credentials.secret_key, credentials.token),
        "bedrock-agentcore",
        region,
    ).add_auth(request)
    return {str(name): str(value) for name, value in request.headers.items()}


def _terminal_envelope(body: bytes, thread_id: str, request_id: str) -> bool:
    """Whether the body is this request's own terminal refusal from the agent."""
    try:
        envelope = json.loads(body)
    except ValueError:
        return False
    return (
        isinstance(envelope, dict)
        and envelope.get("thread_id") == thread_id
        and envelope.get("request_id") == request_id
        and envelope.get("error") in _TERMINAL_FAILURES
        and "output" not in envelope
    )


async def _bounded_body(response: httpx.Response, limit: int) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if len(body) > limit:
            raise ContractError(ErrorCode.INVALID_RESPONSE)
    return bytes(body)


def _log_degraded(what: str, status: int, binding: AgentBinding, run: RunRecord) -> None:
    logger.warning(
        "agent dispatch %s",
        what,
        extra={
            "status": status,
            "path": "/invoke" if binding.runtime is None else "/invocations",
            "agent_id": binding.manifest.agent_id,
            "thread_id": run.thread_id,
            "request_id": run.request_id,
        },
    )


def _runtime_failure(run: RunRecord, started: float, code: FailureCode) -> RunRecord:
    """A failure the service or this process reported, with no agent envelope to keep."""
    return run.model_copy(
        update={
            "state": "failed",
            "failure_code": code,
            "updated_at": datetime.now(UTC),
            "elapsed_ms": (time.monotonic() - started) * 1000,
        }
    )


def _failed_run(run: RunRecord, started: float, body: bytes) -> RunRecord:
    """Keep optional diagnostics on a known failure without accepting its draft."""
    envelope = json.loads(body)
    failed = run.model_copy(
        update={
            "state": "failed",
            "failure_code": envelope["error"],
            "updated_at": datetime.now(UTC),
            "elapsed_ms": (time.monotonic() - started) * 1000,
        }
    )
    with suppress(ValidationError):
        failed = RunRecord.model_validate(
            {
                **failed.model_dump(),
                "usage": envelope.get("usage", {}),
                "model_calls": envelope.get("model_calls"),
            }
        )
    return failed.model_copy(update={"trace": tolerant_trace(envelope.get("trace"))})
