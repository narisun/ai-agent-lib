"""Deterministic review prompts from evidence, with explicit example thresholds.

These thresholds support this example; they are not an approved bank risk model
or a product-eligibility decision. Every finding carries its source references.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from agentcraft2_core.contracts import MASK


def _number(value: object) -> Decimal | None:
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _open(record: dict[str, Any]) -> bool:
    return str(record.get("status") or "").lower() not in {
        "",
        MASK,
        "closed",
        "resolved",
        "complete",
        "completed",
        "ready",
    }


def _latest_by(records: list[dict[str, Any]], key: str, dated_by: str) -> list[dict[str, Any]]:
    """Use each entity's latest observation when evaluating its current status."""
    latest: dict[str, dict[str, Any]] = {}
    for record in sorted(records, key=lambda row: str(row.get(dated_by, "")), reverse=True):
        identity = str(record.get(key) or "")
        if identity and identity != MASK:
            latest.setdefault(identity, record)
    return list(latest.values())


def review_findings(sections: dict[str, Any], meeting_date: date) -> list[dict[str, Any]]:
    """Flag sourced exceptions and review dates; leave interpretation to the RM."""
    findings: list[dict[str, Any]] = []

    def rows(section: str) -> list[dict[str, Any]]:
        return list(sections.get(section, {}).get("records", []))

    def add(code: str, priority: str, message: str, *records: dict[str, Any]) -> None:
        findings.append(
            {
                "code": code,
                "priority": priority,
                "message": message,
                "source_refs": [record["source_ref"] for record in records],
            }
        )

    for row in rows("covenants"):
        if row.get("in_compliance") is False:
            add(
                "COVENANT_BREACH_RISK",
                "urgent",
                "Recorded covenant verdict is not in compliance; verify waiver and cure status.",
                row,
            )
    for row in rows("facilities"):
        days = _number(row.get("days_to_maturity"))
        if days is not None and days <= 90:
            add(
                "REFINANCING_URGENCY",
                "urgent",
                f"Recorded facility maturity is {int(days)} days from the meeting date (negative means past due).",
                row,
            )
    for row in _latest_by(rows("borrowing_base"), "facility_id", "as_of"):
        pct = _number(row.get("availability_pct_of_commitment"))
        if pct is not None and pct < 10:
            add(
                "BORROWING_BASE_PRESSURE",
                "urgent",
                f"Supplied excess availability is {pct}% of the USD commitment, below the example's 10% review threshold.",
                row,
            )
    for row in rows("credit"):
        due = _date(row.get("annual_review_date"))
        if due and (due - meeting_date).days <= 30:
            add(
                "ANNUAL_REVIEW_DUE",
                "review",
                f"Annual review date is {due}; the example checks a 30-day look-ahead.",
                row,
            )
        if str(row.get("watch_status", "")).lower() not in {"none", "pass", "", "null", MASK}:
            add(
                "WATCH_STATUS",
                "review",
                "The supplied watch status requires discussion; it is not a new rating decision.",
                row,
            )
        dscr, current = _number(row.get("debt_service_coverage")), _number(row.get("current_ratio"))
        if dscr is not None and dscr < Decimal("1.10"):
            add(
                "WEAK_DEBT_SERVICE_COVERAGE",
                "review",
                "Recorded DSCR is below the example review threshold of 1.10; use covenant verdicts separately.",
                row,
            )
        if current is not None and current < 1:
            add(
                "WORKING_CAPITAL_STRAIN",
                "review",
                "Recorded current ratio is below 1.0; ask for the liquidity plan.",
                row,
            )
    for section, code in [
        ("credit_exceptions", "CREDIT_DOCUMENT_EXCEPTION"),
        ("commitments", "UNRESOLVED_CLIENT_COMMITMENT"),
    ]:
        for row in rows(section):
            due = _date(row.get("due_date"))
            if _open(row) and due and due < meeting_date:
                add(code, "review", f"An unresolved item is past its recorded due date {due}.", row)
    for row in rows("treasury_controls"):
        if _open(row):
            add(
                "TREASURY_CONTROL_ISSUE",
                "review",
                "An unresolved treasury control finding is recorded; preserve its severity and owner.",
                row,
            )
    for row in _latest_by(rows("source_readiness"), "domain", "as_of_date"):
        if str(row.get("quality_status") or "").lower() not in {"", "ready", MASK}:
            add(
                "SOURCE_READINESS_ISSUE",
                "review",
                "The source is marked stale or under review; qualify conclusions from this domain.",
                row,
            )
    seen: set[tuple[object, ...]] = set()
    for section in ("operational_service_cases", "service_requests"):
        for row in rows(section):
            key = (row.get("uen"), row.get("request"), row.get("opened_date"), row.get("product"))
            if key in seen:
                continue
            seen.add(key)
            if _open(row) and str(row.get("priority", "")).startswith(("1", "2")):
                add(
                    "OPERATIONAL_FRICTION",
                    "review",
                    "A high-priority service case remains open in the supplied snapshot; verify its current status.",
                    row,
                )
                if str(row.get("priority", "")).startswith("1"):
                    add(
                        "CRITICAL_SERVICE_ISSUE",
                        "urgent",
                        "A critical service case is unresolved. Address service recovery before commercial opportunities.",
                        row,
                    )
    history = rows("working_capital")
    if len(history) >= 2 and history[0].get("currency") == history[1].get("currency"):
        latest, previous = history[:2]
        dso = [_number(row.get("dso_days")) for row in (latest, previous)]
        cycle = [_number(row.get("cash_conversion_cycle_days")) for row in (latest, previous)]
        dso_change = dso[0] - dso[1] if dso[0] is not None and dso[1] is not None else None
        cycle_change = (
            cycle[0] - cycle[1] if cycle[0] is not None and cycle[1] is not None else None
        )
        if (dso_change is not None and dso_change >= 5) or (
            cycle_change is not None and cycle_change >= 10
        ):
            add(
                "WORKING_CAPITAL_DETERIORATION",
                "review",
                f"Latest period versus previous: DSO change {dso_change} days; cash-conversion-cycle change {cycle_change} days. Example triggers: +5 DSO or +10 cycle days.",
                latest,
                previous,
            )
    for row in rows("profile"):
        if str(row.get("nps", "")).lower() == "detractor":
            add(
                "DETRACTOR_NPS",
                "review",
                "The supplied NPS category is Detractor; verify the source date and recovery plan.",
                row,
            )
    return sorted(findings, key=lambda item: (item["priority"] != "urgent", item["code"]))
