"""Explicit local configuration, prepared before importing Chainlit."""

import json
import os
import secrets
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def private_value(path: Path) -> str:
    """Create a local random value once, preserving it across restarts."""
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


@dataclass(frozen=True)
class Settings:
    """Only loopback services and synthetic fixture identities are supported."""

    state: Path
    origin: str
    auth_secret: str
    access_code: str
    agent_key: str
    delay: float = 2.0

    @classmethod
    def local(cls, state: Path | None = None) -> "Settings":
        """Read the experiment's state directory and initialize local keys."""
        state = state or ROOT / ".agentlib"
        state.mkdir(parents=True, exist_ok=True)
        return cls(
            state,
            "http://127.0.0.1:8765",
            private_value(state / "auth-secret.txt"),
            private_value(state / "access-code.txt"),
            private_value(state / "agent-key.txt"),
        )

    def prepare_chainlit(self) -> None:
        """Select local assets/auth and refuse accidental external trace credentials."""
        if os.environ.get("LITERAL_API_KEY"):
            raise ValueError("Run the isolated sample without LITERAL_API_KEY.")
        os.environ["CHAINLIT_APP_ROOT"] = str(ROOT)
        os.environ["CHAINLIT_AUTH_SECRET"] = self.auth_secret
        os.environ["CHAINLIT_AUTH_COOKIE_NAME"] = "phase0_chainlit"
        os.environ["CHAINLIT_COOKIE_SAMESITE"] = "strict"


def catalog() -> list[dict]:
    """Read the fixture registry, rejecting destinations outside this sample."""
    document = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
    if document["schema"] != "agentlib.registry/v1":
        raise ValueError("Unsupported fixture registry")
    entries = document["agents"]
    for entry in entries:
        expected = f"http://127.0.0.1:8765/fixture/{entry['id']}"
        if entry["url"] != expected or entry["status"] != "active":
            raise ValueError("Phase 0 permits only its own active scripted endpoints")
    return entries
