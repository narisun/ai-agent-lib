"""Assemble bounded model input without deleting the durable conversation or memos."""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from dataclasses import dataclass

from ai_agent_lib_core import ValidationFailed
from ai_agent_lib_core.integrations.langgraph.prompts import static_system_prompt
from ai_agent_lib_core.pipeline.guardrails import frame_untrusted
from langchain_core.messages import AIMessage, AnyMessage, ToolMessage

from rmprep.clients import resolved_clients
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

    input_tokens: int = 128_000
    question_tokens: int = 16_000
    recent_tokens: int = 96_000
    tool_schema_tokens: int = 4_000

    def __post_init__(self) -> None:
        counts = (
            self.input_tokens,
            self.question_tokens,
            self.recent_tokens,
            self.tool_schema_tokens,
        )
        if any(type(count) is not int or count <= 0 for count in counts):
            raise ValueError("Context budgets must be positive integers")
        if self.input_tokens <= self.tool_schema_tokens:
            raise ValueError("Input budget must leave space after tool definitions")
        available = self.input_tokens - self.tool_schema_tokens
        if max(self.question_tokens, self.recent_tokens) > available:
            raise ValueError(
                "Question and recent-history limits must each fit inside the input budget after tool definitions"
            )


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
        "relationship": ("wallet share", "wallet-share", "relationship"),
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


def _same_context(
    group: list[AnyMessage], selection: ContextSelection, *, legacy: bool = False
) -> bool:
    answers = [
        message for message in group if isinstance(message, AIMessage) and not message.tool_calls
    ]
    if not answers:
        return False
    metadata = answers[-1].additional_kwargs.get("rmprep_context", {})
    selected_date = selection["retrieval_date"] or selection["meeting_date"]
    prior_date = metadata.get("retrieval_date") or metadata.get("meeting_date")
    if selected_date and prior_date and selected_date != prior_date:
        return False
    if selection["memo_ids"]:
        return bool(metadata.get("memo_ids") == selection["memo_ids"])
    selected_clients = set(selection.get("client_uens", []))
    if len(selected_clients) > 1:
        previous_clients = set(metadata.get("client_uens", []))
        if metadata.get("uen"):
            previous_clients.add(metadata["uen"])
        previous_clients.update(resolved_clients(group))
        return bool(previous_clients) and previous_clients <= selected_clients
    if metadata:
        return bool(legacy or selection["uen"]) and metadata.get("uen") == selection["uen"]
    # Old untagged turns may still carry a governed client identity. A legacy
    # fallback must not restore evidence from a different client in this thread.
    clients = resolved_clients(group) if legacy else set()
    return legacy and (not clients or clients == {selection["uen"]})


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

    def build(
        self,
        state: State,
        instructions: str,
        selection: ContextSelection,
        *,
        output_budget: str = "",
    ) -> ModelContext:
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
        pinned_ids.update(selection.get("lookup_message_ids", []))
        pinned_ids.update(selection.get("evidence_message_ids", []))
        # Pin whole prior exchanges, including discovery results used by "these clients".
        # A lookup in the current tool loop is already present in current and must not repeat.
        pinned = [message for group in groups[:-1] for message in group if message.id in pinned_ids]
        pinned = [_relevant_evidence(message, str(current[0].content)) for message in pinned]
        # Legacy checkpoints without memo records retain bounded recent history. An
        # explicit client change always starts from matching context instead.
        legacy = not state.get("memos") and selection["uen"] in {"", state.get("active_client", "")}
        eligible = [
            [_relevant_evidence(message, str(current[0].content)) for message in group]
            for group in groups[:-1]
            if not any(message.id in pinned_ids for message in group)
            and _same_context(group, selection, legacy=legacy)
            and selection["mode"] != "new"
        ]
        manifest: dict[str, object] = {
            "memo_ids": selection["memo_ids"],
            "uen": selection["uen"],
            "client_uens": selection.get("client_uens", []),
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

        def system_message() -> AnyMessage:
            return static_system_prompt(
                instructions, output_budget + "\nContext selection: " + json.dumps(manifest)
            )

        system = system_message()
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
        pinned_turns = sum(
            any(message.id in pinned_ids for message in group) for group in groups[:-1]
        )
        omitted = len(groups) - 1 - len(retained) - pinned_turns
        summary = ""
        if len(eligible) > len(retained):
            older = eligible[: len(eligible) - len(retained)]
            topics = [str(group[0].content)[:160] for group in older[-3:]]
            summary = "Earlier discussion topics (user text, not evidence): " + json.dumps(topics)
        manifest.update({"omitted_turns": max(0, omitted), "discussion_summary": summary})
        system = system_message()
        messages = [system, *pinned, *(m for group in retained for m in group), *current]
        self.check_size(messages)
        manifest["estimated_input_tokens"] = (
            estimate_tokens(messages) + self.budget.tool_schema_tokens
        )
        manifest["input_token_budget"] = self.budget.input_tokens
        manifest["recent_token_budget"] = self.budget.recent_tokens
        manifest["retained_recent_tokens"] = estimate_tokens(
            [m for group in retained for m in group]
        )
        manifest["question_token_budget"] = self.budget.question_tokens
        manifest["tool_schema_token_allowance"] = self.budget.tool_schema_tokens
        manifest["included_message_ids"] = [message.id for message in messages[1:]]
        # IDs and token estimates belong to diagnostics, not model instructions.
        # They change on every tool call and would invalidate the cached prefix.
        return ModelContext(messages, manifest)
