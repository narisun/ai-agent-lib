"""Thin supported Chainlit hooks over the framework-independent chat service."""

import logging

import chainlit as cl
from chainlit.types import ThreadDict
from fastapi import Request, Response
from starlette.datastructures import Headers

from ai_agent_lib_ui.contracts.queries import ContractError, PageRequest
from ai_agent_lib_ui.integrations.data_layer import DataLayer, answer_id, outcome_text
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
    """Supply a supported adapter without patching framework internals."""
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
            markdown_description=f"Chat with {binding.manifest.title}",
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
    """Only the new question crosses the existing /invoke boundary."""
    principal = current_principal()
    try:
        if message.thread_id != cl.context.session.thread_id:
            await cl.Message(content="This message belongs to another thread.").send()
            return
        run = await runtime().chat.submit(
            principal, cl.context.session.thread_id, message.id, message.content
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
    current_principal()
    await cl.Message(id=answer_id(run), content=outcome_text(run)).send()
