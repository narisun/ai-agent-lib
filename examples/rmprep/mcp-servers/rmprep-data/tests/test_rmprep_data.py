"""Exercise real CSV queries, authorization, date cutoffs and financial arithmetic."""

from __future__ import annotations

import csv
from collections import defaultdict
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from agentcraft2_core import ServiceContainer
from agentcraft2_core.integrations.mcp import mcp_result_text, verify_registration
from agentcraft2_core.testing import TEST_AGENT, audit_records, load_test_config
from agentcraft2_core.testing.mcp import call_tool_as
from mcp.server.mcpserver import MCPServer

from rmprep_data import APPLICATION, SERVER_ID, build_server
from rmprep_data.sections import ClientSection, ReferenceSection

SERVICE = Path(__file__).resolve().parents[1]
DATA = SERVICE.parents[1] / "data"
MEETING = "2026-08-03"
Running = tuple[ServiceContainer, MCPServer[Any]]


def csv_rows(name: str) -> list[dict[str, str]]:
    with (DATA / f"{name}.csv").open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


@pytest.fixture
async def running(tmp_path: Path) -> AsyncIterator[Running]:
    config = load_test_config(SERVICE / ".env.example", state_dir=tmp_path, test_agent=TEST_AGENT)
    async with ServiceContainer(config) as services:
        await services.validate()
        yield services, build_server(services)


async def invoke(running: Running, tool: str, **arguments: Any) -> dict[str, Any]:
    services, server = running
    result = await call_tool_as(
        services, server, f"rmprep-data.{tool}", arguments, roles=["analyst"]
    )
    assert not result.is_error, mcp_result_text(result)
    return dict(result.structured_content)


async def test_registry_matches_live_tools(running: Running) -> None:
    services, server = running
    entry = services.registry.tools.get(SERVER_ID)
    assert entry is not None and entry.audience == APPLICATION
    await verify_registration(server, entry, require_pins=True)


async def test_classification_codes_preserve_leading_zeroes(running: Running) -> None:
    page = await invoke(
        running, "get_client_records", uen="WAWO-0046", section="profile", end_date=MEETING
    )
    assert page["records"][0]["sic_code"] == "0100"
    assert page["records"][0]["naics_code"] == "111339"


@pytest.mark.parametrize(
    ("search", "status", "uen"),
    [
        ("jone-0022", "resolved", "JONE-0022"),
        ("Jones Dairy Farm", "resolved", "JONE-0022"),
        ("jones dairy", "resolved", "JONE-0022"),
        ("a", "ambiguous", None),
        ("unknown client", "not_found", None),
        ("' OR '1'='1", "not_found", None),
    ],
)
async def test_lookup_resolves_only_unambiguous_clients(
    running: Running, search: str, status: str, uen: str | None
) -> None:
    result = await invoke(running, "find_clients", search=search)
    assert (result["status"], result["resolved_uen"]) == (status, uen)


@pytest.mark.parametrize("section", list(ClientSection))
async def test_every_client_section_is_scoped_and_has_provenance(
    running: Running, section: ClientSection
) -> None:
    page = await invoke(
        running, "get_client_records", uen="JONE-0022", section=section.value, end_date=MEETING
    )
    assert page["section"] == section.value
    assert len(page["records"]) <= 50
    assert page["source"] is not None
    for row in page["records"]:
        assert row.get("uen", "JONE-0022") == "JONE-0022"
        assert row["source_ref"].startswith(page["source_table"] + "[")
        if "facility_id" in row:
            facilities = {
                r["facility_id"] for r in csv_rows("facilities") if r["uen"] == "JONE-0022"
            }
            assert row["facility_id"] in facilities
    empty = await invoke(
        running,
        "get_client_records",
        uen="JONE-0022' OR '1'='1",
        section=section.value,
        end_date=MEETING,
    )
    assert empty["records"] == []


@pytest.mark.parametrize("section", list(ReferenceSection))
async def test_reference_sections_exclude_scenario_answers(
    running: Running, section: ReferenceSection
) -> None:
    page = await invoke(running, "get_reference_data", section=section.value, end_date=MEETING)
    assert page["records"]
    assert "expected_signal_codes" not in str(page)
    assert "rm_scenarios" not in str(page)


