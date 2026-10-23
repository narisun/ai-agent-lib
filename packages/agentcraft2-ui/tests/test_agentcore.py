"""Chat with an agent on AgentCore Runtime: binding, signing, session id and errors."""

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from local_runtime import AGENT_URL, settings_at
from pydantic import ValidationError
from python_runtime import python_command

from agentcraft2_core.contracts import Principal, Scope
from agentcraft2_ui.adapters.catalog import pin_bindings, read_registries
from agentcraft2_ui.adapters.http import (
    REQUEST_HEADER,
    RETRY_BACKOFF,
    SESSION_HEADER,
    HttpDispatcher,
    session_id,
)
from agentcraft2_ui.application.catalog import read_manifest
from agentcraft2_ui.config.settings import LocalIdentity, Settings
from agentcraft2_ui.contracts.catalog import AgentBinding, AgentCoreRuntime
from agentcraft2_ui.contracts.chat import StoredTurn
from agentcraft2_ui.contracts.conversations import RunRecord, RunState, TurnAdmission
from agentcraft2_ui.contracts.queries import ContractError, ErrorCode, ViewContext
from agentcraft2_ui.contracts.traces import TraceLink, console_link

FIXTURES = Path(__file__).parent / "fixtures"
ARN = "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/rmprep-abc123"
ESCAPED = "arn%3Aaws%3Abedrock-agentcore%3Aeu-west-1%3A123456789012%3Aruntime%2Frmprep-abc123"
URL = f"https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/{ESCAPED}/invocations"
SCOPE = Scope("tenant-1", "ada", "rmprep")
VIEW = ViewContext(principal=Principal(subject="ada", tenant="tenant-1"), scope=SCOPE)
LONG_THREAD = "0f0b6a9e-3f2a-4c8e-9d2b-1a2b3c4d5e6f"
SESSION = "0" * 40


def static_credentials() -> Any:
    import boto3

    return (
        boto3.Session(
            aws_access_key_id="AKIAIOSFODNN7EXAMPLE",
            aws_secret_access_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            region_name="eu-west-1",
        )
        .get_credentials()
        .get_frozen_credentials()
    )


def runtime(**overrides: Any) -> AgentCoreRuntime:
    return AgentCoreRuntime(arn=ARN, **overrides)


def binding(**overrides: Any) -> AgentBinding:
    manifest = read_manifest(json.loads((FIXTURES / "presentation.json").read_text()))
    return AgentBinding(manifest=manifest, base_url=URL, runtime=runtime(**overrides))


def turn(thread_id: str = LONG_THREAD) -> TurnAdmission:
    return TurnAdmission(
        scope=SCOPE, thread_id=thread_id, idempotency_key="send-1", question="A memo?"
    )


def run(thread_id: str = LONG_THREAD, state: RunState = "running") -> RunRecord:
    now = datetime.now(UTC)
    return RunRecord(
        scope=SCOPE,
        thread_id=thread_id,
        request_id="request-1",
        run_id="run-1",
        idempotency_key="send-1",
        state=state,
        accepted_at=now,
        updated_at=now,
    )


def envelope(thread_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "request_id": "request-1",
        "output": {"answer": "Accepted answer"},
        **extra,
    }


# ------------------------------------------------------------------ binding and settings


def test_runtime_derives_region_and_invocation_url_from_the_arn() -> None:
    value = runtime()
    assert value.aws_region == "eu-west-1"
    assert value.qualifier == "DEFAULT"
    assert value.invocation_url == URL
    assert runtime(region="eu-west-1").invocation_url == URL
    with pytest.raises(ValidationError, match="region must match"):
        runtime(region="us-east-1")


@pytest.mark.parametrize(
    "arn",
    [
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:memory/rmprep",
        "arn:aws:bedrock:eu-west-1:123456789012:runtime/rmprep",
        "arn:aws:bedrock-agentcore:eu-west-1:12345:runtime/rmprep",
        "arn:aws:bedrock-agentcore:eu-west-1:123456789012:runtime/rm prep",
        "https://bedrock-agentcore.eu-west-1.amazonaws.com/runtimes/x/invocations",
    ],
)
def test_runtime_rejects_arns_that_are_not_runtime_arns(arn: str) -> None:
    with pytest.raises(ValidationError):
        AgentCoreRuntime(arn=arn)


def test_bearer_auth_requires_a_token_file_and_iam_refuses_one(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="token_file"):
        runtime(auth="bearer")
    with pytest.raises(ValidationError, match="token_file"):
        runtime(token_file=tmp_path / "token")
    assert runtime(auth="bearer", token_file=tmp_path / "token").auth == "bearer"


