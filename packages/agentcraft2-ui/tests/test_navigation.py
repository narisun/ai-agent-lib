"""Login destinations stay within the local UI's document routes."""

import pytest

from agentcraft2_ui.integrations.navigation import is_page_path, login_destination


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/chat",
        "/chat/",
        "/catalog",
        "/conversations",
        "/chat/thread/acaa6a84-a19a-446d-a7cf-1375b1e9c088",
    ],
)
def test_document_routes_are_recognized(path: str) -> None:
    assert is_page_path(path)
    assert login_destination(path) == path


@pytest.mark.parametrize(
    "value",
    [
        "https://example.com/",
        "//example.com/chat/",
        "/\\example.com/",
        "\\\\example.com",
        "/chat/\r\nLocation: https://example.com",
        "/chat/\x00",
        " /chat/",
        "/%2Fexample.com/",
        "/chat/../login",
        "/chat/thread/../../api/threads",
        "/login",
        "/api/threads",
        "/chat/threads",
        "/chat/project/settings",
        "/chat/public/chat.css",
        "/chat/auth/header",
        "/chat/ws/socket.io",
        "/chat/thread/",
        "/conversations?search=" + "x" * 2048,
    ],
)
def test_external_or_non_page_destinations_fall_back_to_chat(value: str) -> None:
    assert login_destination(value) == "/chat/"


def test_conversation_search_is_preserved() -> None:
    target = "/conversations?search=Canada+Goose&archived=false"
    assert login_destination(target) == target
