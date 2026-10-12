"""Regression tests for the evidence mistakes found in the first Anthropic run."""

from copy import deepcopy
from datetime import date
from typing import Any

import pytest

from rmprep_data.measurements import enrich_page

START, END = date(2025, 8, 3), date(2026, 8, 3)


def page(section: str, *records: dict[str, Any], truncated: bool = False) -> dict[str, Any]:
    return {"section": section, "records": list(records), "truncated": truncated}


@pytest.mark.parametrize("status", ["Open", "Closed"])
def test_expiry_received_date_or_status_does_not_establish_waiver_execution(status: str) -> None:
    record: dict[str, Any] = {
        "status": status,
        "due_date": "2026-07-08",
        "waiver_expiry": "2026-08-15",
        "received_date": "2026-07-29",
        "requirement": "Execute a covenant waiver",
    }
    result = enrich_page(page("credit_exceptions", record), START, END)
    assert result["evidence_limits"]["waiver_execution"] == "not_established_by_this_source"
    assert result["evidence_limits"]["syndicate_approval_conditions"] == "not_supplied"
    assert record["days_overdue"] == (26 if status == "Open" else None)


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("financials", "audit_status", "not_supplied"),
        ("credit_exceptions", "statement_fiscal_year", "not_supplied_by_exception_record"),
        ("credit_exceptions", "missing_received_date", "receipt_unknown_not_proof_of_nonreceipt"),
        ("commitments", "received_date_field", "not_present_in_this_schema"),
        ("opportunities", "implementation_outcome", "not_supplied_closed_won_is_a_sales_stage"),
        ("service_requests", "implementation_outcome", "not_established_by_closed_request_status"),
        (
            "credit",
            "contractual_thresholds",
            "not_supplied_use_covenant_records_for_covenant_tests",
        ),
        ("working_capital", "causes_of_changes", "not_established_by_metric_values"),
    ],
)
def test_business_state_and_causality_remain_unknown(section: str, key: str, value: str) -> None:
    result = enrich_page(page(section), START, END)
    assert result["evidence_limits"][key] == value


def financials(
    before: object = "63172556.33", after: object = "43283347.46"
) -> list[dict[str, Any]]:
    return [
        {
            "uen": "client",
            "concept": "GrossProfit",
            "statement": "income_statement",
            "unit": "USD",
            "is_annual": True,
            "period_end": "2025-12-31",
            "value": after,
            "source_ref": "current",
        },
        {
            "uen": "client",
            "concept": "GrossProfit",
            "statement": "income_statement",
            "unit": "USD",
            "is_annual": True,
            "period_end": "2024-12-31",
            "value": before,
            "source_ref": "previous",
        },
    ]


def test_gross_profit_decline_is_calculated_once_using_decimal_arithmetic() -> None:
    rows = financials()
    originals = deepcopy(rows)
    result = enrich_page(page("financials", *rows), START, END)
    (comparison,) = result["comparisons"]
    assert comparison["absolute_change"] == "-19889208.87"
    assert comparison["percent_change"] == "-31.48"
    assert comparison["source_refs"] == ["previous", "current"]
    assert comparison["causes"] == "not_established"
    assert rows == originals


@pytest.mark.parametrize("before", ["0", "-100"])
def test_nonpositive_baseline_does_not_generate_a_percentage(before: str) -> None:
    result = enrich_page(page("financials", *financials(before, "50")), START, END)
    assert result["comparisons"][0]["percent_change"] is None


@pytest.mark.parametrize("value", [None, "***", "NaN", "Infinity"])
def test_missing_and_masked_numbers_do_not_become_comparisons(value: object) -> None:
    result = enrich_page(page("financials", *financials(value)), START, END)
    assert result["comparisons"] == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unit", "CAD"),
        ("is_annual", False),
        ("statement", "cash_flow"),
        ("uen", "other"),
        ("period_end", "2025-12-31"),
    ],
)
def test_incompatible_or_duplicate_periods_are_not_compared(field: str, value: object) -> None:
    rows = financials()
    rows[1][field] = value
    assert enrich_page(page("financials", *rows), START, END)["comparisons"] == []


def test_incomplete_page_does_not_present_an_exhaustive_comparison() -> None:
    result = enrich_page(page("financials", *financials(), truncated=True), START, END)
    assert result["comparisons"] == []
    assert result["comparison_scope"] == "unavailable_on_truncated_page"


@pytest.mark.parametrize("field", ["uen", "period_end", "currency"])
@pytest.mark.parametrize("value", [None, "***"])
def test_unknown_working_capital_identity_or_period_prevents_comparison(
    field: str, value: object
) -> None:
    rows: list[dict[str, Any]] = [
        {"uen": "client", "period_end": period, "currency": "USD", "dso_days": 65}
        for period in ("2026-03-31", "2026-06-30")
    ]
    rows[1][field] = value
    assert enrich_page(page("working_capital", *rows), START, END)["comparisons"] == []


def test_percentage_points_and_currency_changes_keep_their_units() -> None:
    rows = [
        {
            "uen": "client",
            "period_end": "2026-06-30",
            "currency": "USD",
            "ar_over_90_pct": 23,
            "net_working_capital": 6000000,
            "source_ref": "current",
        },
        {
            "uen": "client",
            "period_end": "2026-03-31",
            "currency": "USD",
            "ar_over_90_pct": 14,
            "net_working_capital": 13000000,
            "source_ref": "previous",
        },
    ]
    result = enrich_page(page("working_capital", *rows), START, END)
    changes = {row["metric"]: row for row in result["comparisons"]}
    assert changes["ar_over_90_pct"]["absolute_change"] == "9.00"
    assert changes["ar_over_90_pct"]["change_unit"] == "percentage_points"
    assert changes["net_working_capital"]["absolute_change"] == "-7000000.00"
    assert changes["net_working_capital"]["change_unit"] == "USD"
    rows[1]["currency"] = "CAD"
    assert enrich_page(page("working_capital", *rows), START, END)["comparisons"] == []


@pytest.mark.parametrize("truncated", [False, True])
def test_requested_payment_window_is_separate_from_observed_dates(truncated: bool) -> None:
    result = enrich_page(
        page(
            "payment_totals",
            {
                "first_value_date": "2025-08-05",
                "last_value_date": "2026-06-28",
            },
            truncated=truncated,
        ),
        START,
        END,
    )
    coverage = result["coverage"]
    assert coverage["requested_start"] == "2025-08-03"
    assert coverage["requested_end"] == "2026-08-03"
    assert coverage["last_observed_date"] == "2026-06-28"
    assert coverage["ledger_completeness"] == "not_established"
    if truncated:
        assert coverage["scope"] == "returned_groups_only"


def test_missing_payment_observations_do_not_prove_zero_activity() -> None:
    result = enrich_page(page("payment_totals"), START, END)
    assert result["coverage"]["last_observed_date"] is None
    assert result["coverage"]["ledger_completeness"] == "not_established"