def test_settings_accept_the_https_runtime_binding_and_refuse_http(tmp_path: Path) -> None:
    plain = settings_at(tmp_path)
    accepted = plain.model_copy(
        update={
            "bindings": (*plain.bindings, binding()),
            "allowed_urls": plain.allowed_urls | {URL},
        }
    )
    assert Settings.model_validate(accepted.model_dump()).bindings[-1].runtime == runtime()
    insecure = binding().model_copy(update={"base_url": URL.replace("https", "http", 1)})
    with pytest.raises(ValidationError, match="destination_denied"):
        Settings.model_validate(
            accepted.model_copy(
                update={
                    "bindings": (*plain.bindings, insecure),
                    "allowed_urls": accepted.allowed_urls | {insecure.base_url},
                }
            ).model_dump()
        )
    # The derived URL still needs the explicit allowlist, like every destination.
    with pytest.raises(ValidationError, match="destination_denied"):
        Settings.model_validate(
            accepted.model_copy(update={"allowed_urls": plain.allowed_urls}).model_dump()
        )


def test_loopback_rule_is_unchanged_for_plain_bindings(tmp_path: Path) -> None:
    plain = settings_at(tmp_path)
    remote = plain.bindings[0].model_copy(update={"base_url": "https://agents.example/memo"})
    with pytest.raises(ValidationError, match=r"destination_denied|loopback"):
        Settings.model_validate(
            plain.model_copy(
                update={
                    "bindings": (remote, *plain.bindings[1:]),
                    "allowed_urls": plain.allowed_urls | {remote.base_url},
                }
            ).model_dump()
        )


def test_pin_bindings_derives_the_url_for_a_runtime_agent_without_a_registry_url(
    tmp_path: Path,
) -> None:
    agents_path, tools_path = tmp_path / "agents.json", tmp_path / "tools.json"
    agents_path.write_text(
        json.dumps(
            {
                "schema": "agentcraft2.registry/v1",
                "agents": [
                    {"id": "memo", "owner": "team", "version": "1", "url": f"{AGENT_URL}/memo"},
                    {"id": "hosted", "owner": "team", "version": "1"},
                ],
            }
        )
    )
    tools_path.write_text('{"schema":"agentcraft2.registry/v1","servers":[]}')
    snapshot, _ = read_registries(agents_path, tools_path)
    allowed = frozenset({f"{AGENT_URL}/memo", URL})
    with pytest.raises(ValueError, match="loopback url"):
        pin_bindings(snapshot, ("memo", "hosted"), allowed)
    with pytest.raises(ValueError, match="also be selected"):
        pin_bindings(snapshot, ("memo",), allowed, runtimes={"hosted": runtime()})
    pinned = pin_bindings(snapshot, ("memo", "hosted"), allowed, runtimes={"hosted": runtime()})
    assert pinned[0].runtime is None
    assert pinned[1].base_url == URL
    assert pinned[1].runtime == runtime()
    settings = Settings(
        agents_path=agents_path,
        tools_path=tools_path,
        identity=LocalIdentity(),
        allowed_urls=allowed,
        bindings=pinned,
    )
    config = tmp_path / "config.json"
    config.write_text(json.dumps(settings.model_dump(mode="json")))
    assert Settings.read(config).bindings == pinned


# ------------------------------------------------------------------ session id


def test_session_id_keeps_platform_sized_thread_ids_and_derives_the_rest() -> None:
    assert session_id(LONG_THREAD) == LONG_THREAD
    assert session_id("a" * 128) == "a" * 128
    derived = session_id("thread-1")
    assert derived.startswith("thread-1-")
    assert len(derived) == len("thread-1") + 33
    assert derived == session_id("thread-1")
    assert derived != session_id("thread-2")
    assert len(session_id("b" * 129)) == 64 + 33
    for value in (derived, session_id("b" * 129)):
        TraceLink(provider="cloudwatch", trace_id=value, url=console_link("eu-west-1", value).url)


# ------------------------------------------------------------------ dispatcher


class Recorder:
    """Capture requests and script the service's replies; count the backoff sleeps."""

    def __init__(self, *replies: httpx.Response) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def transport(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def dispatcher(self, client: httpx.AsyncClient) -> HttpDispatcher:
        return HttpDispatcher(client, 10, credentials=static_credentials, sleep=self.sleep)


def service_error(status: int, name: str) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"x-amzn-ErrorType": name + ":http://internal.amazon.com/coral/"},
        json={"message": name},
    )


