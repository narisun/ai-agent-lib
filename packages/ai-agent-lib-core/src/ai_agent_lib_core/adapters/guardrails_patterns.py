"""Guardrails that need no service: size limits and pattern checks.

The size limits are hard limits. The pattern checks look for personal data
and for text that reads like an attempt to redirect a model. They are
heuristics: they miss things and they flag harmless text, so by default they
only record what they saw. The structural defences against a hostile tool
result are elsewhere, in policy, in the registry and in the way results are
framed as data.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from typing import Literal

from pydantic import Field

from ai_agent_lib_core.contracts import (
    GuardrailFinding,
    GuardrailPoint,
    GuardrailVerdict,
    OptionsModel,
    RequestContext,
)

__all__ = ["GuardrailsOptions", "PatternGuardrails", "PatternGuardrailsOptions"]

Action = Literal["off", "flag", "block"]
PiiKind = Literal["email", "credit_card", "iban", "us_ssn"]


class GuardrailsOptions(OptionsModel):
    """Options every guardrails adapter accepts.

    Attributes:
        frame_tool_results: Mark every tool result as untrusted data before a
            model sees it.
    """

    frame_tool_results: bool = True


class PatternGuardrailsOptions(GuardrailsOptions):
    """Options of the ``patterns`` guardrails provider.

    Attributes:
        max_model_input_chars: The most text one model call may send.
        max_model_output_chars: The most text one model reply may hold.
        max_tool_input_chars: The most text the arguments of a tool call may hold.
        max_tool_result_chars: The most text a tool may return.
        pii: What to do when personal data is found, at every point.
        pii_kinds: The kinds of personal data looked for.
        injection: What to do when a tool result reads like an instruction to the model.
    """

    max_model_input_chars: int = Field(default=2_000_000, gt=0)
    max_model_output_chars: int = Field(default=100_000, gt=0)
    max_tool_input_chars: int = Field(default=20_000, gt=0)
    max_tool_result_chars: int = Field(default=200_000, gt=0)
    pii: Action = "flag"
    pii_kinds: tuple[PiiKind, ...] = ("email", "credit_card", "iban", "us_ssn")
    injection: Action = "flag"


def _luhn(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 1:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _card(match: re.Match[str]) -> bool:
    digits = re.sub(r"[ -]", "", match.group())
    return 13 <= len(digits) <= 19 and _luhn(digits)


def _iban(match: re.Match[str]) -> bool:
    compact = match.group().replace(" ", "")
    rearranged = compact[4:] + compact[:4]
    number = "".join(str(int(char, 36)) for char in rearranged)
    return int(number) % 97 == 1


def _always(match: re.Match[str]) -> bool:  # noqa: ARG001 - the pattern alone decides
    return True


_PII: Mapping[str, tuple[re.Pattern[str], Callable[[re.Match[str]], bool]]] = {
    "email": (
        re.compile(r"\b[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){1,8}\b"),
        _always,
    ),
    "credit_card": (re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])"), _card),
    "iban": (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b"), _iban),
    "us_ssn": (re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b"), _always),
}

_INJECTION: Mapping[str, re.Pattern[str]] = {
    "override": re.compile(
        r"(?i)\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}"
        r"\b(?:previous|prior|above|earlier|all|your)\b[^.\n]{0,40}"
        r"\b(?:instructions?|prompts?|rules|guidelines)\b"
    ),
    "role": re.compile(
        r"(?i)(?:\byou are now\b|\bfrom now on,? you\b|\bnew (?:system )?(?:prompt|instructions)\b"
        r"|^\s*(?:system|assistant)\s*:)",
        re.MULTILINE,
    ),
    "exfiltration": re.compile(
        r"(?i)\b(?:reveal|print|show|repeat|send|leak)\b[^.\n]{0,40}"
        r"\b(?:system prompt|your instructions|api[ _-]?key|secret|password|credentials)\b"
    ),
    "markup": re.compile(r"(?i)<\s*/?\s*system\s*>|\[/?INST\]|<\|im_(?:start|end)\|>"),
}


class PatternGuardrails:
    """Size limits, personal-data patterns and prompt-injection heuristics.

    Args:
        options: The limits and what to do about each kind of finding.
    """

    def __init__(self, options: PatternGuardrailsOptions | None = None) -> None:
        self._options = options if options is not None else PatternGuardrailsOptions()
        self._limits = {
            GuardrailPoint.MODEL_INPUT: self._options.max_model_input_chars,
            GuardrailPoint.MODEL_OUTPUT: self._options.max_model_output_chars,
            GuardrailPoint.TOOL_INPUT: self._options.max_tool_input_chars,
            GuardrailPoint.TOOL_RESULT: self._options.max_tool_result_chars,
        }

    async def check(
        self,
        point: GuardrailPoint,
        text: str,
        context: RequestContext | None,  # noqa: ARG002 - these checks treat every caller alike
    ) -> GuardrailVerdict:
        """Return what the checks found in ``text``."""
        if len(text) > self._limits[GuardrailPoint(point)]:
            # Text over the limit is not scanned: the call is stopped anyway.
            return GuardrailVerdict((GuardrailFinding("size", blocked=True),))
        findings = list(self._pii(text))
        if point == GuardrailPoint.TOOL_RESULT:
            findings.extend(self._injection(text))
        return GuardrailVerdict(tuple(findings))

    def _pii(self, text: str) -> Iterable[GuardrailFinding]:
        if self._options.pii == "off":
            return
        for kind in self._options.pii_kinds:
            pattern, confirm = _PII[kind]
            count = sum(1 for match in pattern.finditer(text) if confirm(match))
            if count:
                yield GuardrailFinding(
                    f"pii.{kind}", count=count, blocked=self._options.pii == "block"
                )

    def _injection(self, text: str) -> Iterable[GuardrailFinding]:
        if self._options.injection == "off":
            return
        for kind, pattern in _INJECTION.items():
            count = sum(1 for _ in pattern.finditer(text))
            if count:
                yield GuardrailFinding(
                    f"injection.{kind}", count=count, blocked=self._options.injection == "block"
                )
