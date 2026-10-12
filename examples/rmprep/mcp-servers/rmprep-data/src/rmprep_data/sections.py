"""Allowlisted evidence sections and their provenance; SQL lives in queries/."""

from dataclasses import dataclass
from enum import StrEnum


class ClientSection(StrEnum):
    """Sections available through the corresponding MCP tool."""

    PROFILE = "profile"
    RELATIONSHIP = "relationship"
    COMPANY = "company"
    CONTACTS = "contacts"
    ACTIVITIES = "activities"
    OPPORTUNITIES = "opportunities"
    CREDIT = "credit"
    FACILITIES = "facilities"
    COVENANTS = "covenants"
    UTILIZATION = "utilization"
    BORROWING_BASE = "borrowing_base"
    CREDIT_EXCEPTIONS = "credit_exceptions"
    WORKING_CAPITAL = "working_capital"
    COMMITMENTS = "commitments"
    TREASURY_CONTROLS = "treasury_controls"
    SOURCE_READINESS = "source_readiness"
    SERVICE_REQUESTS = "service_requests"
    OPERATIONAL_SERVICE_CASES = "operational_service_cases"
    DEPOSITS = "deposits"
    DEPOSIT_HISTORY = "deposit_history"
    FINANCIALS = "financials"
    PROFITABILITY = "profitability"
    TRANSACTIONS = "transactions"
    PAYMENT_SUMMARIES = "payment_summaries"
    COUNTERPARTIES = "counterparties"
    BENCHMARKS = "benchmarks"
    PAYMENT_TOTALS = "payment_totals"


class ReferenceSection(StrEnum):
    """Sections available through the corresponding MCP tool."""

    CATALOG = "catalog"
    RATES = "rates"
    FX = "fx"
    COMMODITIES = "commodities"
    FORWARD_CURVE = "forward_curve"
    XREF = "xref"
    DATA_MODEL = "data_model"


@dataclass(frozen=True)
class SectionSpec:
    """Where to look up a row and how to cite it."""

    table: str
    keys: tuple[str, ...]
    note: str


