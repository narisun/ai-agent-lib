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
_MARKDOWN_PREFIX = re.compile(r"^ {0,3}(?:#{1,6}|[-+*]|\d{1,9}[.)])(?:[ \t]+|$)")
_MARKDOWN_DIVIDER = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
_MARKDOWN_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_REFERENCE_PLACEHOLDER = re.compile(
    r"\b(?:corresponding|same table|all source)\b[^.\n]{0,100}\b(?:references?|refs?|source_refs?|groups)\b",
    re.IGNORECASE,
)
_DATED_AUDITED_STATEMENTS = re.compile(
    r"\b(?:"
    r"(?:audited\s+(?:FY\s*)?\d{4}|(?:FY\s*)?\d{4}\s+audited)\s+(?:financial\s+)?statements"
    r"|audited\s+(?:financial\s+)?statements\s+(?:(?:for|of|covering|from)\s+)?"
    r"(?:the\s+)?(?:(?:fiscal|financial)\s+year\s+|FY\s*)?\d{4}"
    r")\b",
    re.IGNORECASE,
)
# Business names used in temporal comparisons. A point-in-time comparison of
# two companies does not require either company's previous-year records.
_FINANCIAL_NAMES = {
    "Revenues": r"\brevenues?\b",
    "GrossProfit": r"\bgross profit\b",
    "OperatingIncomeLoss": r"\boperating income\b",
    "NetIncomeLoss": r"\bnet income\b",
    "DebtLongtermAndShorttermCombinedAmount": r"\btotal debt\b",
    "CashAndCashEquivalentsAtCarryingValue": r"\bcash (?:balance|and cash equivalents)\b",
}
_TEMPORAL_CHANGE = re.compile(
    r"\b(?:grew|grown|growth|fell|fallen|rose|risen|increas\w*|decreas\w*|"
    r"declin\w*|improv\w*|deteriorat\w*|chang\w*|yoy|year[- ]over[- ]year)\b",
    re.IGNORECASE,
)
_PERIOD = re.compile(r"\b(?:FY\s*)?(?:19|20)\d{2}\b", re.IGNORECASE)
_PERIOD_COMPARISON = re.compile(r"\b(?:from|to|vs\.?|versus|than|higher|lower)\b", re.IGNORECASE)
_MCP_TOOLS = {
    f"rmprep-data{separator}{name}"
    for separator in (".", "_")
    for name in ("get_memo_evidence", "get_client_records", "get_reference_data")
}
# These target observed waiver mistakes, not arbitrary legal or causal reasoning.
# In particular, a hedge such as "may" does not establish an absent blocker.
_WAIVER_ASSERTION = re.compile(
    r"\bwaiver(?: document)?\s+(?:"
    r"(?:has|had)\s+(?:not\s+)?(?:yet\s+)?been\s+|"
    r"(?:is|was|remains)\s+(?:not\s+)?(?:yet\s+)?|"
    r"(?:may|might|must)\s+(?:not\s+)?(?:have been|be)\s+|(?:not\s+)?"
    r")(?:executed|signed|active|in force|unexecuted|unsigned)\b|"
    r"\bwaiver execution\s+(?:(?:is|was|remains)\s+)?(?:pending|complete|confirmed)\b",
    re.IGNORECASE,
)
_WAIVER_STATUS_INFERENCE = re.compile(
    r"\b(?:open|closed|overdue|pending|status|requirement|expiry|received_date|cure plan)\b"
    r".{0,160}\b(?:suggests?|indicates?|implies?|means?|proves?|confirms?|establishes?)\b"
    r".{0,100}(?:\b(?:execut\w*|sign\w*)\b|\b(?:this|it)\s+has\s+"
    r"(?:not\s+)?(?:yet\s+)?(?:occurred|happened)\b)",
    re.IGNORECASE,
)
_WAIVER_BLOCKER = re.compile(
    r"\b(?:block(?:s|ed|ing)?|delay(?:s|ed|ing)?|prevent(?:s|ed|ing)?|"
    r"imped(?:e|es|ed|ing)|holds? up|holding up|depends? on|contingent on|because|due to)\b|"
    r"\b(?:blockers?|dependencies)\s+(?:is|are|include)\b",
    re.IGNORECASE,
)
_WAIVER_UNKNOWN = re.compile(
    r"\b(?:not (?:established|known|confirmed|determined|supplied|provided)|unknown|unverified|"
    r"no (?:proof|evidence)|cannot (?:establish|infer|determine|conclude)|"
    r"(?:does|do) not (?:establish|prove|show|confirm|indicate|mean|imply|support|infer))\b|"
    r"\bneither\b.{0,100}\bestablish\w* whether\b",
    re.IGNORECASE,
)


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