async def dispatch(
    recorder: Recorder, bound: AgentBinding | None = None, thread_id: str = LONG_THREAD
) -> RunRecord:
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder.transport)) as client:
        return await recorder.dispatcher(client).dispatch(
            VIEW, bound or binding(), turn(thread_id), run(thread_id)
        )


async def test_iam_mode_signs_the_exact_body_and_sends_the_runtime_headers() -> None:
    recorder = Recorder(httpx.Response(200, json=envelope(LONG_THREAD)))
    result = await dispatch(recorder)
    request = recorder.requests[0]
    assert str(request.url) == URL + "?qualifier=DEFAULT"
    assert request.headers[SESSION_HEADER] == LONG_THREAD
    assert request.headers[REQUEST_HEADER] == "request-1"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content)["thread_id"] == LONG_THREAD
    authorization = request.headers["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/")
    assert "/eu-west-1/bedrock-agentcore/aws4_request" in authorization
    assert "x-amzn-bedrock-agentcore-runtime-session-id" in authorization
    assert request.headers["x-amz-date"]
    assert result.state == "completed"
    assert result.answer == "Accepted answer"
    assert result.thread_id == LONG_THREAD
    assert result.trace is not None
    assert result.trace.provider == "cloudwatch"
    assert result.trace.trace_id == LONG_THREAD
    assert result.trace.label == "CloudWatch"
    assert result.trace.url == (
        "https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1"
        "#gen-ai-observability"
    )


async def test_bearer_mode_reads_the_first_line_of_the_token_file_per_request(
    tmp_path: Path,
) -> None:
    token = tmp_path / "token"
    token.write_text("first-token\nsecond line is ignored\n")
    bound = binding(auth="bearer", token_file=token)
    recorder = Recorder(httpx.Response(200, json=envelope(LONG_THREAD)))
    await dispatch(recorder, bound)
    assert recorder.requests[0].headers["authorization"] == "Bearer first-token"
    assert "x-amz-date" not in recorder.requests[0].headers
    token.write_text("rotated\n")
    await dispatch(recorder, bound)
    assert recorder.requests[1].headers["authorization"] == "Bearer rotated"


async def test_unreadable_token_file_fails_the_run_without_sending(tmp_path: Path) -> None:
    recorder = Recorder(httpx.Response(200, json=envelope(LONG_THREAD)))
    result = await dispatch(recorder, binding(auth="bearer", token_file=tmp_path / "missing"))
    assert recorder.requests == []
    assert result.state == "failed"
    assert result.failure_code == "unauthorized"


async def test_missing_aws_credentials_fail_the_run_without_sending() -> None:
    recorder = Recorder(httpx.Response(200, json=envelope(LONG_THREAD)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder.transport)) as client:
        dispatcher = HttpDispatcher(client, 10, credentials=lambda: None, sleep=recorder.sleep)
        result = await dispatcher.dispatch(VIEW, binding(), turn(), run())
    assert recorder.requests == []
    assert result.state == "failed"
    assert result.failure_code == "unauthorized"


async def test_short_thread_ids_become_a_derived_session_id_the_agent_echoes() -> None:
    session = session_id("thread-1")
    recorder = Recorder(httpx.Response(200, json=envelope(session)))
    result = await dispatch(recorder, thread_id="thread-1")
    request = recorder.requests[0]
    assert request.headers[SESSION_HEADER] == session
    assert json.loads(request.content)["thread_id"] == session
    assert result.state == "completed"
    assert result.thread_id == "thread-1"
    assert result.trace is not None
    assert result.trace.trace_id == session


async def test_an_envelope_for_the_ui_thread_id_is_uncorrelated_once_derived() -> None:
    recorder = Recorder(httpx.Response(200, json=envelope("thread-1")))
    with pytest.raises(ContractError) as error:
        await dispatch(recorder, thread_id="thread-1")
    assert error.value.code == ErrorCode.INVALID_RESPONSE


async def test_retryable_conflict_is_retried_with_backoff_then_left_unknown() -> None:
    recorder = Recorder(service_error(409, "RetryableConflictException"))
    with pytest.raises(ContractError) as error:
        await dispatch(recorder)
    assert error.value.code == ErrorCode.INVALID_RESPONSE
    assert len(recorder.requests) == len(RETRY_BACKOFF) + 1
    assert recorder.sleeps == list(RETRY_BACKOFF)
    assert len({request.headers["x-amz-date"] for request in recorder.requests}) >= 1


