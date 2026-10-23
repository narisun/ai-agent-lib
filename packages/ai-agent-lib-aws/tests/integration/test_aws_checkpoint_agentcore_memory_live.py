"""The AgentCore Memory checkpoint store against a real memory.

Create a memory in AgentCore (short-term memory is enough), sign in to an
account whose role may create, list, get and delete its events, and name it::

    set AGENTCORE_MEMORY_ID=<memory id>
    set AWS_REGION=<the memory's region>
    python -m pytest -m integration packages/ai-agent-lib-aws/tests/integration

The contract uses fixed tenants and subjects, so the threads they would find
from an earlier run are deleted before each test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ai_agent_lib_aws.checkpoint_agentcore_memory import (
    AgentCoreMemoryCheckpointBackend,
    AgentCoreMemoryCheckpointOptions,
)
from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import CheckpointBackend, Scope
from ai_agent_lib_core.testing.langgraph_contracts import CheckpointBackendContract

pytestmark = pytest.mark.integration

MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID")
# The scopes the contract suite writes under; see its _context() helper.
SCOPES = [
    Scope("t-1", "u-1", "app-1"),
    Scope("t-2", "u-1", "app-1"),
    Scope("t-1", "u-2", "app-1"),
    Scope("t-1", "u-1", "app-2"),
    Scope("t-1/u-1", "x", "app-1"),
]
THREAD = "shared-thread-name"


class TestAgentCoreMemoryLive(CheckpointBackendContract):
    """The shipped contract, over the real memory checkpointer."""

    async def make_backend(self, tmp_path: Path) -> CheckpointBackend:
        if not MEMORY_ID:
            pytest.skip("set AGENTCORE_MEMORY_ID to run against a real memory")
        sessions = AwsSessionFactory(
            profile=os.environ.get("AWS_PROFILE"), region=os.environ.get("AWS_REGION")
        )
        options = AgentCoreMemoryCheckpointOptions(memory_id=MEMORY_ID)
        backend = AgentCoreMemoryCheckpointBackend(options, sessions)
        await backend.start()
        await backend.validate()
        for scope in SCOPES:
            await backend.checkpointer.adelete_thread(scope.key("thread", THREAD))
        return backend
