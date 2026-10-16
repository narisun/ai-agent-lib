"""Recognize browser pages and constrain post-login navigation to the local UI."""

import re
from urllib.parse import urlsplit


def is_page_path(path: str) -> bool:
    """Identify document routes without redirecting API, asset or socket requests."""
    return path in {"/", "/chat", "/chat/", "/catalog", "/conversations", "/logs"} or bool(
        re.fullmatch(r"/chat/thread/[A-Za-z0-9_-]{1,200}", path)
        or re.fullmatch(
            r"/conversations/[A-Za-z0-9_-]{1,200}/turns/[A-Za-z0-9_-]{1,200}/inspect", path
        )
    )


def login_destination(value: str) -> str:
    """Accept a local UI page only; never turn a login form into an open redirect."""
    if (
        len(value) > 2048
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return "/chat/"
    target = urlsplit(value)
    return (
        value if not target.netloc and not target.scheme and is_page_path(target.path) else "/chat/"
    )