async def test_retryable_conflict_then_success_completes_the_run() -> None:
    recorder = Recorder(
        service_error(409, "RetryableConflictException"),
        httpx.Response(200, json=envelope(LONG_THREAD)),
    )
    result = await dispatch(recorder)
    assert result.state == "completed"
    assert recorder.sleeps == [RETRY_BACKOFF[0]]
    assert len(recorder.requests) == 2


async def test_runtime_client_error_is_a_failed_run_the_ui_never_replays() -> None:
    recorder = Recorder(service_error(424, "RuntimeClientError"))
    result = await dispatch(recorder)
    assert result.state == "failed"
    assert result.failure_code == "runtime_client_error"
    assert result.answer is None
    assert result.trace is not None
    assert result.trace.provider == "cloudwatch"
    assert recorder.sleeps == []


async def test_runtime_client_error_keeps_the_agents_own_refusal_when_it_survives() -> None:
    recorder = Recorder(
        httpx.Response(
            424,
            headers={"x-amzn-ErrorType": "RuntimeClientError"},
            json={"error": "invalid", "thread_id": LONG_THREAD, "request_id": "request-1"},
        )
    )
    result = await dispatch(recorder)
    assert result.state == "failed"
    assert result.failure_code == "invalid"


@pytest.mark.parametrize(
    ("status", "name", "code"),
    [(401, "UnauthorizedException", "unauthorized"), (403, "AccessDeniedException", "denied")],
)
async def test_service_refusals_are_terminal_failures(status: int, name: str, code: str) -> None:
    recorder = Recorder(service_error(status, name))
    result = await dispatch(recorder)
    assert result.state == "failed"
    assert result.failure_code == code
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    ("status", "name"),
    [
        (429, "ThrottlingException"),
        (500, "InternalServerException"),
        (402, "ServiceQuotaExceededException"),
    ],
)
async def test_service_faults_leave_the_outcome_unknown(status: int, name: str) -> None:
    recorder = Recorder(service_error(status, name))
    with pytest.raises(ContractError) as error:
        await dispatch(recorder)
    assert error.value.code == ErrorCode.INVALID_RESPONSE
    assert recorder.sleeps == []


async def test_validation_exception_without_an_envelope_fails_like_any_4xx() -> None:
    recorder = Recorder(service_error(400, "ValidationException"))
    result = await dispatch(recorder)
    assert result.state == "failed"
    assert result.failure_code is None


async def test_event_stream_through_the_runtime_correlates_on_the_session_id() -> None:
    session = session_id("thread-1")
    draft = {"thread_id": session, "request_id": "request-1", "call": 1, "text": "x", "reset": True}
    frames = (
        f"event: draft\ndata: {json.dumps(draft)}\n\n"
        f"event: result\ndata: {json.dumps(envelope(session))}\n\n"
    )
    recorder = Recorder(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=frames)
    )
    result = await dispatch(recorder, thread_id="thread-1")
    assert result.state == "completed"
    assert result.answer == "Accepted answer"


async def test_an_envelope_trace_link_is_kept_over_the_console_link() -> None:
    trace = {
        "provider": "phoenix",
        "trace_id": "1234567890abcdef1234567890abcdef",
        "url": "https://traces.example/redirects/traces/1234567890abcdef1234567890abcdef",
    }
    body = envelope(LONG_THREAD)
    body["output"]["trace"] = trace
    recorder = Recorder(httpx.Response(200, json=body))
    result = await dispatch(recorder)
    assert result.trace is not None
    assert result.trace.provider == "phoenix"


async def test_outcome_is_not_reconciled_through_the_runtime(
    caplog: pytest.LogCaptureFixture,
) -> None:
    recorder = Recorder(httpx.Response(200, json=envelope(LONG_THREAD)))
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder.transport)) as client:
        with caplog.at_level("INFO", logger="agentcraft2_ui.adapters.http"):
            result = await recorder.dispatcher(client).outcome(
                VIEW, binding(), StoredTurn(run=run(state="unknown"), question="A memo?")
            )
    assert result is None
    assert recorder.requests == []
    assert "cannot be reconciled" in caplog.text
    assert "A memo?" not in caplog.text


# ------------------------------------------------------------------ trace link


