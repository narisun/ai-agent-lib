"""A dataset of cases, one JSON object per line."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ai_agent_lib_core.contracts import ConfigurationError

__all__ = ["EvalCase", "load_cases"]


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One case: what the agent is given and what a good answer has.

    Attributes:
        id: Names the case in the report. The line number when the file names none.
        input: What the task is given, for example ``{"text": "..."}``.
        expected: What a good answer has, for the scorers to compare with.
        tags: Labels for slicing the report, such as ``"fraud"`` or ``"hard"``.
    """

    id: str
    input: Mapping[str, Any]
    expected: Mapping[str, Any] = field(default_factory=dict)
    tags: tuple[str, ...] = ()


def _case(raw: object, number: int, path: Path) -> EvalCase:
    where = f"{path.name}, line {number}"
    if not isinstance(raw, dict):
        raise ConfigurationError(
            f"{where} is not a case",
            expected='an object such as {"input": {...}, "expected": {...}}',
            actual=f"a {type(raw).__name__}",
        )
    unknown = sorted(set(raw) - {"id", "input", "expected", "tags"})
    if unknown or not isinstance(raw.get("input"), dict):
        raise ConfigurationError(
            f"{where} is not a case",
            expected='"input" as an object, and optionally "id", "expected" and "tags"',
            actual=(f"unknown keys: {', '.join(unknown)}" if unknown else 'no "input" object'),
        )
    expected, tags = raw.get("expected", {}), raw.get("tags", [])
    if not isinstance(expected, dict) or not isinstance(tags, list):
        raise ConfigurationError(
            f"{where} is not a case",
            expected='"expected" as an object and "tags" as a list',
            actual=f'"expected" is a {type(expected).__name__}, "tags" a {type(tags).__name__}',
        )
    return EvalCase(
        id=str(raw.get("id", number)),
        input=raw["input"],
        expected=expected,
        tags=tuple(str(tag) for tag in tags),
    )


def load_cases(path: Path) -> list[EvalCase]:
    """Read the cases in a JSON Lines file. Blank lines and lines starting with # are skipped.

    Raises:
        ConfigurationError: If the file is missing, a line is not JSON or not a
            case, or two cases share an ID.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ConfigurationError(
            f"the cases file {path} cannot be read",
            expected="a JSON Lines file, one case per line",
            fix="create it next to the eval test",
        ) from exc
    cases: list[EvalCase] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ConfigurationError(
                f"{path.name}, line {number} is not JSON",
                actual=f"{exc.msg.lower()} at column {exc.colno}",
            ) from None
        cases.append(_case(raw, number, path))
    ids = [case.id for case in cases]
    repeated = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if repeated:
        raise ConfigurationError(
            f"{path.name}: case IDs are used more than once", actual=", ".join(repeated)
        )
    return cases