def _word_count(text: str) -> int:
    """Count content and citations without charging for Markdown block markers.

    Only heading/list prefixes and divider-only lines are discounted. Keep other
    syntax, source references and code literal rather than guessing what renders
    as content. This is a counting view; the saved answer is never rewritten.
    """
    count = 0
    fence = ""
    for line in text.splitlines():
        marker = _MARKDOWN_FENCE.match(line)
        if fence:
            count += len(line.split())
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= len(fence)
                and not marker[2].strip()
            ):
                fence = ""
        elif marker:
            fence = marker[1]
            count += len(line.split())
        elif not _MARKDOWN_DIVIDER.fullmatch(line):
            count += len(_MARKDOWN_PREFIX.sub("", line, count=1).split())
    return count


def _page_references(payload: dict[str, Any]) -> set[str]:
    pages = payload.get("sections", {}).values() if "sections" in payload else [payload]
    return {
        str(record["source_ref"])
        for page in pages
        for record in page.get("records", [])
        if isinstance(record.get("source_ref"), str)
    }


def tool_payload(message: AnyMessage, *, include_lookup: bool = False) -> dict[str, Any]:
    """Parse evidence only from recognized, framed MCP results."""
    if (
        not isinstance(message, ToolMessage)
        or message.name
        not in (
            _MCP_TOOLS | {"rmprep-data.find_clients", "rmprep-data_find_clients"}
            if include_lookup
            else _MCP_TOOLS
        )
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
    claims = _temporal_claims(text)
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
                if pattern is None:
                    continue
                for claim in claims:
                    claim_refs = set(_REFERENCE.findall(claim))
                    prose = _REFERENCE.sub("", claim)
                    if not re.search(pattern, prose, re.IGNORECASE) or not claim_refs.intersection(
                        _page_references(page)
                    ):
                        continue
                    missing = set(comparison["source_refs"]) - cited
                    if missing:
                        issues.add(
                            f"The {metric} comparison needs its own complete references: "
                            + ", ".join(sorted(missing))
                            + ". Include them or remove that secondary metric."
                        )
    return sorted(issues)


def _temporal_claims(text: str) -> list[str]:
    """Find trend statements and temporal tables without reading dates inside citations.

    This is a conservative format check, not a semantic judge. A same-year
    company comparison must not be rewritten merely because its evidence page
    also contains calculated year-over-year changes.
    """
    claims = []
    temporal_section = False
    temporal_table = False
    for line in text.splitlines():
        prose = _REFERENCE.sub("", line).strip()
        trend = bool(_TEMPORAL_CHANGE.search(prose))
        periods = {period.lower().replace("fy", "").strip() for period in _PERIOD.findall(prose)}
        table = prose.startswith("|")
        period_pair = len(periods) > 1 and (table or bool(_PERIOD_COMPARISON.search(prose)))
        if prose.startswith("#"):
            temporal_section = trend or period_pair
        if table:
            temporal_table |= trend or period_pair
        else:
            temporal_table = False
        if trend or period_pair or temporal_section or temporal_table:
            claims.append(line)
    return claims


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
    count = _word_count(text)
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
    issues.extend(_implementation_issues(text, messages))
    issues.extend(_waiver_issues(text, messages))
    return issues


def _waiver_issues(text: str, messages: Sequence[AnyMessage]) -> list[str]:
    """Keep unknown execution unknown, including in saved pre-fix evidence.

    Only recognized MCP limits activate this check. Inspect clauses separately:
    a question or caveat must not excuse a later unsupported assertion. These
    patterns cover known failures; the prompt and business review still matter.
    """
    unknown = False
    for message in messages:
        payload = tool_payload(message)
        pages = payload.get("sections", {}).values() if "sections" in payload else [payload]
        unknown |= any(
            page.get("evidence_limits", {}).get("waiver_execution")
            == "not_established_by_this_source"
            for page in pages
        )
    if not unknown:
        return []
    question = next(
        (
            str(message.content)
            for message in reversed(messages)
            if isinstance(message, HumanMessage)
        ),
        "",
    )
    waiver_context = bool(re.search(r"\bwaiver\b", text + " " + question, re.IGNORECASE))
    clauses: list[str] = []
    negated_list = False
    for line in _REFERENCE.sub("", text).splitlines():
        if not line.strip():
            continue
        # "The records do not prove: - that the waiver is unsigned" is a
        # denial, not an assertion. Scope this exception to the following list;
        # a separate paragraph or heading must be checked on its own.
        if negated_list and re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", line):
            continue
        line = re.sub(r"[*`|]", " ", line)
        negated_list = bool(
            re.search(
                r"\b(?:does|do) not (?:establish|prove|show|confirm|indicate|mean|imply|support)\s*:\s*$",
                line,
                re.IGNORECASE,
            )
        )
        clauses.extend(re.split(r"(?<=[.!?])\s+|;|,?\s+\b(?:but|however)\b\s+|,\s+yet\s+", line))
    for clause in clauses:
        clause = " ".join(clause.split())
        if _WAIVER_UNKNOWN.search(clause):
            continue
        if re.search(r"\b(?:ask|check|verify|confirm)\s+whether\b|^if\b", clause, re.IGNORECASE):
            continue
        if re.search(r"\b(?:if|whether)\s+(?:(?:the|a)\s+)?waiver\b", clause, re.IGNORECASE):
            continue
        if clause.endswith("?") and re.match(
            r"^(?:\d+[.)]\s*)?(?:was|is|are|has|have|did|does|do|could|can|would)\b",
            clause,
            re.IGNORECASE,
        ):
            continue
        assertion = _WAIVER_ASSERTION.search(clause)
        inference = waiver_context and _WAIVER_STATUS_INFERENCE.search(clause)
        blocker = (
            waiver_context
            and re.search(r"\b(?:waiver|execution)\b", clause, re.IGNORECASE)
            and _WAIVER_BLOCKER.search(clause)
        )
        if assertion or inference or blocker:
            return [
                "Waiver execution and its blockers are not established by these exception records. "
                "Report recorded status, requirement, dates and cure plan only. Do not infer "
                "execution OR non-execution from them, or a link between overdue statements and "
                "execution, even with 'may' or 'suggests'. State that execution is unknown; "
                "ask whether a signed document exists and whether any blocker is documented."
            ]
    return []


def _implementation_issues(text: str, messages: Sequence[AnyMessage]) -> list[str]:
    """Reject explicit deployment claims cited only to status-only evidence.

    This narrow safeguard complements the prompt; it is not a general semantic
    validator. Questions and qualified statements about missing proof are allowed.
    """
    limited: set[str] = set()
    for message in messages:
        payload = tool_payload(message)
        pages = payload.get("sections", {}).values() if "sections" in payload else [payload]
        for page in pages:
            if page.get("evidence_limits", {}).get("implementation_outcome") in {
                "not_supplied_closed_won_is_a_sales_stage",
                "not_established_by_closed_request_status",
            }:
                limited.update(_page_references(page))
    for line in text.splitlines():
        refs = set(_REFERENCE.findall(line))
        if not refs or not refs <= limited:
            continue
        for clause in re.split(r"[.;]", _REFERENCE.sub("", line)):
            claim = re.search(
                r"\b(?:deployed|implemented|launched|activated|went live|"
                r"(?:implementation|deployment|activation)\s+(?:was\s+)?(?:completed|confirmed))\b",
                clause,
                re.IGNORECASE,
            )
            qualified = re.search(
                r"\b(?:not|no|never|unverified|unknown|unconfirmed|whether|if|"
                r"confirm|verify|check|prove|proof|evidence|pending)\b|\?",
                clause,
                re.IGNORECASE,
            )
            if claim and not qualified:
                return [
                    "Closed Won opportunities and closed service requests do not establish "
                    "implementation. Replace the deployment claim with the exact recorded "
                    "sales/service status and date; state that implementation is unverified."
                ]
    return []


def response_advisories(reply: AIMessage, messages: Sequence[AnyMessage]) -> list[str]:
    """Record uncertain prose interpretations for inspection, never as retry triggers.

    Exact citation existence remains a blocking check. Semantic association of
    metrics, companies and periods requires business evaluation, not a regex proof.
    """
    if reply.tool_calls:
        return []
    text = text_content(reply)
    return _comparison_citation_issues(text, set(_REFERENCE.findall(text)), messages)
