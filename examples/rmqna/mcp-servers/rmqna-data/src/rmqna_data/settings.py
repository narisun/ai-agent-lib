"""Where this service finds its local settings, its data and the schema document."""

from __future__ import annotations

from pathlib import Path

__all__ = ["DATA_DIR", "ENV_FILE", "SCHEMA_PATH"]

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
"""The service's ``.env`` file, beside ``pyproject.toml``.

A deployed service has no such file and is configured by its environment.
"""

DATA_DIR = (ENV_FILE.parent / "../../data").resolve()
"""The workspace's folder of CSV files, ``data/`` beside ``agents/`` and ``mcp-servers/``.

A module constant, not a setting: this example is local-only, and the data it serves is the data
the schema document describes. The engine loads each content file in it as a table.
"""

SCHEMA_PATH = (ENV_FILE.parent / "../../agents/rmqna/src/rmqna/prompts/schema_v1.md").resolve()
"""The committed schema document, the same file the agent's prompt carries.

``get_schema`` serves this file's text, so the server and the agent cannot describe different data.
"""
