"""System implementations of the clock and identifier ports."""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

__all__ = ["SystemClock", "UuidGenerator"]


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        """Return the current time as an aware UTC datetime."""
        return datetime.now(UTC)

    def monotonic(self) -> float:
        """Return seconds from a clock that never goes backwards."""
        return time.monotonic()


class UuidGenerator:
    """Random, unique identifiers."""

    def new_id(self) -> str:
        """Return a new random identifier."""
        return uuid.uuid4().hex