SECTIONS = {
    "profile": SectionSpec(
        "accounts",
        ("uen",),
        "Current CRM snapshot; no observation date supplied. Revenue is client sales in USD; currency is the primary operating currency.",
    ),
    "relationship": SectionSpec(
        "relationship",
        ("uen",),
        "Current relationship snapshot without an as-of date. nii/nir are bank-attributed income in USD, not client sales or payment fees. Do not infer freshness from review dates.",
    ),
    "company": SectionSpec(
        "company",
        ("uen",),
        "Company lookup is supplied data, not independently verified filings. Blank CIK means no filing identifier is supplied.",
    ),
    "contacts": SectionSpec(
        "contacts",
        ("uen", "first_name", "last_name", "title"),
        "Contact snapshot has no observation date. Use only contact details relevant to the request.",
    ),
    "activities": SectionSpec("activities", ("activity_id",), ""),
    "opportunities": SectionSpec(
        "opportunities",
        ("opportunity_id",),
        "Status is the current supplied snapshot, not a historical status reconstruction. Amount is opportunity value, not booked bank revenue.",
    ),
    "credit": SectionSpec(
        "credit_metrics",
        ("uen",),
        "Rating date is supplied; other ratios may have different effective dates. Keep covenant-specific tested values separate.",
    ),
    "facilities": SectionSpec(
        "facilities",
        ("facility_id",),
        "Facility amounts are USD per data_model.csv. No balance as-of date is supplied; maturity countdown uses the requested end date. our_hold differs from commitment on syndications.",
    ),
    "covenants": SectionSpec(
        "covenants",
        ("facility_id", "name"),
        "No test observation date is supplied; next_test_date is not the test date. Use in_compliance as the recorded verdict.",
    ),
    "utilization": SectionSpec(
        "utilization",
        ("facility_id", "as_of"),
        "Dated monthly balances; not necessarily the same snapshot as facilities.outstanding. Do not infer financial health from utilization alone.",
    ),
    "borrowing_base": SectionSpec(
        "borrowing_base",
        ("facility_id", "as_of"),
        "availability is supplied excess availability after reserves. Percentage is calculated only when certificate currency matches the USD facility commitment; missing conversion stays null.",
    ),
    "credit_exceptions": SectionSpec("credit_exceptions", ("exception_id",), ""),
    "working_capital": SectionSpec(
        "working_capital",
        ("uen", "period_end"),
        "Compare consecutive source periods in the same currency; empty history is a gap, not zero.",
    ),
    "commitments": SectionSpec("commitments", ("commitment_id",), ""),
    "treasury_controls": SectionSpec("treasury_controls", ("control_id",), ""),
    "source_readiness": SectionSpec(
        "source_readiness",
        ("readiness_id",),
        "Missing readiness rows mean readiness has not been assessed here; they do not establish that a domain is ready.",
    ),
    "service_requests": SectionSpec(
        "service_requests",
        ("uen", "opened_date", "request", "product"),
        "Current supplied status; an open row may be old. Do not infer a new incident from an old open case.",
    ),
    "operational_service_cases": SectionSpec(
        "operational_service_cases",
        ("uen", "opened_date", "request", "product"),
        "Additional operational cases. Preserve provenance separately from service_requests; do not double-count the same case.",
    ),
    "deposits": SectionSpec(
        "balances",
        ("uen", "as_of", "account_type", "currency"),
        "Latest available deposit month. Keep native currencies separate. Average collected balances, end balances and idle estimates have different meanings.",
    ),
    "deposit_history": SectionSpec(
        "balances",
        ("uen", "as_of", "account_type", "currency"),
        "Monthly average balances cannot be summed across months as a current balance. Preserve currency and account type.",
    ),
    "financials": SectionSpec(
        "financial_facts",
        ("uen", "concept", "period_end", "statement", "is_annual"),
        "Future period ends are excluded. No filing/publication date is supplied, so a period cutoff does not prove information was available historically. Preserve unit, fiscal year and annual/interim flag.",
    ),
    "profitability": SectionSpec(
        "product_profitability",
        ("uen", "period", "line_of_business", "product"),
        "All supplied period labels are returned; no exact observation date is supplied. USD attributed income, expense, risk cost and capital; do not combine FY and overlapping TTM periods.",
    ),
    "transactions": SectionSpec(
        "transactions",
        ("transaction_id",),
        "Each amount and fee is in its own currency. For totals use payment_totals instead of summing a truncated page.",
    ),
    "payment_summaries": SectionSpec(
        "payment_summaries",
        ("uen", "rail", "direction", "period_end", "currency"),
        "Source summary totals can mix native currencies; currency is only the modal bucket label. Do not present these totals as a single-currency amount. Use transaction-derived payment_totals.",
    ),
    "counterparties": SectionSpec(
        "counterparties",
        ("uen", "counterparty", "direction", "rail", "currency"),
        "No measurement period is supplied. Dictionary says annual_value is USD while currency describes the corridor; verify this mapping before currency-based calculations.",
    ),
    "benchmarks": SectionSpec(
        "benchmarks",
        ("naics_industry", "indicator"),
        "Matched by first five NAICS digits. No observation date or cohort methodology is supplied; preserve units. Missing comparison stays missing.",
    ),
    "catalog": SectionSpec(
        "catalog",
        ("product_id",),
        "Reference snapshot; dates, units and source labels are supplied values, not live retrieval. Catalog thresholds and pricing are indicative, not product approval.",
    ),
    "rates": SectionSpec(
        "rates",
        ("name", "as_of"),
        "Reference snapshot; dates, units and source labels are supplied values, not live retrieval. Catalog thresholds and pricing are indicative, not product approval.",
    ),
    "fx": SectionSpec(
        "fx",
        ("pair", "as_of"),
        "Reference snapshot; dates, units and source labels are supplied values, not live retrieval. Catalog thresholds and pricing are indicative, not product approval.",
    ),
    "commodities": SectionSpec(
        "commodities",
        ("name", "as_of"),
        "Reference snapshot; dates, units and source labels are supplied values, not live retrieval. Catalog thresholds and pricing are indicative, not product approval.",
    ),
    "forward_curve": SectionSpec(
        "forward_curve",
        ("curve", "tenor", "as_of"),
        "Reference snapshot; dates, units and source labels are supplied values, not live retrieval. Catalog thresholds and pricing are indicative, not product approval.",
    ),
    "xref": SectionSpec(
        "xref",
        ("xref_id",),
        "Reference snapshot; dates, units and source labels are supplied values, not live retrieval. Catalog thresholds and pricing are indicative, not product approval.",
    ),
    "data_model": SectionSpec(
        "data_model",
        ("table", "column"),
        "Reference snapshot; dates, units and source labels are supplied values, not live retrieval. Catalog thresholds and pricing are indicative, not product approval.",
    ),
    "payment_totals": SectionSpec(
        "transactions",
        ("uen", "rail", "direction", "currency", "first_value_date", "last_value_date"),
        "Transaction-derived totals for the requested date window, separated by native currency. Gross fees are not the bank-attributed NIR. No currency conversion is applied.",
    ),
}
