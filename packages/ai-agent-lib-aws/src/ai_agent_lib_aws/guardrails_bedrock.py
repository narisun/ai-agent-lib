"""Guardrails checked by an Amazon Bedrock guardrail.

A security team defines the guardrail in Bedrock: its content filters, denied
topics, word lists and personal-data rules. This adapter sends text to it and
turns the answer into a verdict. It works with any model, on Bedrock or not,
because it calls the guardrail on its own.

Text a model will read is checked as input: what is sent to a model and what
a tool returned. A tool result is where an attack on the prompt hides, and
Bedrock looks for those in input only. Text a model wrote is checked as
output: a model's reply and the arguments of a tool call.

Every check is a call to Bedrock that is paid for by the amount of text, and
the text sent to a model includes the whole conversation. ``points`` limits
the checks to the points a service needs.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator, Mapping
from typing import Any

from botocore import exceptions as aws
from pydantic import Field

from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import (
    AgentLibError,
    ConfigurationError,
    GuardrailFinding,
    GuardrailPoint,
    GuardrailVerdict,
    RequestContext,
)
from ai_agent_lib_core.kit import GuardrailsOptions

__all__ = ["DRAFT_VERSION", "BedrockGuardrails", "BedrockGuardrailsOptions"]

DRAFT_VERSION = "DRAFT"
"""The working version of a guardrail, which changes without a new version number."""

_WHAT = "the bedrock guardrails"
_INTERVENED = "GUARDRAIL_INTERVENED"
_NO_ACTION = frozenset({"", "NONE"})
_NOT_A_CODE = re.compile(r"[^a-z0-9]+")
_ALL_POINTS = tuple(GuardrailPoint)

_SOURCES: Mapping[GuardrailPoint, str] = {
    GuardrailPoint.MODEL_INPUT: "INPUT",
    GuardrailPoint.TOOL_RESULT: "INPUT",
    GuardrailPoint.MODEL_OUTPUT: "OUTPUT",
    GuardrailPoint.TOOL_INPUT: "OUTPUT",
}

# Where each kind of finding is in an assessment: policy, list, the field that
# names the finding, and the first part of its code. A field of None means the
# finding has no safe name: a custom word's only name is the matched text.
_FINDINGS: tuple[tuple[str, str, str | None, str], ...] = (
    ("topicPolicy", "topics", "name", "topic"),
    ("contentPolicy", "filters", "type", "content"),
    ("wordPolicy", "customWords", None, "word"),
    ("wordPolicy", "managedWordLists", "type", "word"),
    ("sensitiveInformationPolicy", "piiEntities", "type", "pii"),
    ("sensitiveInformationPolicy", "regexes", "name", "regex"),
    ("contextualGroundingPolicy", "filters", "type", "grounding"),
)


class BedrockGuardrailsOptions(GuardrailsOptions):
    """Options of the ``bedrock`` guardrails provider.

    Attributes:
        guardrail_id: The ID or the ARN of the guardrail.
        guardrail_version: A published version number. ``DRAFT`` is accepted
            outside production only.
        points: The points at which text is sent to the guardrail. The size
            limits apply at every point.
        max_model_input_chars: The most text one model call may send.
        max_model_output_chars: The most text one model reply may hold.
        max_tool_input_chars: The most text the arguments of a tool call may hold.
        max_tool_result_chars: The most text a tool may return.
    """

    guardrail_id: str = Field(
        pattern=r"^([a-z0-9]+|arn:aws(-[^:]+)?:bedrock:[a-z0-9-]{1,20}:[0-9]{12}:guardrail/[a-z0-9]+)$",
        max_length=2048,
    )
    guardrail_version: str = Field(pattern=r"^([1-9][0-9]{0,7}|DRAFT)$")
    points: tuple[GuardrailPoint, ...] = _ALL_POINTS
    max_model_input_chars: int = Field(default=2_000_000, gt=0)
    max_model_output_chars: int = Field(default=100_000, gt=0)
    max_tool_input_chars: int = Field(default=20_000, gt=0)
    max_tool_result_chars: int = Field(default=200_000, gt=0)


def _code(prefix: str, name: object) -> str:
    """Return a finding code built from a name the guardrail's owner chose."""
    cleaned = _NOT_A_CODE.sub("_", str(name or "").lower()).strip("_")
    return f"{prefix}.{cleaned}" if cleaned else prefix