@pytest.mark.parametrize("scenario", csv_rows("rm_scenarios"), ids=lambda row: row["scenario_id"])
async def test_business_scenarios_are_derived_from_evidence(
    running: Running, scenario: dict[str, str]
) -> None:
    # Evaluation answers are read only by the tests, never by the running server.
    pack = await invoke(
        running, "get_memo_evidence", uen=scenario["uen"], meeting_date=scenario["as_of_date"]
    )
    assert pack["status"] == "found"
    codes = {finding["code"] for finding in pack["review_findings"]}
    expected = set(filter(None, scenario["expected_signal_codes"].split(";")))
    if expected:
        assert expected <= codes
    else:
        assert codes == set(), pack["review_findings"]
    refs = {row["source_ref"] for page in pack["sections"].values() for row in page["records"]}
    assert all(set(finding["source_refs"]) <= refs for finding in pack["review_findings"])
    assert "expected_signal_codes" not in str(pack)
    assert any("No news feed" in note for note in pack["limitations"])


async def test_future_financials_and_unknown_clients_are_not_memo_evidence(
    running: Running,
) -> None:
    pack = await invoke(running, "get_memo_evidence", uen="JONE-0022", meeting_date=MEETING)
    facts = pack["sections"]["financials"]["records"]
    assert facts and all(row["period_end"] <= MEETING for row in facts)
    assert all(row["period_end"] != "2026-12-31" for row in facts)
    missing = await invoke(running, "get_memo_evidence", uen="missing", meeting_date=MEETING)
    assert missing["status"] == "not_found" and not missing["sections"]


async def test_jones_memo_and_detail_pages_include_verified_measurements(running: Running) -> None:
    pack = await invoke(running, "get_memo_evidence", uen="JONE-0022", meeting_date=MEETING)
    sections = pack["sections"]
    gross_profit = next(
        item for item in sections["financials"]["comparisons"] if item["metric"] == "GrossProfit"
    )
    assert gross_profit["previous_value"] == "63172556.33"
    assert gross_profit["current_value"] == "43283347.46"
    assert gross_profit["percent_change"] == "-31.48"
    assert sections["financials"]["evidence_limits"]["audit_status"] == "not_supplied"

    detail = await invoke(
        running, "get_client_records", uen="JONE-0022", section="working_capital", end_date=MEETING
    )
    assert detail["comparisons"] == sections["working_capital"]["comparisons"]
    changes = {item["metric"]: item for item in detail["comparisons"]}
    for metric, change, unit in (
        ("dso_days", "11.00", "days"),
        ("cash_conversion_cycle_days", "19.00", "days"),
        ("ar_over_90_pct", "9.00", "percentage_points"),
        ("net_working_capital", "-7000000.00", "USD"),
    ):
        assert changes[metric]["absolute_change"] == change
        assert changes[metric]["change_unit"] == unit
        assert changes[metric]["causes"] == "not_established"
    for section in (sections["financials"], detail):
        references = {row["source_ref"] for row in section["records"]}
        assert all(set(item["source_refs"]) <= references for item in section["comparisons"])

    exceptions = sections["credit_exceptions"]
    assert {row["exception_id"]: row["days_overdue"] for row in exceptions["records"]} == {
        "CE-JONE-001": 26,
        "CE-JONE-002": 34,
    }
    assert exceptions["evidence_limits"]["waiver_execution"] == "not_established_by_this_source"
    assert exceptions["evidence_limits"]["waiver_non_execution"] == (
        "not_established_by_status_requirement_or_missing_receipt"
    )
    assert exceptions["evidence_limits"]["waiver_execution_blockers"] == (
        "not_supplied_no_causal_link_to_other_exceptions"
    )
    assert sections["commitments"]["records"][0]["days_overdue"] == 10
    assert sections["commitments"]["evidence_limits"]["received_date_field"] == (
        "not_present_in_this_schema"
    )
    coverage = sections["payment_totals"]["coverage"]
    assert (coverage["requested_start"], coverage["requested_end"]) == ("2025-08-03", MEETING)
    assert coverage["last_observed_date"] == "2026-06-28"
    assert coverage["ledger_completeness"] == "not_established"


