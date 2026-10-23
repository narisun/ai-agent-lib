"""Load service-specific context limits at startup using the library's config sources."""

from __future__ import annotations

from pathlib import Path

from agentcraft2_core.config import DotenvConfigSource, EnvironConfigSource, LayeredConfigSource
from agentcraft2_core.contracts import ConfigSource, ConfigurationError

from rmprep.context import ContextBudget

__all__ = ["ENV_FILE", "load_context_budget"]

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
"""The service's ``.env`` file, beside ``pyproject.toml``.

A deployed service has no such file and is configured by its environment.
"""

_CONTEXT_SETTINGS = {
    "RMPREP_CONTEXT_INPUT_TOKENS": "input_tokens",
    "RMPREP_CONTEXT_QUESTION_TOKENS": "question_tokens",
    "RMPREP_CONTEXT_RECENT_TOKENS": "recent_tokens",
    "RMPREP_CONTEXT_TOOL_SCHEMA_TOKENS": "tool_schema_tokens",
}


def load_context_budget(
    dotenv_path: Path | None = ENV_FILE, *, source: ConfigSource | None = None
) -> ContextBudget:
    """Snapshot context settings; process variables override the agent's .env.

    Pass a ConfigSource for isolated tests or another configuration backend.
    Missing settings use ContextBudget defaults. Invalid or misspelled context
    settings fail startup, before any model calls. Reading never changes os.environ.
    """
    if source is None:
        layers: list[ConfigSource] = [EnvironConfigSource()]
        if dotenv_path is not None:
            layers.append(DotenvConfigSource(dotenv_path))
        source = LayeredConfigSource(*layers)
    unknown = sorted(
        name
        for name in source.names()
        if name.startswith("RMPREP_CONTEXT_") and name not in _CONTEXT_SETTINGS
    )
    if unknown:
        raise ConfigurationError("Unknown rmprep context setting: " + ", ".join(unknown))
    values: dict[str, int] = {}
    for name, field in _CONTEXT_SETTINGS.items():
        raw = source.get(name)
        if raw is None:
            continue
        if not raw.strip().isascii() or not raw.strip().isdecimal() or int(raw) <= 0:
            raise ConfigurationError(f"{name} must be a positive whole number of tokens")
        values[field] = int(raw)
    try:
        return ContextBudget(**values)
    except ValueError as error:
        raise ConfigurationError(f"Invalid RMPREP_CONTEXT_* settings: {error}") from None
