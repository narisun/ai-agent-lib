"""Thin supported Chainlit hooks over the framework-independent chat service."""

import logging
from datetime import UTC, datetime

import chainlit as cl
from chainlit.types import ThreadDict
from fastapi import Request, Response
from starlette.datastructures import Headers

from ai_agent_lib_ui.application.progress import Draft
from ai_agent_lib_ui.contracts.queries import ContractError, PageRequest
from ai_agent_lib_ui.integrations.data_layer import (
    DataLayer,
    answer_id,
    outcome_text,
)
from ai_agent_lib_ui.integrations.runtime import runtime
from ai_agent_lib_ui.integrations.security import current_principal, session_token


@cl.header_auth_callback
async def authenticate(headers: Headers) -> cl.User:
    """The parent session establishes identity; caller-supplied headers grant nothing."""
    principal = current_principal()
    return cl.User(
        identifier=runtime().identity,
        display_name=principal.subject,
        metadata={"provider": "header"},
    )


@cl.data_layer
def data_layer() -> DataLayer:
    """Supply the application-owned persistence adapter."""
    return DataLayer()


@cl.on_logout
async def logout(request: Request, response: Response) -> dict[str, bool]:
    """Revoke the parent session, including future operations on existing sockets."""
    runtime().store.revoke(session_token.get())
    response.delete_cookie("agentlib_session")
    return {"success": True}


@cl.set_chat_profiles
async def profiles(
    current_user: cl.User | None, language: str | None = None
) -> list[cl.ChatProfile]:
    """Show registered agents permitted for the verified local identity."""
    principal = current_principal()
    if current_user is None or current_user.identifier != runtime().identity:
        return []
    page = await runtime().catalog.agents(principal, PageRequest(limit=200))
    return [
        cl.ChatProfile(
            name=binding.manifest.agent_id,
            icon="/ui/agent-lab.svg",
            markdown_description=(
                "## Welcome to Agent Lab\n\n"
                "Test your agents, explore their answers, and inspect how they work.\n\n"
                f"Selected agent: **{binding.manifest.title}**. "
                "Type a question below, or [choose another agent](/catalog) to start a new chat. "
                "Open **Memos** for saved documents and **Logs** for model input/output, "
                "MCP calls, and checkpoints."
            ),
            default=i == 0,
        )
        for i, binding in enumerate(page.items)
    ]


@cl.on_chat_start
async def start() -> None:
    """Persist selection before accepting messages on a new thread."""
    principal = current_principal()
    profile = (
        cl.user_session.get("chat_profile") or runtime().settings.bindings[0].manifest.agent_id
    )
    await runtime().chat.create(principal, str(profile), thread_id=cl.context.session.thread_id)


@cl.on_chat_resume
async def resume(thread: ThreadDict) -> None:
    """Reattach to an admitted request and restore its saved outcome without replay."""
    await runtime().chat.reattach(current_principal(), thread["id"])
    restored = await DataLayer().get_thread(thread["id"])
    if restored:
        thread.update(restored)


@cl.on_message
async def respond(message: cl.Message) -> None:
    """Display provisional text, then remove it when the saved outcome is ready."""
    principal = current_principal()
    draft_step: cl.Step | None = None
    displayed_call = 0

    async def show_draft(draft: Draft) -> None:
        nonlocal draft_step, displayed_call
        current_principal()
        if draft_step is None:
            draft_step = cl.Step(
                name=f"Live draft (model call {draft.call}; provisional)",
                type="tool",
                default_open=True,
                auto_collapse=True,
                parent_id=message.id,
            )
            draft_step.start = datetime.now(UTC).isoformat()
            draft_step.output = "Waiting for model text…"
            await draft_step.send()
        elif draft.call != displayed_call:
            draft_step.name = f"Live draft (model call {draft.call}; provisional)"
            draft_step.output = "Waiting for model text…"
            await draft_step.update()
        displayed_call = draft.call
        await draft_step.stream_token(draft.text or "Waiting for model text…", is_sequence=True)

    async def remove_draft() -> None:
        if draft_step is not None:
            current_principal()
            # This step exists only in the browser. Delete its presentation
            # through Chainlit's emitter, not the immutable transcript API.
            await cl.context.emitter.delete_step(draft_step.to_dict())

    try:
        if message.thread_id != cl.context.session.thread_id:
            await cl.Message(content="This message belongs to another thread.").send()
            return
        run = await runtime().chat.submit(
            principal,
            cl.context.session.thread_id,
            message.id,
            message.content,
            on_progress=show_draft,
        )
    except ContractError as error:
        await cl.Message(
            content=f"The message was not accepted ({error.code.value}). "
            "Reopen the conversation to see its saved state."
        ).send()
        return
    except Exception:  # noqa: BLE001 - browser errors must not disclose service content
        logging.getLogger(__name__).error("UI operation failed; reopen the durable conversation")
        await cl.Message(
            content="The request could not be saved. Reopen this conversation "
            "to check its state before sending another question."
        ).send()
        return
    finally:
        await remove_draft()
    current_principal()
    await cl.Message(id=answer_id(run), content=outcome_text(run)).send()
