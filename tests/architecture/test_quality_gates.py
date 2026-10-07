"""Tests that prove the shared quality gates are switched on."""

from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketBlockedError


def test_network_sockets_are_blocked_in_tests() -> None:
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_all_three_packages_import() -> None:
    import ai_agent_lib_aws
    import ai_agent_lib_cli
    import ai_agent_lib_core

    assert ai_agent_lib_core.__doc__
    assert ai_agent_lib_aws.__doc__
    assert ai_agent_lib_cli.__doc__
