"""Where this service finds its local settings."""

from __future__ import annotations

from pathlib import Path

__all__ = ["ENV_FILE"]

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
"""The service's ``.env`` file, beside ``pyproject.toml``.

A deployed service has no such file and is configured by its environment.
"""
