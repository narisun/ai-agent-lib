"""The Chainlit data layer is exercised in process with an injected Runtime."""

import pytest
from fastapi import HTTPException
from local_runtime import sign_in

from agentcraft2_ui.contracts.queries import ContractError
from agentcraft2_ui.integrations.runtime import Runtime

# Chainlit's own imports trip Pydantic deprecation warnings; they are not ours.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


async def test_delete_thread_is_refused_and_points_at_the_conversations_page(
    runtime: Runtime,
) -> None:
    from agentcraft2_ui.integrations.data_layer import DataLayer

    principal = runtime.settings.identity.principal()
    thread = (await runtime.chat.create(principal, "memo")).thread.thread_id
    await sign_in(runtime)
    with pytest.raises(HTTPException) as refused:
        await DataLayer(runtime).delete_thread(thread)
    assert refused.value.status_code == 409
    assert "Conversations" in str(refused.value.detail)
    assert "Logs" not in str(refused.value.detail)
    assert (await runtime.store.get(principal, thread)).thread.thread_id == thread
    with pytest.raises(ContractError, match="not_found"):
        await DataLayer(runtime).delete_thread("missing")


async def test_update_thread_swallows_not_found_only_for_lifecycle_metadata(
    runtime: Runtime,
) -> None:
    from agentcraft2_ui.integrations.data_layer import DataLayer

    await sign_in(runtime)
    layer = DataLayer(runtime)
    # Chainlit writes metadata before on_chat_start creates the thread.
    await layer.update_thread("missing", metadata={"chat_profile": "memo"})
    with pytest.raises(ContractError, match="not_found"):
        await layer.update_thread("missing", name="Renamed")
    with pytest.raises(HTTPException) as refused:
        await layer.update_thread("missing", user_id="someone-else")
    assert refused.value.status_code == 404
    principal = runtime.settings.identity.principal()
    thread = (await runtime.chat.create(principal, "memo")).thread.thread_id
    await layer.update_thread(thread, name="Renamed", metadata={"chat_profile": "helper"})
    conversation = await runtime.store.get(principal, thread)
    assert conversation.thread.title == "Renamed"
    assert conversation.thread.scope.application == "memo"


async def test_list_threads_returns_owned_summaries_and_rejects_other_users(
    runtime: Runtime,
) -> None:
    from chainlit.types import Pagination, ThreadFilter

    from agentcraft2_ui.integrations.data_layer import DataLayer

    principal = runtime.settings.identity.principal()
    first = (await runtime.chat.create(principal, "memo")).thread.thread_id
    second = (await runtime.chat.create(principal, "helper")).thread.thread_id
    await sign_in(runtime)
    layer = DataLayer(runtime)
    page = await layer.list_threads(Pagination(first=20), ThreadFilter(userId=runtime.identity))
    assert {thread["id"] for thread in page.data} == {first, second}
    assert all(thread["userId"] == runtime.identity for thread in page.data)
    profiles = {(thread["metadata"] or {})["chat_profile"] for thread in page.data}
    assert profiles == {"memo", "helper"}
    assert page.pageInfo.hasNextPage is False
    one = await layer.list_threads(Pagination(first=1), ThreadFilter(search="helper"))
    assert [thread["id"] for thread in one.data] == [second]
    with pytest.raises(HTTPException) as refused:
        await layer.list_threads(Pagination(first=20), ThreadFilter(userId="someone-else"))
    assert refused.value.status_code == 404
    restored = await layer.get_thread(first)
    assert restored is not None
    assert restored["metadata"] == {"chat_profile": "memo"}
    assert restored["steps"] == []


async def test_user_lookups_need_the_parent_session_and_the_configured_identity(
    runtime: Runtime,
) -> None:
    from agentcraft2_ui.integrations.data_layer import DataLayer

    layer = DataLayer(runtime)
    with pytest.raises(HTTPException) as anonymous:
        await layer.get_user(runtime.identity)
    assert anonymous.value.status_code == 401
    await sign_in(runtime)
    with pytest.raises(HTTPException) as unknown:
        await layer.get_user("someone-else")
    assert unknown.value.status_code == 404
    user = await layer.get_user(runtime.identity)
    assert user is not None
    assert (user.identifier, user.display_name) == (runtime.identity, "dev-user")
    assert await layer.get_favorite_steps(runtime.identity) == []
    assert await layer.build_debug_url() == "/logs"