async def test_maturity_and_availability_use_the_requested_meeting_date(running: Running) -> None:
    pack = await invoke(running, "get_memo_evidence", uen="SHIN-0039", meeting_date=MEETING)
    facilities = pack["sections"]["facilities"]["records"]
    target = next(row for row in facilities if row["facility_id"] == "FAC000000020")
    assert target["days_to_maturity"] == 25
    availability = pack["sections"]["borrowing_base"]["records"]
    assert availability[0]["availability_pct_of_commitment"] == pytest.approx(7.62)
    pack = await invoke(running, "get_memo_evidence", uen="WAWO-0046", meeting_date=MEETING)
    assert pack["sections"]["borrowing_base"]["records"][0]["availability_pct_of_commitment"] == 4.8


async def test_payment_totals_match_raw_transactions_without_currency_mixing(
    running: Running,
) -> None:
    start, end, uen = "2025-08-03", MEETING, "CANA-0010"
    page = await invoke(
        running,
        "get_client_records",
        uen=uen,
        section="payment_totals",
        start_date=start,
        end_date=end,
    )
    assert not page["truncated"]
    expected: dict[str, Decimal] = defaultdict(Decimal)
    counts: dict[str, int] = defaultdict(int)
    for row in csv_rows("transactions"):
        if row["uen"] == uen and start <= row["value_date"] <= end:
            expected[row["currency"]] += Decimal(row["amount"])
            counts[row["currency"]] += 1
    actual: dict[str, Decimal] = defaultdict(Decimal)
    actual_counts: dict[str, int] = defaultdict(int)
    for row in page["records"]:
        actual[row["currency"]] += Decimal(str(row["total_value"]))
        actual_counts[row["currency"]] += row["transaction_count"]
    assert actual == expected and actual_counts == counts
    assert len(actual) > 1 and "CAD" in actual


async def test_transaction_pages_are_complete_and_do_not_repeat_rows(running: Running) -> None:
    ids: list[str] = []
    offset = 0
    while True:
        page = await invoke(
            running,
            "get_client_records",
            uen="CANA-0010",
            section="transactions",
            start_date="2026-01-01",
            end_date=MEETING,
            offset=offset,
        )
        ids.extend(row["transaction_id"] for row in page["records"])
        if not page["truncated"]:
            assert page["next_offset"] is None
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    expected = {
        row["transaction_id"]
        for row in csv_rows("transactions")
        if row["uen"] == "CANA-0010" and "2026-01-01" <= row["value_date"] <= MEETING
    }
    assert len(ids) == len(set(ids)) == len(expected)
    assert set(ids) == expected


@pytest.mark.parametrize(
    "arguments",
    [
        {"uen": "JONE-0022", "section": "transactions", "offset": -1},
        {"uen": "JONE-0022", "section": "rm_scenarios"},
        {
            "uen": "JONE-0022",
            "section": "financials",
            "start_date": "2027-01-01",
            "end_date": MEETING,
        },
    ],
)
async def test_invalid_queries_fail_before_data_access(
    running: Running, tmp_path: Path, arguments: dict[str, Any]
) -> None:
    services, server = running
    result = await call_tool_as(
        services, server, "rmprep-data.get_client_records", arguments, roles=["analyst"]
    )
    assert result.is_error
    assert not [event for event in audit_records(tmp_path) if event["event"] == "data.query"]


async def test_unauthorized_caller_is_denied_before_the_queries(
    running: Running, tmp_path: Path
) -> None:
    services, server = running
    result = await call_tool_as(
        services, server, "rmprep-data.get_memo_evidence", {"uen": "JONE-0022"}, roles=["intern"]
    )
    assert result.is_error
    assert "denied" in mcp_result_text(result)
    records = audit_records(tmp_path)
    assert len(records) == 1
    assert (records[0]["event"], records[0]["outcome"]) == ("tool.call", "denied")
