"""Tests that prove the shared quality gates are switched on."""

from __future__ import annotations

import re
import socket
from pathlib import Path

import pytest
from pytest_socket import SocketConnectBlockedError


def test_network_sockets_are_blocked_in_tests() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection,
        pytest.raises(SocketConnectBlockedError),
    ):
        connection.connect(("192.0.2.1", 443))


def test_all_three_packages_import() -> None:
    import ai_agent_lib_aws
    import ai_agent_lib_cli
    import ai_agent_lib_core

    assert ai_agent_lib_core.__doc__
    assert ai_agent_lib_aws.__doc__
    assert ai_agent_lib_cli.__doc__


def test_ci_tests_the_policy_with_the_opa_version_that_is_deployed() -> None:
    root = Path(__file__).parents[2]
    dockerfile = (
        root
        / "packages/ai-agent-lib-cli/src/ai_agent_lib_cli/templates/deploy-opa/Dockerfile.jinja"
    ).read_text(encoding="utf-8")
    deployed = re.search(r"openpolicyagent/opa:(\d+\.\d+\.\d+)", dockerfile)
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    tested = re.search(r'setup-opa@v\d+\s+with:\s+version: "(\d+\.\d+\.\d+)"', workflow)
    assert deployed
    assert tested
    assert tested.group(1) == deployed.group(1)
