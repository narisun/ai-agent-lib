"""Compose memo evidence from governed queries, independent of MCP and the model."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any, Protocol, cast

from agentcraft2_core import ValidationFailed
from agentcraft2_core.contracts import QueryResult

from rmprep_data.measurements import enrich_page
from rmprep_data.sections import SECTIONS, ClientSection, ReferenceSection
from rmprep_data.signals import review_findings


class QuerySource(Protocol):
    """The only infrastructure the evidence service needs."""

    async def query(self, name: str, parameters: Mapping[str, object]) -> QueryResult:
        """Execute a named, parameterized query through the data policy pipeline."""
        ...


MEMO_SECTIONS = (
    ClientSection.RELATIONSHIP,
    ClientSection.CREDIT,
    ClientSection.FACILITIES,
    ClientSection.COVENANTS,
    ClientSection.BORROWING_BASE,
    ClientSection.CREDIT_EXCEPTIONS,
    ClientSection.WORKING_CAPITAL,
    ClientSection.COMMITMENTS,
    ClientSection.TREASURY_CONTROLS,
    ClientSection.SOURCE_READINESS,
    ClientSection.SERVICE_REQUESTS,
    ClientSection.OPERATIONAL_SERVICE_CASES,
    ClientSection.DEPOSITS,
    ClientSection.FINANCIALS,
    ClientSection.PROFITABILITY,
    ClientSection.PAYMENT_TOTALS,
    ClientSection.OPPORTUNITIES,
    ClientSection.ACTIVITIES,
    ClientSection.CONTACTS,
    ClientSection.BENCHMARKS,
)


def _window(start: date | None, end: date) -> tuple[date, date]:
    first = start or date(1900, 1, 1)
    if first > end:
        raise ValidationFailed("start_date must be on or before end_date")
    return first, end


def _page(name: str, result: QueryResult, offset: int) -> dict[str, Any]:
    """Keep query metadata and stable row references alongside JSON-safe values."""
    payload = result.to_payload()
    rows = cast(list[list[Any]], payload["rows"])
    records = [dict(zip(result.columns, row, strict=True)) for row in rows]
    spec = SECTIONS[name]
    for record in records:
        key = ";".join(f"{column}={record.get(column)}" for column in spec.keys)
        record["source_ref"] = f"{spec.table}[{key}]"
    return {
        "section": name,
        "source_table": spec.table,
        "source": payload.get("source"),
        "records": records,
        "masked_columns": payload["masked_columns"],
        "truncated": result.truncated,
        "next_offset": offset + len(records) if result.truncated else None,
        "note": spec.note,
    }


class ClientEvidence:
    """Read scoped evidence, calculate review findings, and describe data gaps.

    The injected query source enforces authorization and audits each query.
    This class never opens CSV files or accepts SQL supplied by a caller.
    """

    def __init__(self, source: QuerySource) -> None:
        self._source = source

    async def find_clients(self, search: str) -> dict[str, Any]:
        """Resolve an exact UEN/name first, otherwise return all substring matches."""
        cleaned = search.strip()
        if not cleaned:
            raise ValidationFailed("supply a client name or UEN to search for")
        result = await self._source.query("find_clients", {"search": cleaned})
        matches = result.to_payload()
        rows = [
            dict(zip(result.columns, row, strict=True))
            for row in cast(list[list[Any]], matches["rows"])
        ]
        exact = [row for row in rows if row["match_rank"] == 0]
        selected = exact or rows
        resolved = (
            selected[0]["uen"] if len(selected) == 1 and (exact or not result.truncated) else None
        )
        return {
            "matches": selected,
            "resolved_uen": resolved,
            "truncated": result.truncated,
            "status": "resolved" if resolved else "ambiguous" if rows else "not_found",
        }

    async def records(
        self,
        uen: str,
        section: ClientSection,
        *,
        end_date: date,
        start_date: date | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """Return one page of evidence for exactly one UEN and an inclusive date range."""
        if offset < 0:
            raise ValidationFailed("offset must be zero or greater")
        first, last = _window(start_date, end_date)
        result = await self._source.query(
            section.value,
            {
                "uen": uen.strip().upper(),
                "start_date": first,
                "end_date": last,
                "row_offset": offset,
            },
        )
        page = {
            **_page(section.value, result, offset),
            "uen": uen.strip().upper(),
            "start_date": first.isoformat(),
            "end_date": last.isoformat(),
        }
        return enrich_page(page, first, last)

    async def reference(
        self,
        section: ReferenceSection,
        *,
        end_date: date,
        search: str = "",
        offset: int = 0,
    ) -> dict[str, Any]:
        """Read allowed reference tables; scenario answers are never an evidence source."""
        if offset < 0:
            raise ValidationFailed("offset must be zero or greater")
        result = await self._source.query(
            section.value,
            {
                "search": search.strip(),
                "start_date": date(1900, 1, 1),
                "end_date": end_date,
                "row_offset": offset,
            },
        )
        return _page(section.value, result, offset)

    async def memo(self, uen: str, meeting_date: date) -> dict[str, Any]:
        """Collect a bounded memo pack; expose omissions and use code for calculations."""
        profile = await self.records(uen, ClientSection.PROFILE, end_date=meeting_date)
        if not profile["records"]:
            return {
                "status": "not_found",
                "uen": uen,
                "sections": {},
                "message": "No client matches this UEN. Use find_clients and clarify the client.",
            }
        sections = {"profile": profile}
        for section in MEMO_SECTIONS:
            sections[section.value] = await self.records(
                uen,
                section,
                end_date=meeting_date,
                start_date=meeting_date - timedelta(days=365)
                if section is ClientSection.PAYMENT_TOTALS
                else None,
            )
        limitations = [
            "This is a supplied CSV snapshot, not live internal systems or verified external news.",
            "No news feed is supplied. External news is unavailable in this evidence pack.",
            "Dated rows after the meeting date are excluded. Undated/current-status tables cannot reconstruct historical knowledge.",
            "Financial period_end is not a publication date. No publication dates were supplied.",
            "Payment totals come from transactions grouped by native currency; source payment_summaries can mix currencies.",
            "Read each section's note and source dates. An empty section is unavailable evidence, not zero activity or a clean bill of health.",
        ]
        limitations += [
            f"{name} is truncated; retrieve further pages before an exhaustive answer."
            for name, page in sections.items()
            if page["truncated"]
        ]
        return {
            "status": "found",
            "uen": uen.strip().upper(),
            "meeting_date": meeting_date.isoformat(),
            "sections": sections,
            "review_findings": review_findings(sections, meeting_date),
            "missing_sections": [name for name, page in sections.items() if not page["records"]],
            "limitations": limitations,
        }
