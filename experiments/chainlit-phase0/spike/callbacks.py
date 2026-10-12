"""Thin Chainlit callbacks: existing HTTP agents remain outside this process."""

import chainlit as cl
from fastapi import HTTPException

from spike.data_layer import DataLayer
from spike.runtime import runtime
from spike.security import current_owner, session_token
from spike.settings import catalog


@cl.header_auth_callback
def authenticate(headers):
    """Use only the identity injected by the parent session boundary."""
    owner = current_owner(runtime().store)
    if headers.get("x-phase0-user") != owner:
        return None
    return cl.User(identifier=owner, display_name=owner, metadata={"provider": "header"})


@cl.data_layer
def data_layer():
    """Use the supported data-layer factory, never a patched Chainlit singleton."""
    return DataLayer()


@cl.on_logout
async def logout(_request, response):
    """End the parent session when the user signs out through the chat menu."""
    runtime().store.revoke(session_token.get())
    response.delete_cookie("phase0_session")
    return {"success": True}


@cl.set_chat_profiles
async def profiles(current_user):
    """Build agent choices from a checked registry fixture."""
    if current_user.identifier != current_owner(runtime().store):
        raise HTTPException(401, "Identity mismatch")
    return [
        cl.ChatProfile(name=e["id"], markdown_description=e["description"], default=i == 0)
        for i, e in enumerate(catalog())
    ]


@cl.on_chat_start
async def start():
    """Create one application-owned thread before any messages are persisted."""
    owner = current_owner(runtime().store)
    profile = cl.user_session.get("chat_profile") or "rmprep-demo"
    if profile not in {entry["id"] for entry in catalog()}:
        raise HTTPException(400, "Unknown agent")
    runtime().store.update_thread(
        owner, cl.context.session.thread_id, metadata={"chat_profile": profile}
    )


@cl.on_chat_resume
async def resume(thread):
    """Restore persisted messages and reattach to existing work, without HTTP replay."""
    owner = current_owner(runtime().store)
    await runtime().runs.wait(owner, thread["id"])
    thread.update(runtime().store.thread(owner, thread["id"]))


@cl.on_message
async def respond(message: cl.Message):
    """Pass only the new question to the chosen HTTP agent."""
    owner = current_owner(runtime().store)
    thread_id = cl.context.session.thread_id
    run = await runtime().runs.submit(owner, thread_id, message.id, message.content)
    if run["status"] != "completed":
        await cl.Message(
            content=f"The outcome is unknown. [Inspect this run](/inspector/{run['id']}). "
            "This thread is blocked until the outcome is resolved."
        ).send()
        return
    result = run["result"]
    memo = cl.Text(
        id=result["memo_id"],
        name=result["memo_name"],
        url=f"/artifacts/{run['id']}",
        display="side",
    )
    metrics = cl.CustomElement(
        id=result["metrics_id"],
        name="Metrics",
        props={
            **result["metrics"],
            "elapsed_seconds": result["elapsed_seconds"],
            "run_id": run["id"],
        },
    )
    await cl.Message(
        id=result["message_id"], content=result["answer"], elements=[memo, metrics]
    ).send()
    await cl.ElementSidebar.set_title("Preparation memo · synthetic example")
    await cl.ElementSidebar.set_elements([memo])
