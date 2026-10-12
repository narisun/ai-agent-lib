"""Expose client lookup, memo evidence and scoped detail through governed MCP tools."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from ai_agent_lib_core import ServiceContainer
from mcp.server.mcpserver import MCPServer
from pydantic import Field

from rmprep_data.evidence import ClientEvidence
from rmprep_data.sections import ClientSection, ReferenceSection

__all__ = ["APPLICATION", "DATA_SOURCE", "SERVER_ID", "build_server"]

APPLICATION = SERVER_ID = "rmprep-data"
DATA_SOURCE = "rmprep"


def build_server(services: ServiceContainer) -> MCPServer[Any]:
    """Inject governed query access; keep business composition separate from transport."""
    server: MCPServer[Any] = MCPServer(
        APPLICATION,
        instructions=(
            "Read the supplied RM CSV snapshot. Resolve the client UEN, then retrieve memo evidence "
            "or a specific section. Cite source_ref and source dates. Follow next_offset when needed. "
            "External news is unavailable. Scenario answer keys are excluded."
        ),
        **services.mcp_server_kwargs(SERVER_ID, application=APPLICATION),
    )
    evidence = ClientEvidence(services.data_source(DATA_SOURCE))

    @server.tool(name="rmprep-data.find_clients")
    async def find_clients(search: str) -> dict[str, Any]:
        """Find a client by UEN, exact name or a name fragment; clarify ambiguous matches."""
        return await evidence.find_clients(search)

    @server.tool(name="rmprep-data.get_memo_evidence")
    async def get_memo_evidence(uen: str, meeting_date: date | None = None) -> dict[str, Any]:
        """Get a full RM memo pack with sources, dates, calculated findings and data gaps.

        Supply an exact UEN from find_clients. meeting_date defaults to today's UTC date.
        A date cutoff filters dated facts but cannot reconstruct undated/current-status history.
        """
        return await evidence.memo(uen, meeting_date or services.clock.now().date())

    @server.tool(name="rmprep-data.get_client_records")
    async def get_client_records(
        uen: str,
        section: ClientSection,
        start_date: date | None = None,
        end_date: date | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Get a client's evidence section for Q&A, with inclusive date filters and pagination.

        Use payment_totals for currency-safe totals, transactions for individual payments,
        deposit_history for balance trends, utilization for facility history, or another enum section.
        end_date defaults to today UTC; omitted start_date includes all available earlier dates.
        Follow next_offset until null before claiming an exhaustive list. Read section notes.
        """
        return await evidence.records(
            uen,
            section,
            start_date=start_date,
            end_date=end_date or services.clock.now().date(),
            offset=offset,
        )

    @server.tool(name="rmprep-data.get_reference_data")
    async def get_reference_data(
        section: ReferenceSection,
        search: str = "",
        end_date: date | None = None,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> dict[str, Any]:
        """Read product catalog, dated market snapshots, data dictionary or vocabularies.

        search matches product/rate/commodity name, FX pair, curve, xref type, or dictionary table.
        Dated market rows after end_date are excluded. Pricing and eligibility are indicative.
        Client industry benchmarks are available through get_client_records(section='benchmarks').
        """
        return await evidence.reference(
            section,
            search=search,
            end_date=end_date or services.clock.now().date(),
            offset=offset,
        )

    return server