@pytest.mark.parametrize(
    "url",
    [
        "https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#gen-ai-observability",
        "https://console.aws.amazon.com/cloudwatch/home?region=eu-west-1#gen-ai-observability",
        "https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1"
        "#gen-ai-observability/agent-core/sessions",
    ],
)
def test_cloudwatch_trace_links_open_the_regional_observability_view(url: str) -> None:
    link = TraceLink(provider="cloudwatch", trace_id=SESSION, url=url)
    assert link.label == "CloudWatch"
    assert link.url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#gen-ai-observability",
        "https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#gen-ai-observability",
        "https://eu-west-1.console.aws.amazon.com/cloudwatch/home#gen-ai-observability",
        "https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1",
        "https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#logsV2",
        "https://eu-west-1.console.aws.amazon.com/cloudwatch/?region=eu-west-1#gen-ai-observability",
        "https://eu-west-1.console.aws.amazon.com.evil.example/cloudwatch/home?region=eu-west-1#gen-ai-observability",
        "https://user:secret@eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#gen-ai-observability",
        "https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1&token=x#gen-ai-observability",
        "https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#gen-ai-observability)[x](https://evil.example)",
    ],
)
def test_cloudwatch_trace_links_reject_other_hosts_schemes_and_views(url: str) -> None:
    with pytest.raises(ValidationError):
        TraceLink(provider="cloudwatch", trace_id=SESSION, url=url)


@pytest.mark.parametrize("trace_id", ["0" * 32, "0" * 129, "bad id" + "0" * 30, ""])
def test_cloudwatch_trace_ids_are_session_ids(trace_id: str) -> None:
    with pytest.raises(ValidationError):
        console_link("eu-west-1", trace_id)


def test_other_providers_keep_their_trace_id_and_route_rules() -> None:
    with pytest.raises(ValidationError):
        TraceLink(
            provider="phoenix",
            trace_id=SESSION,
            url=f"http://127.0.0.1:6006/redirects/traces/{SESSION}",
        )
    with pytest.raises(ValidationError):
        TraceLink(
            provider="phoenix",
            trace_id="1234567890abcdef1234567890abcdef",
            url="https://eu-west-1.console.aws.amazon.com/cloudwatch/home?region=eu-west-1#gen-ai-observability",
        )


# ------------------------------------------------------------------ init


@pytest.mark.slow
def test_init_pins_a_runtime_agent_that_settings_read_and_check_accept(tmp_path: Path) -> None:
    (tmp_path / "agents.json").write_text(
        json.dumps(
            {
                "schema": "agentcraft2.registry/v1",
                "agents": [
                    {"id": "memo", "version": "1", "owner": "team", "url": f"{AGENT_URL}/memo"},
                    {"id": "hosted", "version": "1", "owner": "team"},
                ],
            }
        )
    )
    (tmp_path / "tools.json").write_text('{"schema":"agentcraft2.registry/v1","servers":[]}')
    (tmp_path / "agent.env").write_text(
        'AC2_REGISTRY_OPTIONS={"agents_path":"agents.json","tools_path":"tools.json"}\n'
    )
    token = tmp_path / "token"
    token.write_text("secret-token\n")
    config = tmp_path / ".agentcraft2/ui/config.json"
    env = {key: value for key, value in os.environ.items() if key != "LITERAL_API_KEY"}

    def command(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed module in this test's interpreter
            python_command("-m", "agentcraft2_ui", *args, "--config", str(config)),
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=60,
        )

    result = command(
        "init",
        "--env-file",
        str(tmp_path / "agent.env"),
        "--agent",
        "memo",
        "--agent",
        "hosted",
        "--allow-url",
        f"{AGENT_URL}/memo",
        "--agentcore-runtime",
        f"hosted={ARN}",
        "--agentcore-auth",
        "bearer",
        "--agentcore-token-file",
        str(token),
    )
    assert result.returncode == 0, result.stderr
    settings = Settings.read(config)
    hosted = next(b for b in settings.bindings if b.manifest.agent_id == "hosted")
    assert hosted.base_url == URL
    assert hosted.runtime == AgentCoreRuntime(arn=ARN, auth="bearer", token_file=token.resolve())
    assert URL in settings.allowed_urls
    assert "secret-token" not in config.read_text()
    check = command("check")
    assert check.returncode == 0, check.stderr
    assert "2 agent(s)" in check.stdout
    rejected = command(
        "init",
        "--overwrite",
        "--env-file",
        str(tmp_path / "agent.env"),
        "--agent",
        "memo",
        "--allow-url",
        f"{AGENT_URL}/memo",
        "--agentcore-runtime",
        f"hosted={ARN}",
    )
    assert rejected.returncode == 2
    assert "--agent" in rejected.stderr
