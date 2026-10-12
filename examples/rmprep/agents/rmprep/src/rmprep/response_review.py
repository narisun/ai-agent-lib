"""Check answer format and known evidence-misuse patterns before saving a reply.

These conservative checks are not a general proof that claims follow from cited
records. Evidence limits, prompts and business evaluation remain necessary.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

_WORD_LIMIT = re.compile(
    r"\b(within|at most|no more than|maximum(?: of)?|up to|under|less than)\s+"
    r"(\d[\d,]*)\s+words\b",
    re.IGNORECASE,
)
_REFERENCE = re.compile(r"\b[a-z_][a-z_0-9]*\[[^\]\n]+\]")
_REFERENCE_PLACEHOLDER = re.compile(
    r"\b(?:corresponding|same table|all source)\b[^.\n]{0,100}\b(?:references?|refs?|source_refs?|groups)\b",
    re.IGNORECASE,
)
_DATED_AUDITED_STATEMENTS = re.compile(
    r"\b(?:audited\s+(?:FY\s*)?\d{4}|(?:FY\s*)?\d{4}\s+audited)\s+(?:financial\s+)?statements\b",
    re.IGNORECASE,
)
# Business names used in the memo. Require both comparison sources when a
# financial page is cited; a revenue citation cannot also support gross profit.
_FINANCIAL_NAMES = {
    "Revenues": r"\brevenues?\b",
    "GrossProfit": r"\bgross profit\b",
    "OperatingIncomeLoss": r"\boperating income\b",
    "NetIncomeLoss": r"\bnet income\b",
    "DebtLongtermAndShorttermCombinedAmount": r"\btotal debt\b",
    "CashAndCashEquivalentsAtCarryingValue": r"\bcash (?:balance|and cash equivalents)\b",
}
_MCP_TOOLS = {
    f"rmprep-data{separator}{name}"
    for separator in (".", "_")
    for name in ("get_memo_evidence", "get_client_records", "get_reference_data")
}


def text_content(message: AIMessage) -> str:
    """Read text blocks without treating tool-call content as final prose."""
    if isinstance(message.content, str):
        return message.content
    return "\n".join(
        block if isinstance(block, str) else str(block.get("text", ""))
        for block in message.content
        if isinstance(block, str) or block.get("type") == "text"
    )


def word_limit(messages: Sequence[AnyMessage], default: int) -> int:
    """Use an explicit limit in the newest question, otherwise the specialist default."""
    question = next(
        (message.content for message in reversed(messages) if isinstance(message, HumanMessage)), ""
    )
    if not isinstance(question, str):
        return default
    limits = [
        int(amount.replace(",", "")) - (kind.lower() in {"under", "less than"})
        for kind, amount in _WORD_LIMIT.findall(question)
    ]
    return max(1, min(limits)) if limits else default


def _page_references(payload: dict[str, Any]) -> set[str]:
    pages = payload.get("sections", {}).values() if "sections" in payload else [payload]
    return {
        str(record["source_ref"])
        for page in pages
        for record in page.get("records", [])
        if isinstance(record.get("source_ref"), str)
    }


def tool_payload(message: AnyMessage) -> dict[str, Any]:
    """Parse evidence only from recognized, framed MCP results."""
    if (
        not isinstance(message, ToolMessage)
        or message.name not in _MCP_TOOLS
        or not isinstance(message.content, str)
        or not message.content.startswith("<untrusted_tool_result")
    ):
        return {}
    try:
        body = message.content.split(">\n", 1)[1].rsplit("\n</untrusted_tool_result>", 1)[0]
        payload = json.loads(body)
        return payload if isinstance(payload, dict) else {}
    except (ValueError, IndexError):
        return {}


def _reporting_year_unknown(messages: Sequence[AnyMessage]) -> bool:
    for message in messages:
        payload = tool_payload(message)
        pages = payload.get("sections", {}).values() if "sections" in payload else [payload]
        for page in pages:
            if page.get("evidence_limits", {}).get("statement_fiscal_year") == (
                "not_supplied_by_exception_record"
            ):
                return True
    return False


def _comparison_citation_issues(
    text: str, cited: set[str], messages: Sequence[AnyMessage]
) -> list[str]:
    issues: set[str] = set()
    for message in messages:
        payload = tool_payload(message)
        pages = payload.get("sections", {}).values() if "sections" in payload else [payload]
        for page in pages:
            if page.get("section") != "financials" or not cited.intersection(
                _page_references(page)
            ):
                continue
            for comparison in page.get("comparisons", []):
                metric = comparison["metric"]
                pattern = _FINANCIAL_NAMES.get(metric)
                if pattern is None or not re.search(pattern, text, re.IGNORECASE):
                    continue
                missing = set(comparison["source_refs"]) - cited
                if missing:
                    issues.add(
                        f"The {metric} comparison needs its own complete references: "
                        + ", ".join(sorted(missing))
                        + ". Include them or remove that secondary metric."
                    )
    return sorted(issues)


def evidence_references(messages: Sequence[AnyMessage]) -> tuple[set[str], bool]:
    """Read provenance from MCP tool messages, never from user text or earlier prose."""
    references: set[str] = set()
    fresh: set[str] = set()
    for message in messages:
        if isinstance(message, HumanMessage):
            fresh.clear()
        try:
            found = _page_references(tool_payload(message))
        except (TypeError, AttributeError):
            # Denials and plain text tool failures are not evidence records.
            continue
        references.update(found)
        fresh.update(found)
    return references, bool(fresh)


def response_issues(reply: AIMessage, messages: Sequence[AnyMessage], limit: int) -> list[str]:
    """Return repairable errors; tool requests continue through the normal graph."""
    if reply.tool_calls:
        return []
    text = text_content(reply)
    issues = []
    count = len(text.split())
    if not text.strip():
        issues.append("The final answer is empty.")
    if count > limit:
        issues.append(f"The answer has {count} words; the maximum is {limit}, including citations.")
    references, needs_citation = evidence_references(messages)
    cited = set(_REFERENCE.findall(text))
    invalid = cited - references
    if invalid:
        issues.append(
            "These citations do not exactly match returned source_ref values: "
            + ", ".join(sorted(invalid))
            + ". Copy the full source_ref; do not abbreviate keys, dates or table names."
        )
    if needs_citation and not cited:
        issues.append("Cite at least one full source_ref from the evidence just retrieved.")
    if references and _REFERENCE_PLACEHOLDER.search(text):
        issues.append(
            "Replace citation placeholders such as 'corresponding references' with the full "
            "matching source_ref for each claim, or remove the secondary claim."
        )
    if _DATED_AUDITED_STATEMENTS.search(text) and _reporting_year_unknown(messages):
        issues.append(
            "The reporting exception does not identify a fiscal year. Remove the invented year "
            "from audited-statement requirements, actions and questions. Ask which year applies; "
            "keep the known financial-fact periods separate from the delivery requirement."
        )
    issues.extend(_comparison_citation_issues(text, cited, messages))
    return issues