def _items(assessment: Mapping[str, Any]) -> Iterator[tuple[str, bool]]:
    """Yield a code and whether it blocks, for everything an assessment holds.

    The matched text, which the service returns beside each finding, is never read.
    """
    for policy, collection, name_field, prefix in _FINDINGS:
        section = assessment.get(policy)
        if not isinstance(section, Mapping):
            continue
        for item in section.get(collection) or ():
            if not isinstance(item, Mapping):
                continue
            code = _code(prefix, item.get(name_field)) if name_field else f"{prefix}.custom"
            yield code, str(item.get("action") or "") not in _NO_ACTION


def _verdict(reply: Mapping[str, Any]) -> GuardrailVerdict:
    intervened = reply.get("action") == _INTERVENED
    counts: Counter[tuple[str, bool]] = Counter()
    for assessment in reply.get("assessments") or ():
        if isinstance(assessment, Mapping):
            counts.update(_items(assessment))
    findings = [
        # An intervention stops the call, so nothing the guardrail acted on is only a note.
        GuardrailFinding(code, count=count, blocked=blocked and intervened)
        for (code, blocked), count in sorted(counts.items())
    ]
    if intervened and not any(finding.blocked for finding in findings):
        findings.append(GuardrailFinding("guardrail", blocked=True))
    return GuardrailVerdict(tuple(findings))


class BedrockGuardrails:
    """Checks text with one version of one Bedrock guardrail.

    Whenever the guardrail intervenes the call is stopped. That includes a
    guardrail set up to mask personal data: the masked text cannot be passed
    on through this port, so the call stops instead of continuing unmasked.

    A check that gets no answer raises, and the pipeline then stops the call.

    Args:
        options: The guardrail and the size limits.
        sessions: Builds the client and makes its calls.
        client: A Bedrock runtime client to use instead of building one.
    """

    def __init__(
        self, options: BedrockGuardrailsOptions, sessions: AwsSessionFactory, *, client: Any = None
    ) -> None:
        self._options = options
        self._sessions = sessions
        self._client = client if client is not None else sessions.client("bedrock-runtime")
        self._points = frozenset(options.points)
        self._limits = {
            GuardrailPoint.MODEL_INPUT: options.max_model_input_chars,
            GuardrailPoint.MODEL_OUTPUT: options.max_model_output_chars,
            GuardrailPoint.TOOL_INPUT: options.max_tool_input_chars,
            GuardrailPoint.TOOL_RESULT: options.max_tool_result_chars,
        }

    def __repr__(self) -> str:
        options = self._options
        return (
            f"BedrockGuardrails(guardrail={options.guardrail_id!r}, "
            f"version={options.guardrail_version!r})"
        )

    async def check(
        self,
        point: GuardrailPoint,
        text: str,
        context: RequestContext | None,  # noqa: ARG002 - the guardrail treats every caller alike
    ) -> GuardrailVerdict:
        """Return what the guardrail found in ``text``.

        Raises:
            TransientError: If Bedrock throttled the call or could not be reached.
            ConfigurationError: If AWS refused the service's access.
            AgentLibError: If the guardrail gave no usable answer.
        """
        where = GuardrailPoint(point)
        if len(text) > self._limits[where]:
            # Text over the limit is not sent: the call is stopped anyway.
            return GuardrailVerdict((GuardrailFinding("size", blocked=True),))
        if not text or where not in self._points:
            return GuardrailVerdict()
        return _verdict(await self._apply(_SOURCES[where], text))

    async def validate(self) -> None:
        """Check that the guardrail exists and that the service may use it.

        One short text is sent through the guardrail, which needs only the
        permission every check needs.

        Raises:
            ConfigurationError: If the guardrail cannot be used.
        """
        try:
            await self._apply("INPUT", "ready")
        except ConfigurationError:
            raise
        except AgentLibError as exc:
            raise ConfigurationError(str(exc)) from None

    async def _apply(self, source: str, text: str) -> Mapping[str, Any]:
        options = self._options
        try:
            reply = await self._sessions.invoke(
                _WHAT,
                self._client.apply_guardrail,
                guardrailIdentifier=options.guardrail_id,
                guardrailVersion=options.guardrail_version,
                source=source,
                content=[{"text": {"text": text}}],
            )
        except (aws.BotoCoreError, aws.ClientError) as exc:
            code = (
                exc.response.get("Error", {}).get("Code")
                if isinstance(exc, aws.ClientError)
                else None
            )
            # The service's message can repeat the text, so it stays in the cause.
            raise AgentLibError(
                f"{_WHAT} could not check the text ({code or type(exc).__name__})"
            ) from exc
        if not isinstance(reply, Mapping) or not isinstance(reply.get("action"), str):
            raise AgentLibError(f"{_WHAT} gave no verdict")
        return reply
