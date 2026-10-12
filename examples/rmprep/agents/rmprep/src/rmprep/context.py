"""Assemble bounded model input without deleting the durable conversation or memos."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from ai_agent_lib_core import ValidationFailed
from ai_agent_lib_core.pipeline.guardrails import frame_untrusted
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage

from rmprep.memos import turns
from rmprep.response_review import tool_payload
from rmprep.selection import ContextSelection
from rmprep.state import State


@dataclass(frozen=True)
class ContextBudget:
    """Conservative text estimates, independent of any provider's tokenizer.

    Choose input_tokens below the deployed model's context window, leaving room
    for output. tool_schema_tokens reserves space for the bound MCP definitions.
    The estimator is a planning limit, not a provider-exact token count.
    """

    input_tokens: int = 48_000
    question_tokens: int = 8_000
    recent_tokens: int = 6_000
    tool_schema_tokens: int = 4_000

    def __post_init__(self) -> None:
        if min(self.question_tokens, self.recent_tokens, self.tool_schema_tokens) <= 0:
            raise ValueError("Context budgets must be positive")
        if self.input_tokens <= self.tool_schema_tokens:
            raise ValueError("Input budget must leave space after tool definitions")


def estimate_tokens(messages: Sequence[AnyMessage]) -> int:
    """Estimate from UTF-8 bytes including tool arguments, with per-message overhead."""
    return sum(
        16
        + math.ceil(
            len(
                json.dumps(
                    {
                        "content": message.content,
                        "tool_calls": getattr(message, "tool_calls", []),
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            )
            / 3
        )
        for message in messages
    )


def _relevant_evidence(message: AnyMessage, question: str) -> AnyMessage:
    """Select complete evidence sections; retain limits, dates and provenance verbatim."""
    payload = tool_payload(message)
    sections = payload.get("sections")
    if not isinstance(sections, dict) or not isinstance(message, ToolMessage):
        return message
    aliases = {
        "covenants": ("covenant", "dscr", "waiver"),
        "working_capital": ("working capital", "dso", "dio", "dpo"),
        "financials": ("financial", "revenue", "profit", "income"),
        "facilities": ("facility", "facilities", "maturity"),
        "payment_totals": ("payment", "transaction"),
    }
    selected = {
        section
        for section, words in aliases.items()
        if any(w in question.casefold() for w in words)
    }
    selected.update(
        section for section in sections if section.replace("_", " ") in question.casefold()
    )
    if not selected:
        return message
    # This is a projection for one model call. The original payload stays in the checkpoint.
    projected = dict(payload)
    projected["sections"] = {key: value for key, value in sections.items() if key in selected}
    projected["context_omitted_sections"] = sorted(set(sections) - selected)
    return message.model_copy(
        update={
            "content": frame_untrusted(
                message.name or "memo_evidence", json.dumps(projected, ensure_ascii=False)
            )
        }
    )


def _same_context(group: list[AnyMessage], selection: ContextSelection) -> bool:
    answers = [
        message for message in group if isinstance(message, AIMessage) and not message.tool_calls
    ]
    if not answers:
        return False
    metadata = answers[-1].additional_kwargs.get("rmprep_context", {})
    if selection["memo_ids"]:
        return bool(
            metadata.get("memo_ids") == selection["memo_ids"]
            and metadata.get("mode") == selection["mode"]
        )
    return bool(selection["uen"]) and bool(metadata.get("uen") == selection["uen"])


@dataclass(frozen=True)
class ModelContext:
    """The selected messages and a small manifest suitable for checkpoint inspection."""

    messages: list[AnyMessage]
    diagnostics: dict[str, object]


class ContextBuilder:
    """Inject a budget policy; retain selected documents and complete recent exchanges."""

    def __init__(self, budget: ContextBudget | None = None) -> None:
        self.budget = budget or ContextBudget()

    def check_size(self, messages: Sequence[AnyMessage]) -> None:
        """Apply the same bound to initial calls, tool observations and revision attempts."""
        if estimate_tokens(messages) + self.budget.tool_schema_tokens > self.budget.input_tokens:
            raise ValidationFailed(
                "The selected memo and evidence exceed the model input budget. "
                "Ask about one memo section or a smaller date range."
            )

    def build(self, state: State, instructions: str, selection: ContextSelection) -> ModelContext:
        """Pin selected memo snapshots, then add recent discussion within the remaining budget."""
        groups = turns(state["messages"])
        current = groups[-1]
        if estimate_tokens(current[:1]) > self.budget.question_tokens:
            raise ValidationFailed(
                "The question exceeds the input budget. Split it into smaller questions."
            )
        selected = [
            memo for memo in state.get("memos", []) if memo["memo_id"] in selection["memo_ids"]
        ]
        pinned_ids = {message_id for memo in selected for message_id in memo["message_ids"]}
        pinned = [message for message in state["messages"] if message.id in pinned_ids]
        pinned = [_relevant_evidence(message, str(current[0].content)) for message in pinned]
        # Legacy checkpoints without memo records retain bounded recent history. An
        # explicit client change always starts from matching context instead.
        legacy = not state.get("memos") and selection["uen"] in {"", state.get("active_client", "")}
        eligible = [
            group
            for group in groups[:-1]
            if not any(message.id in pinned_ids for message in group)
            and (legacy or _same_context(group, selection))
            and selection["mode"] != "new"
        ]
        manifest: dict[str, object] = {
            "memo_ids": selection["memo_ids"],
            "uen": selection["uen"],
            "meeting_date": selection["meeting_date"],
            "retrieval_date": selection["retrieval_date"],
            "mode": selection["mode"],
            "reason": selection["reason"],
            "memos": [
                {key: memo[key] for key in ("memo_id", "uen", "meeting_date", "generated_at")}
                for memo in selected
            ],
        }
        retained: list[list[AnyMessage]] = []
        system = SystemMessage(instructions + "\nContext selection: " + json.dumps(manifest))
        for group in reversed(eligible):
            candidate = [*group, *(m for prior in retained for m in prior)]
            if estimate_tokens(candidate) > self.budget.recent_tokens:
                break
            if (
                estimate_tokens([system, *pinned, *candidate, *current])
                + self.budget.tool_schema_tokens
                > self.budget.input_tokens - 600
            ):
                break
            retained.insert(0, group)
        omitted = len(groups) - 1 - len(retained) - len(selected)
        summary = ""
        if len(eligible) > len(retained):
            older = eligible[: len(eligible) - len(retained)]
            topics = [str(group[0].content)[:160] for group in older[-3:]]
            summary = "Earlier discussion topics (user text, not evidence): " + json.dumps(topics)
        manifest.update({"omitted_turns": max(0, omitted), "discussion_summary": summary})
        system.content = instructions + "\nContext selection: " + json.dumps(manifest)
        messages = [system, *pinned, *(m for group in retained for m in group), *current]
        self.check_size(messages)
        manifest["estimated_input_tokens"] = (
            estimate_tokens(messages) + self.budget.tool_schema_tokens
        )
        manifest["input_token_budget"] = self.budget.input_tokens
        manifest["included_message_ids"] = [message.id for message in messages[1:]]
        # Keep the same manifest visible in both the checkpoint and model tracing.
        system.content = instructions + "\nContext selection: " + json.dumps(manifest)
        manifest["estimated_input_tokens"] = (
            estimate_tokens(messages) + self.budget.tool_schema_tokens
        )
        system.content = instructions + "\nContext selection: " + json.dumps(manifest)
        self.check_size(messages)
        return ModelContext(messages, manifest)
