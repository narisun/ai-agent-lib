"""Deterministic comparisons and limits on what the supplied records establish.

This module accepts already-authorized pages. It neither fetches extra rows nor
infers business events from status labels. Calculations retain their source refs.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from agentcraft2_core.contracts import MASK

# These are limits of the supplied schemas, even when a row happens to be Closed.
EVIDENCE_LIMITS: dict[str, dict[str, str]] = {
    "credit_exceptions": {
        "waiver_execution": "not_established_by_this_source",
        "waiver_non_execution": "not_established_by_status_requirement_or_missing_receipt",
        "waiver_execution_blockers": "not_supplied_no_causal_link_to_other_exceptions",
        "waiver_expiry": "recorded_date_only_not_proof_of_execution",
        "syndicate_approval_conditions": "not_supplied",
        "statement_fiscal_year": "not_supplied_by_exception_record",
        "statement_audit_status": "not_supplied",
        "missing_received_date": "receipt_unknown_not_proof_of_nonreceipt",
    },
    "commitments": {
        "receipt_history": "not_supplied",
        "received_date_field": "not_present_in_this_schema",
        "open_status": "recorded_status_only_not_proof_nothing_was_delivered",
    },
    "opportunities": {
        "implementation_outcome": "not_supplied_closed_won_is_a_sales_stage",
    },
    "service_requests": {
        "implementation_outcome": "not_established_by_closed_request_status",
    },
    "operational_service_cases": {
        "implementation_outcome": "not_established_by_closed_request_status",
    },
    "financials": {
        "audit_status": "not_supplied",
        "statement_delivery_status": "not_supplied",
        "publication_date": "not_supplied",
        "causes_of_changes": "not_established_by_metric_values",
    },
    "working_capital": {
        "causes_of_changes": "not_established_by_metric_values",
        "actual_cash_flow": "not_established_by_working_capital_ratios_or_balance_changes",
    },
    "credit": {
        "contractual_thresholds": "not_supplied_use_covenant_records_for_covenant_tests",
        "ratio_methodology": "not_supplied",
        "ratio_observation_dates": "not_established_by_risk_rating_date",
    },
    "covenants": {
        "test_observation_date": "not_supplied_next_test_date_is_a_future_milestone",
        "ratio_methodology": "not_supplied",
    },
}

WORKING_CAPITAL_METRICS = {
    "dso_days": ("days", "days"),
    "dio_days": ("days", "days"),
    "dpo_days": ("days", "days"),
    "cash_conversion_cycle_days": ("days", "days"),
    "ar_over_90_pct": ("percent", "percentage_points"),
    "inventory_aging_pct": ("percent", "percentage_points"),
    "net_working_capital": ("currency", "currency"),
}


def _number(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _known(value: object) -> bool:
    return value is not None and str(value) not in {"", MASK}


def _decimal(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _comparison(
    metric: str,
    previous: dict[str, Any],
    current: dict[str, Any],
    field: str,
    unit: str,
    change_unit: str,
) -> dict[str, Any] | None:
    before, after = _number(previous.get(field)), _number(current.get(field))
    if before is None or after is None:
        return None
    change = after - before
    # Zero and negative baselines require a business interpretation; do not
    # manufacture an infinite percentage or invert an improvement's sign.
    percentage = _decimal(change / before * 100) if before > 0 else None
    return {
        "metric": metric,
        "previous_period_end": previous["period_end"],
        "current_period_end": current["period_end"],
        "previous_value": str(before),
        "current_value": str(after),
        "unit": unit,
        "absolute_change": _decimal(change),
        "change_unit": change_unit,
        "percent_change": percentage,
        "percent_change_note": "rounded_half_up_to_2_decimal_places"
        if percentage is not None
        else "unavailable_for_nonpositive_baseline",
        "source_refs": [previous["source_ref"], current["source_ref"]],
        "causes": "not_established",
    }


def _financial_comparisons(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[object, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        keys = ("uen", "concept", "statement", "unit", "is_annual", "period_end")
        if not all(_known(row.get(key)) for key in keys):
            continue
        # Annual and interim observations never enter the same comparison.
        grouped[tuple(row[key] for key in keys[:-1])].append(row)
    comparisons = []
    for rows in grouped.values():
        ordered = sorted(rows, key=lambda row: row["period_end"], reverse=True)
        if len(ordered) < 2 or ordered[0]["period_end"] == ordered[1]["period_end"]:
            continue
        current, previous = ordered[:2]
        comparison = _comparison(
            str(current["concept"]),
            previous,
            current,
            "value",
            str(current["unit"]),
            str(current["unit"]),
        )
        if comparison is not None:
            comparisons.append(comparison)
    return comparisons


def _working_capital_comparisons(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if any(
        not _known(row.get(key)) for row in records for key in ("uen", "period_end", "currency")
    ):
        # Unknown dates cannot be ordered, and unknown identities/units cannot
        # establish comparable periods. Do not skip them and compare older rows.
        return []
    ordered = sorted(records, key=lambda row: row["period_end"], reverse=True)
    if len(ordered) < 2:
        return []
    current, previous = ordered[:2]
    if (
        current["currency"] != previous["currency"]
        or current.get("uen") != previous.get("uen")
        or current["period_end"] == previous["period_end"]
    ):
        return []
    comparisons = []
    for metric, (unit, change_unit) in WORKING_CAPITAL_METRICS.items():
        if unit == "currency":
            unit = change_unit = str(current["currency"])
        comparison = _comparison(metric, previous, current, metric, unit, change_unit)
        if comparison is not None:
            comparisons.append(comparison)
    return comparisons


def enrich_page(page: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    """Add interpretation metadata and calculations after policy/masking.

    Incomplete pages have no financial comparisons: an unseen row could change
    the comparison. Payment coverage describes observations, not a complete ledger.
    """
    section = page["section"]
    records = page["records"]
    page["evidence_limits"] = dict(EVIDENCE_LIMITS.get(section, {}))
    if section in {"financials", "working_capital"}:
        calculate = (
            _financial_comparisons if section == "financials" else _working_capital_comparisons
        )
        page["comparisons"] = [] if page["truncated"] else calculate(records)
        page["comparison_scope"] = (
            "unavailable_on_truncated_page"
            if page["truncated"]
            else "latest_two_comparable_periods_on_returned_page"
        )
    if section in {"credit_exceptions", "commitments"}:
        for row in records:
            raw_due = row.get("due_date")
            status = str(row.get("status") or "").lower()
            days = None
            # Only explicit open statuses qualify; null/masked/unrecognized
            # states cannot establish that an item remains overdue.
            if _known(raw_due) and status in {"open", "overdue", "in progress", "pending"}:
                days = max(0, (end - date.fromisoformat(str(raw_due))).days)
            row["days_overdue"] = days
    if section == "payment_totals":
        first_dates = [r["first_value_date"] for r in records if _known(r.get("first_value_date"))]
        last_dates = [r["last_value_date"] for r in records if _known(r.get("last_value_date"))]
        first, last = min(first_dates, default=None), max(last_dates, default=None)
        page["coverage"] = {
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "first_observed_date": first,
            "last_observed_date": last,
            "scope": "returned_groups_only"
            if page["truncated"]
            else "all_returned_currency_groups",
            "ledger_completeness": "not_established",
            "note": "State requested window AND last observed transaction date. Missing later rows do not prove zero activity.",
        }
    return page
