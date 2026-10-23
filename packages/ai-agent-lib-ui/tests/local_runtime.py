"""Compose a complete local Runtime in a temporary folder for in-process tests."""

import json
from pathlib import Path

from ai_agent_lib_ui.adapters.catalog import pin_bindings, read_registries
from ai_agent_lib_ui.config.settings import LocalIdentity, Settings
from ai_agent_lib_ui.integrations.runtime import Runtime
from ai_agent_lib_ui.integrations.security import session_token

AGENT_URL = "http://127.0.0.1:8199"


def settings_at(path: Path, *agents: str) -> Settings:
    """Write a two-agent file registry and pin both agents for a loopback UI."""
    names = agents or ("memo", "helper")
    agents_path, tools_path = path / "agents.json", path / "tools.json"
    agents_path.write_text(
        json.dumps(
            {
                "schema": "agentcraft2.registry/v1",
                "agents": [
                    {"id": name, "owner": "team", "version": "1", "url": f"{AGENT_URL}/{name}"}
                    for name in names
                ],
            }
        ),
        encoding="utf-8",
    )
    tools_path.write_text('{"schema":"agentcraft2.registry/v1","servers":[]}', encoding="utf-8")
    snapshot, _ = read_registries(agents_path, tools_path)
    urls = frozenset(f"{AGENT_URL}/{name}" for name in names)
    return Settings(
        agents_path=agents_path,
        tools_path=tools_path,
        identity=LocalIdentity(),
        allowed_urls=urls,
        bindings=pin_bindings(snapshot, names, urls),
    )


async def sign_in(resources: Runtime) -> str:
    """Create a parent session and bind it to the current task, as the boundary does."""
    token = await resources.store.new_session(resources.identity)
    session_token.set(token)
    return token
