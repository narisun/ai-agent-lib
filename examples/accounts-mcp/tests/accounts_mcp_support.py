"""What the offline and the live tests of the reference server share."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from accounts_mcp import APPLICATION
from ai_agent_lib_core.adapters import FakeChatModelProvider
from ai_agent_lib_core.config import ConfigResolver, MappingConfigSource
from ai_agent_lib_core.contracts import ServiceConfig
from ai_agent_lib_core.di import MODEL_PORT, BuildContext, ServiceProviders

__all__ = ["HERE", "REGISTRY", "agent_config", "records", "scripted", "server_config"]

HERE = Path(__file__).parents[1]
AGENT_RULES = HERE.parent / "accounts-agent" / "policies" / "agentlib" / "rules" / "data.yaml"
REGISTRY = {
    "agents_path": str(HERE / "registry" / "agents.yaml"),
    "tools_path": str(HERE / "registry" / "mcp-tools.yaml"),
}


def server_config(tmp_path: Path, **overrides: str) -> ServiceConfig:
    """The server's configuration as its .env gives it, with local state in a temporary folder."""
    source = {
        "EAP_DATA_SOURCES": json.dumps(
            {
                "ledger": {
                    "kind": "duckdb_csv",
                    "data_dir": str(HERE / "data"),
                    "queries_dir": str(HERE / "queries"),
                }
            }
        ),
        "EAP_IDENTITY_OPTIONS": json.dumps({"audience": APPLICATION, "roles": ["analyst"]}),
        "EAP_POLICY_OPTIONS": json.dumps(
            {"path": str(HERE / "policies" / "agentlib" / "rules" / "data.yaml")}
        ),
        "EAP_REGISTRY_OPTIONS": json.dumps(REGISTRY),
        "EAP_AUDIT_OPTIONS": json.dumps({"path": str(tmp_path / "server-audit.jsonl")}),
        # The server compiles no graph, so it keeps no checkpoint store.
        "EAP_CHECKPOINT_PROVIDER": "none",
        **overrides,
    }
    return ConfigResolver(MappingConfigSource(source)).resolve()


def agent_config(tmp_path: Path, roles: list[str]) -> ServiceConfig:
    source = {
        "EAP_MODEL_PROVIDER": "fake",
        "EAP_MODEL_ID": "fake-model",
        "EAP_IDENTITY_OPTIONS": json.dumps({"subject": "u-7", "tenant": "acme", "roles": roles}),
        "EAP_POLICY_OPTIONS": json.dumps({"path": str(AGENT_RULES)}),
        "EAP_REGISTRY_OPTIONS": json.dumps(REGISTRY),
        "EAP_AUDIT_OPTIONS": json.dumps({"path": str(tmp_path / "agent-audit.jsonl")}),
        "EAP_CHECKPOINT_OPTIONS": json.dumps({"path": str(tmp_path / "agent-cp.sqlite")}),
    }
    return ConfigResolver(MappingConfigSource(source)).resolve()


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


def scripted(*replies: Any) -> ServiceProviders:
    """The local adapters, with a model that replays ``replies``."""

    def fake_model(context: BuildContext) -> FakeChatModelProvider:
        return FakeChatModelProvider(list(replies))

    return ServiceProviders.default().register(MODEL_PORT, "fake", fake_model, replace=True)
