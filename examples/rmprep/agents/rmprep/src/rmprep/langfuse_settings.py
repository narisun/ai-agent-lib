"""Compatibility imports for the original rmprep tracing settings."""

from pathlib import Path

from ai_agent_lib_core.config.langfuse import LangfuseSettings
from ai_agent_lib_core.config.langfuse import load_langfuse_settings as _load
from ai_agent_lib_core.contracts import ConfigSource

__all__ = ["LangfuseSettings", "load_langfuse_settings"]


def load_langfuse_settings(
    dotenv_path: Path, *, source: ConfigSource | None = None
) -> LangfuseSettings | None:
    """Accept the original trial variables as aliases for the shared settings."""
    return _load(dotenv_path, source=source, legacy_prefix="RMPREP")
