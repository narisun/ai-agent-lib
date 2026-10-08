"""The outcome of checking one thing a service depends on."""

from __future__ import annotations

from dataclasses import dataclass

from ai_agent_lib_core.contracts._validation import require_identifier

__all__ = ["CheckResult"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Whether one dependency of a service is usable, and what to do if it is not.

    Attributes:
        name: What was checked, for example ``audit (jsonl)``.
        ok: Whether it is usable.
        detail: What was found. It never holds a secret or a caller's data.
        fix: What to do about a failure.
    """

    name: str
    ok: bool
    detail: str = ""
    fix: str = ""

    def __post_init__(self) -> None:
        require_identifier("name", self.name)
