"""Prepare the local browser shell without importing an agent environment."""

import os
import secrets
from importlib.resources import files
from pathlib import Path

from ai_agent_lib_ui.config.settings import Settings


def private_secret(path: Path) -> str:
    """Keep generated local secrets stable across restarts without overwriting them."""
    try:
        with path.open("x", encoding="utf-8") as target:
            target.write(secrets.token_urlsafe(48))
    except FileExistsError:
        pass
    value = path.read_text(encoding="utf-8").strip()
    if len(value) < 32:
        raise ValueError(
            "local secret file is invalid; restore it or initialize a new state directory"
        )
    return value


def prepare_shell(state: Path, settings: Settings) -> tuple[str, str]:
    """Prepare writable framework assets before importing Chainlit."""
    if os.environ.get("LITERAL_API_KEY"):
        raise ValueError("unset LITERAL_API_KEY in this local UI environment")
    state.mkdir(parents=True, exist_ok=True)
    secret = private_secret(state / "session-secret")
    code = private_secret(state / "access-code")
    shell = state / "shell"
    (shell / ".chainlit").mkdir(parents=True, exist_ok=True)
    (shell / "public").mkdir(exist_ok=True)
    assets = files("ai_agent_lib_ui").joinpath("assets")
    config = assets.joinpath("chainlit.toml").read_text().replace("__ORIGIN__", settings.origin)
    (shell / ".chainlit/config.toml").write_text(config, encoding="utf-8")
    for name in ("chat.css", "conversations.svg"):
        (shell / "public" / name).write_bytes(assets.joinpath(name).read_bytes())
    (shell / "chainlit.md").write_text(
        "# Agent workspace\nChoose an agent, then ask a question. "
        "Previous conversations appear on the left.\n",
        encoding="utf-8",
    )
    # Chainlit otherwise reads the working directory's .env during import. Keep
    # agent model keys and unrelated framework settings outside this process.
    empty_environment = shell / "ui-empty.env"
    empty_environment.write_text("# Settings are supplied by ai-agent-lib-ui.\n", encoding="utf-8")
    os.environ.update(
        {
            "CHAINLIT_APP_ROOT": str(shell),
            "CHAINLIT_ENV_FILE": str(empty_environment.resolve()),
            "CHAINLIT_AUTH_SECRET": secret,
            "CHAINLIT_AUTH_COOKIE_NAME": "agentlib_chat",
            "CHAINLIT_COOKIE_SAMESITE": "strict",
        }
    )
    return secret, code
