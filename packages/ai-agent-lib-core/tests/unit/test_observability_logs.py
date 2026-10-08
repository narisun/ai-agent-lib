"""Operational logs: one line of JSON, metadata only, nothing shaped like a credential."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer

from ai_agent_lib_core import Principal, RequestContext, bind_request_context
from ai_agent_lib_core.adapters import (
    JwtIdentityOptions,
    JwtIdentityVerifier,
    StaticIdentityOptions,
    StaticIdentityVerifier,
    resolve_jwt_options,
)
from ai_agent_lib_core.contracts import (
    Classification,
    PolicyDenied,
    ServerEntry,
    ToolEntry,
    ValidationFailed,
)
from ai_agent_lib_core.integrations.http import ServiceLifecycle, agent_app
from ai_agent_lib_core.integrations.mcp import META_REQUEST_ID
from ai_agent_lib_core.observability import configure_logging, redact
from ai_agent_lib_core.testing import (
    FakePolicyDecisionPoint,
    FakeRegistry,
    Fakes,
    FrozenClock,
    SequentialIds,
)
from ai_agent_lib_core.testing.oauth import FakeIdentityProvider

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1LTEifQ.c2lnbmF0dXJlLWJ5dGVz"


class Logs:
    """Everything a service wrote to its log, line by line."""

    def __init__(self, level: int | str = logging.INFO) -> None:
        self._stream = io.StringIO()
        self.handler = configure_logging("accounts-agent", level=level, stream=self._stream)

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self._stream.getvalue().splitlines()]

    def text(self) -> str:
        return self._stream.getvalue()


@pytest.fixture
def logs() -> Iterator[Logs]:
    root = logging.getLogger()
    before = (root.level, list(root.handlers))
    captured = Logs()
    yield captured
    root.removeHandler(captured.handler)
    root.setLevel(before[0])
    for handler in before[1]:
        if handler not in root.handlers:
            root.addHandler(handler)


def test_a_line_is_one_json_object_with_the_service_and_plain_extras(logs: Logs) -> None:
    logging.getLogger("ai_agent_lib_core.demo").info(
        "listening", extra={"address": "0.0.0.0:8000", "attempt": 2, "objects": [1, 2]}
    )
    (line,) = logs.lines()
    assert line["level"] == "INFO"
    assert line["logger"] == "ai_agent_lib_core.demo"
    assert (line["message"], line["service"]) == ("listening", "accounts-agent")
    assert (line["address"], line["attempt"], line["objects"]) == ("0.0.0.0:8000", 2, "[1, 2]")
    assert line["time"].endswith("+00:00")
    assert "request_id" not in line


def test_inside_a_request_every_line_carries_its_id(logs: Logs) -> None:
    context = RequestContext(
        principal=Principal(subject="u-1", tenant="t-1"),
        application="accounts-agent",
        request_id="r-42",
        thread_id="th-1",
    )
    with bind_request_context(context):
        logging.getLogger("ai_agent_lib_core.demo").info("working")
    (line,) = logs.lines()
    assert (line["request_id"], line["application"]) == ("r-42", "accounts-agent")
    # Who the caller is belongs in the audit log, not here.
    assert "u-1" not in logs.text()


@pytest.mark.parametrize(
    "secret",
    [
        f"Authorization: Bearer {JWT}",
        f"token={JWT}",
        f"the header was {JWT} today",
        'payload {"client_secret": "s3cr3t-value-123", "scope": "x"}',
        "password=hunter2hunter2&user=ann",
        "key AKIAIOSFODNN7EXAMPLE was used",
        "api_key: sk-live-0123456789abcdef",
        "basic dXNlcjpwYXNzd29yZA==",
    ],
)
def test_anything_shaped_like_a_credential_is_replaced(logs: Logs, secret: str) -> None:
    logging.getLogger("ai_agent_lib_core.demo").warning("the call failed: %s", secret)
    logging.getLogger("ai_agent_lib_core.demo").warning("field", extra={"detail": secret})
    text = logs.text()
    for hidden in (JWT, "s3cr3t-value-123", "hunter2", "AKIAIOSFODNN7EXAMPLE",
                   "sk-live-0123456789abcdef", "dXNlcjpwYXNzd29yZA=="):  # fmt: skip
        assert hidden not in text
    assert "[redacted]" in text
    assert redact("nothing secret here: 3 rows in 12 ms") == "nothing secret here: 3 rows in 12 ms"


def test_an_error_is_logged_by_type_and_place_and_only_the_librarys_own_by_message(
    logs: Logs,
) -> None:
    log = logging.getLogger("ai_agent_lib_core.demo")
    try:
        raise ValueError("the customer's question was: where is account 4411?")
    except ValueError:
        log.exception("the run failed unexpectedly")
    try:
        raise ValidationFailed("parameter 'region' must be a string")
    except ValidationFailed:
        log.exception("the call was refused")
    foreign, own = logs.lines()
    assert foreign["error_type"] == "ValueError"
    assert foreign["error_at"][-1].endswith(
        "in "
        + test_an_error_is_logged_by_type_and_place_and_only_the_librarys_own_by_message.__name__
    )
    assert "error" not in foreign
    assert "4411" not in logs.text()
    assert (own["error_type"], own["error"]) == (
        "ValidationFailed",
        "parameter 'region' must be a string",
    )


def test_other_libraries_are_quiet_below_warning_and_long_messages_are_cut(logs: Logs) -> None:
    logging.getLogger("httpx").info("HTTP Request: POST https://api.example.test 200 OK")
    logging.getLogger("some.library").warning("x" * 5000)
    logging.getLogger("uvicorn.error").info(
        "Application startup complete.", extra={"color_message": "coloured"}
    )
    lines = logs.lines()
    assert [line["logger"] for line in lines] == ["some.library", "uvicorn.error"]
    assert "color_message" not in lines[1]
    assert len(lines[0]["message"]) == 2000


def test_configuring_again_replaces_the_handler_and_the_level_can_be_named() -> None:
    root = logging.getLogger()
    before = (root.level, list(root.handlers))
    try:
        first = Logs()
        second = Logs("DEBUG")
        logging.getLogger("ai_agent_lib_core.demo").debug("detail")
        logging.getLogger("some.library").info("still quiet")
        assert first.lines() == []
        assert [line["message"] for line in second.lines()] == ["detail"]
        assert first.handler not in root.handlers
        third = Logs("ERROR")
        logging.getLogger("some.library").warning("now quiet too")
        assert third.lines() == []
        with pytest.raises(ValueError, match="not a log level"):
            configure_logging("x", level="LOUD")
    finally:
        for handler in list(root.handlers):
            if handler not in before[1]:
                root.removeHandler(handler)
        root.setLevel(before[0])


# ----------------------------------------------------------- what the library logs


class Services:
    ids = SequentialIds()

    async def authenticate(
        self, credential: str | None, *, application: str, thread_id: str,
        request_id: str | None = None,
    ) -> RequestContext:  # fmt: skip
        if credential != "good":
            raise PolicyDenied("the caller is not known", reason_code="invalid_token")
        return RequestContext(
            principal=Principal(subject="ann", tenant="t-1"),
            application=application,
            request_id=request_id or "r-1",
            thread_id=thread_id,
        )


async def _ready() -> None:
    return None


async def test_each_request_is_one_line_and_a_bug_is_logged_without_its_text(logs: Logs) -> None:
    async def run(context: RequestContext, given: Any) -> Any:
        logging.getLogger("ai_agent_lib_core.demo").info("inside the run")
        if given == "bug":
            raise RuntimeError("the question was: what is Ann's balance?")
        return {"ok": True}

    lifecycle = ServiceLifecycle(_ready)
    await lifecycle.start()
    app = agent_app(Services(), run, application="accounts-agent", lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
        good = {"authorization": "Bearer good", "x-request-id": "r-7"}
        ok = await http.post("/invoke", json={"input": "a private question"}, headers=good)
        bug = await http.post("/invoke", json={"input": "bug"}, headers=good)
        refused = await http.post(
            "/invoke", json={"input": "x"}, headers={"authorization": "Bearer bad"}
        )

    assert (ok.status_code, bug.status_code, refused.status_code) == (200, 500, 401)
    assert bug.json() == {"error": "internal", "request_id": "r-7"}
    lines = logs.lines()
    inside = lines[0]
    assert (inside["message"], inside["request_id"]) == ("inside the run", "r-7")
    requests = [line for line in lines if line["message"] == "request"]
    assert [(r["status"], r["level"], r["request_id"]) for r in requests] == [
        (200, "INFO", "r-7"),
        (500, "ERROR", "r-7"),
        (401, "INFO", None),
    ]
    assert all(r["route"] == "/invoke" and r["duration_ms"] >= 0 for r in requests)
    failure = next(line for line in lines if line["message"] == "the run failed unexpectedly")
    assert failure["error_type"] == "RuntimeError"
    text = logs.text()
    for content in ("a private question", "Ann's balance", "Bearer good", "Bearer bad"):
        assert content not in text


async def test_each_tool_call_of_a_server_is_one_line_without_arguments_or_results(
    logs: Logs,
) -> None:
    entry = ServerEntry(
        id="notes",
        owner="tests",
        url="http://127.0.0.1:1/mcp",
        audience="notes-mcp",
        tools=tuple(
            ToolEntry(name=f"notes.{name}", version="1", classification=Classification.INTERNAL)
            for name in ("read", "locked", "broken", "buggy")
        ),
    )
    clock = FrozenClock()
    fakes = Fakes(
        clock=clock,
        # A caller without a token is the development identity.
        identity=StaticIdentityVerifier(StaticIdentityOptions(audience="notes-mcp"), clock),
        registry=FakeRegistry(servers=[entry]),
        policy=FakePolicyDecisionPoint(
            lambda request: "not_yours" if request.resource.name.endswith("locked") else True
        ),
    )
    async with fakes.container() as services:
        server: MCPServer[Any] = MCPServer(
            "notes-mcp", middleware=[services.mcp_middleware("notes", application="notes-mcp")]
        )

        @server.tool(name="notes.read")
        async def read(title: str) -> str:
            return f"the private note called {title}"

        @server.tool(name="notes.locked")
        async def locked(title: str) -> str:
            return "never reached"

        @server.tool(name="notes.broken")
        async def broken(title: str) -> str:
            raise ValidationFailed("there is no such note")

        @server.tool(name="notes.buggy")
        async def buggy(title: str) -> str:
            raise RuntimeError(f"could not open {title}")

        async with Client(server) as client:
            meta: Any = {META_REQUEST_ID: "r-9"}
            for tool in ("notes.read", "notes.locked", "notes.broken", "notes.buggy"):
                await client.call_tool(tool, {"title": "salary review"}, meta=meta)
            await client.call_tool("x" * 200, {"title": "salary review"}, meta=meta)

    calls = [line for line in logs.lines() if line["message"] == "tool_call"]
    assert [(c["tool"], c["outcome"], c["level"]) for c in calls] == [
        ("notes.read", "success", "INFO"),
        ("notes.locked", "denied", "INFO"),
        # The tool's own code raised: the line says so, and with details on, what it said.
        ("notes.broken", "tool_error", "WARNING"),
        ("notes.buggy", "tool_error", "WARNING"),
        # A name that is not shaped like a tool's name could be content: it is not logged.
        ("unnamed", "denied", "INFO"),
    ]
    assert all(c["request_id"] == "r-9" and c["duration_ms"] >= 0 for c in calls)
    assert all(c["service"] == "accounts-agent" for c in calls)
    for content in ("salary review", "private note", "xxxx"):
        assert content not in logs.text()


async def test_a_failed_refresh_of_the_signing_keys_is_logged(logs: Logs) -> None:
    clock = FrozenClock()
    idp = FakeIdentityProvider(clock=clock)
    settings = resolve_jwt_options(JwtIdentityOptions.model_validate(idp.options("api://agent")))
    identity = JwtIdentityVerifier(settings, clock, transport=idp.transport)
    await identity.verify(idp.user_token(audience="api://agent", subject="u-1"))
    assert logs.lines() == []

    # A day later the keys are due again and the issuer cannot be reached.
    idp.available = False
    clock.advance(86_401)
    token = idp.user_token(audience="api://agent", subject="u-1")
    await identity.verify(token)  # the keys already held stay in use
    (line,) = logs.lines()
    assert (line["level"], line["message"]) == (
        "WARNING",
        "the signing keys could not be refreshed",
    )
    assert line["keys_held"] == 1
    assert "signing keys" in line["reason"]
    assert token not in logs.text()
    await identity.aclose()


# ------------------------------------------------- errors tell the developer why


async def test_a_run_that_ends_with_a_library_error_logs_what_was_expected(logs: Logs) -> None:
    async def run(context: RequestContext, given: Any) -> Any:
        if given == "bug":
            raise RuntimeError("the question was: what is Ann's balance?")
        raise ValidationFailed(
            "the reply does not match the schema",
            expected="category to be one of 'billing', 'fraud'",
            actual="text of 6 characters",
            detail="category was 'refund'",
        )

    lifecycle = ServiceLifecycle(_ready)
    await lifecycle.start()
    app = agent_app(Services(), run, application="accounts-agent", lifecycle=lifecycle)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as http:
        good = {"authorization": "Bearer good", "x-request-id": "r-8"}
        invalid = await http.post("/invoke", json={"input": "x"}, headers=good)
        await http.post("/invoke", json={"input": "bug"}, headers=good)
        await http.post("/invoke", json={"input": "x"}, headers={"authorization": "Bearer bad"})

    assert invalid.status_code == 400
    lines = logs.lines()
    ended = next(line for line in lines if line["message"] == "the run ended with ValidationFailed")
    assert ended["level"] == "WARNING"
    assert ended["error_expected"] == "category to be one of 'billing', 'fraud'"
    assert ended["error_actual"] == "text of 6 characters"
    assert ended["request_id"] == "r-8"
    # The line of the run's own code that raised is named.
    assert ended["error_in_your_code"].endswith("in run")
    bug = next(line for line in lines if line["message"] == "the run failed unexpectedly")
    assert bug["error_in_your_code"].endswith("in run")
    refused = next(line for line in lines if line["message"] == "the caller was refused at sign-in")
    assert (refused["reason_code"], refused["error_reason"]) == ("invalid_token", "invalid_token")
    for content in ("refund", "Ann's balance"):
        assert content not in logs.text()


async def test_with_details_on_a_developer_sees_what_a_tool_raised() -> None:
    stream = io.StringIO()
    handler = configure_logging("notes-mcp", stream=stream, details=True)
    try:
        entry = ServerEntry(
            id="notes",
            owner="tests",
            url="http://127.0.0.1:1/mcp",
            audience="notes-mcp",
            tools=(
                ToolEntry(name="notes.buggy", version="1", classification=Classification.INTERNAL),
            ),
        )
        clock = FrozenClock()
        fakes = Fakes(
            clock=clock,
            identity=StaticIdentityVerifier(StaticIdentityOptions(audience="notes-mcp"), clock),
            registry=FakeRegistry(servers=[entry]),
        )
        async with fakes.container() as services:
            server: MCPServer[Any] = MCPServer(
                "notes-mcp", middleware=[services.mcp_middleware("notes", application="notes-mcp")]
            )

            @server.tool(name="notes.buggy")
            async def buggy(title: str) -> str:
                raise KeyError(title)

            async with Client(server) as client:
                await client.call_tool("notes.buggy", {"title": "minutes"})
    finally:
        logging.getLogger().removeHandler(handler)
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    (call,) = [line for line in lines if line["message"] == "tool_call"]
    assert (call["outcome"], call["level"]) == ("tool_error", "WARNING")
    assert call["detail"] == "Error executing tool notes.buggy"
    # The MCP SDK logs the exception itself; the line names its cause and the tool's code.
    (raised,) = [line for line in lines if "raised an unexpected exception" in line["message"]]
    assert raised["error_causes"][0].startswith("KeyError: 'minutes'")
    assert raised["error_in_your_code"].endswith("in buggy")


async def test_a_governed_tool_that_raises_is_logged_with_the_line_that_raised(
    logs: Logs,
) -> None:
    def lookup(account: str) -> str:
        """Return the balance of an account."""
        raise LookupError("the ledger is closed")

    fakes = Fakes()
    async with fakes.container() as services:
        (tool,) = services.tools([lookup])
        context = RequestContext(
            principal=Principal(subject="u-1", tenant="t-1"),
            application="accounts-agent",
            request_id="r-3",
            thread_id="th-1",
        )
        with bind_request_context(context), pytest.raises(LookupError):
            await tool.ainvoke({"account": "4411"})

    (line,) = [line for line in logs.lines() if line["message"].startswith("the tool lookup")]
    assert line["message"] == "the tool lookup raised LookupError"
    assert line["level"] == "WARNING"
    assert line["tool"] == "lookup"
    assert line["error_in_your_code"].endswith("in lookup")
    # Its message may hold content: only with details on.
    assert "the ledger is closed" not in logs.text()
