"""Governed tools: native, audited and identity-bound."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.tools import BaseTool, tool

from ai_agent_lib_core.contracts import AuditOutcome, PolicyDenied, Principal, RequestContext
from ai_agent_lib_core.integrations.langgraph import GovernedTool
from ai_agent_lib_core.pipeline import bind_request_context, frame_untrusted
from ai_agent_lib_core.testing import Fakes

CONTEXT = RequestContext(
    principal=Principal(subject="u-1", tenant="t-9"),
    application="accounts-agent",
    request_id="r-1",
    thread_id="th-1",
)


@tool
async def lookup_account(account: str, include_closed: bool = False) -> str:
    """Look up the balance of an account."""
    return f"balance of {account}: 100 (closed={include_closed})"


def close_account(account: str) -> str:
    """Close an account."""
    return f"closed {account}"


async def test_a_governed_tool_looks_the_same_to_a_model() -> None:
    async with Fakes().container() as services:
        (governed,) = services.tools([lookup_account])
    assert isinstance(governed, BaseTool)
    assert isinstance(governed, GovernedTool)
    assert governed.name == lookup_account.name
    assert governed.description == lookup_account.description
    assert governed.args == lookup_account.args
    assert governed.get_input_jsonschema() == lookup_account.get_input_jsonschema()


async def test_a_tool_call_runs_and_is_audited_without_its_arguments() -> None:
    fakes = Fakes()
    async with fakes.container() as services:
        (governed,) = services.tools([lookup_account], read_only=["lookup_account"])
        with bind_request_context(CONTEXT):
            result = await governed.ainvoke({"account": "12345678", "include_closed": True})

    assert result == frame_untrusted("lookup_account", "balance of 12345678: 100 (closed=True)")
    (record,) = fakes.audit.records
    assert record.event == "tool.call"
    assert record.outcome is AuditOutcome.SUCCESS
    assert record.attributes["tool"] == "lookup_account"
    assert record.attributes["read_only"] is True
    assert (record.tenant, record.subject) == ("t-9", "u-1")
    assert "12345678" not in str(record.to_dict())


async def test_a_tool_call_without_a_request_context_is_denied_before_the_tool_runs() -> None:
    ran: list[str] = []

    def side_effect(account: str) -> str:
        """Do something that must not happen."""
        ran.append(account)
        return "done"

    fakes = Fakes()
    async with fakes.container() as services:
        (governed,) = services.tools([side_effect])
        with pytest.raises(PolicyDenied) as caught:
            await governed.ainvoke({"account": "1"})
    assert caught.value.reason_code == "identity_missing"
    assert ran == []
    assert fakes.audit.records[0].outcome is AuditOutcome.DENIED


async def test_plain_functions_are_converted_and_tools_default_to_not_read_only() -> None:
    fakes = Fakes()
    async with fakes.container() as services:
        (governed,) = services.tools([close_account])
        with bind_request_context(CONTEXT):
            assert await governed.ainvoke({"account": "7"}) == frame_untrusted(
                "close_account", "closed 7"
            )
    assert governed.name == "close_account"
    assert fakes.audit.records[0].attributes["read_only"] is False


async def test_invalid_arguments_are_rejected_by_the_schema() -> None:
    async with Fakes().container() as services:
        (governed,) = services.tools([lookup_account])
        with bind_request_context(CONTEXT), pytest.raises(ValueError, match="account"):
            await governed.ainvoke({"wrong": "field"})


async def test_tool_errors_propagate_and_are_audited_as_failed() -> None:
    def broken(account: str) -> str:
        """Fail."""
        raise LookupError("no such account")

    fakes = Fakes()
    async with fakes.container() as services:
        (governed,) = services.tools([broken])
        with bind_request_context(CONTEXT), pytest.raises(LookupError):
            await governed.ainvoke({"account": "404"})
    assert fakes.audit.records[0].outcome is AuditOutcome.FAILED
    assert fakes.audit.records[0].error_type == "LookupError"


async def test_the_synchronous_api_works_from_a_worker_thread() -> None:
    fakes = Fakes()
    async with fakes.container() as services:
        (governed,) = services.tools([close_account])

        def call_in_thread() -> str:
            with bind_request_context(CONTEXT):
                return str(governed.invoke({"account": "9"}))

        assert await asyncio.to_thread(call_in_thread) == frame_untrusted(
            "close_account", "closed 9"
        )


async def test_tool_lists_are_validated() -> None:
    async with Fakes().container() as services:
        with pytest.raises(ValueError, match="two tools are named 'close_account'"):
            services.tools([close_account, close_account])
        with pytest.raises(ValueError, match="not given"):
            services.tools([close_account], read_only=["lookup_account"])
        governed = services.tools([close_account])
        with pytest.raises(ValueError, match="already governed"):
            services.tools(governed)
