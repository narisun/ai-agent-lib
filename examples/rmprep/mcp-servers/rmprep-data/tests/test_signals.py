"""Review rules must not manufacture findings from missing or superseded evidence."""

from datetime import date
from typing import Any

from rmprep_data.signals import review_findings

MEETING = date(2026, 8, 3)


def test_missing_or_masked_values_do_not_become_financial_signals() -> None:
    masked: dict[str, Any] = {
        "profile": {"records": [{"nps": "***"}]},
        "credit": {
            "records": [
                {
                    "annual_review_date": None,
                    "debt_service_coverage": "***",
                    "current_ratio": "NaN",
                    "watch_status": "***",
                }
            ]
        },
        "treasury_controls": {"records": [{"status": None}, {"status": "***"}]},
        "source_readiness": {"records": [{"domain": "Credit", "quality_status": "***"}]},
        "working_capital": {
            "records": [
                {"currency": "USD", "dso_days": None},
                {"currency": "USD", "dso_days": "***"},
            ]
        },
    }
    assert review_findings(masked, MEETING) == []


def test_superseded_borrowing_base_and_readiness_do_not_create_current_alerts() -> None:
    sections = {
        "borrowing_base": {
            "records": [
                {"facility_id": "f1", "as_of": "2026-06-30", "availability_pct_of_commitment": 4.8},
                {"facility_id": "f1", "as_of": "2026-07-31", "availability_pct_of_commitment": 20},
            ]
        },
        "source_readiness": {
            "records": [
                {"domain": "Credit", "as_of_date": "2026-06-30", "quality_status": "stale"},
                {"domain": "Credit", "as_of_date": "2026-07-31", "quality_status": "ready"},
            ]
        },
    }
    assert review_findings(sections, MEETING) == []


def test_working_capital_does_not_compare_different_currencies() -> None:
    sections = {
        "working_capital": {
            "records": [
                {"currency": "CAD", "dso_days": 90, "cash_conversion_cycle_days": 100},
                {"currency": "USD", "dso_days": 30, "cash_conversion_cycle_days": 40},
            ]
        }
    }
    assert review_findings(sections, MEETING) == []


def test_a_duplicate_service_case_has_one_finding_per_code() -> None:
    case = {
        "uen": "client1",
        "request": "Restore access",
        "opened_date": "2026-07-31",
        "product": "Payments",
        "status": "Open",
        "priority": "1-Critical",
        "source_ref": "case1",
    }
    findings = review_findings(
        {
            "operational_service_cases": {"records": [case]},
            "service_requests": {"records": [dict(case, source_ref="case2")]},
        },
        MEETING,
    )
    assert len(findings) == 2
    assert {finding["code"] for finding in findings} == {
        "CRITICAL_SERVICE_ISSUE",
        "OPERATIONAL_FRICTION",
    }
    assert all(finding["source_refs"] == ["case1"] for finding in findings)
