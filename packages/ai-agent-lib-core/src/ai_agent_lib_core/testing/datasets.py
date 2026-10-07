"""A small standard dataset that every data source is tested against.

The data source contract suite expects a source that holds these accounts and
offers these two queries. A SQL adapter gets them from the files written by
:func:`write_accounts_files`; :func:`fake_accounts_source` is the in-memory
equivalent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import httpx

from ai_agent_lib_core.contracts import (
    Classification,
    Clock,
    ParameterType,
    QueryDescription,
    QueryParameter,
)
from ai_agent_lib_core.testing.fakes import FakeDataSource, FakeQuery

__all__ = [
    "ACCOUNT_COLUMNS",
    "ACCOUNT_ENDPOINTS",
    "ACCOUNT_QUERIES",
    "ACCOUNT_ROWS",
    "accounts_api",
    "fake_accounts_source",
    "write_accounts_endpoints",
    "write_accounts_files",
]

ACCOUNT_COLUMNS: tuple[str, ...] = ("account_id", "holder", "region", "balance")

ACCOUNT_ROWS: tuple[tuple[int, str, str, float], ...] = (
    (4411, "Ann", "east", 1250.00),
    (4412, "Bo", "east", 87300.10),
    (4413, "Cy", "east", 10.00),
    (4414, "Di", "east", 500.00),
    (5520, "Eve", "west", 12004.55),
    (5521, "Fay", "west", 1.00),
)

ACCOUNT_QUERIES: Mapping[str, str] = MappingProxyType(
    {
        "accounts_by_region": (
            "-- description: Accounts in one region, with their balances.\n"
            "-- param region: string\n"
            "-- param min_balance: number = 0\n"
            "-- max_rows: 3\n"
            "-- classification: restricted\n"
            "SELECT account_id, holder, region, balance\n"
            "FROM accounts\n"
            "WHERE region = :region AND balance >= :min_balance\n"
            "ORDER BY account_id\n"
        ),
        "all_accounts": (
            "-- description: Every account.\n"
            "-- max_rows: 100\n"
            "SELECT account_id, holder, region, balance\n"
            "FROM accounts\n"
            "ORDER BY account_id\n"
        ),
    }
)
"""The standard queries as SQL files, in the Redshift dialect."""


def write_accounts_files(directory: Path) -> tuple[Path, Path]:
    """Write the dataset as a CSV file and the queries as SQL files.

    Returns:
        The data directory and the query directory.
    """
    data_dir, queries_dir = directory / "data", directory / "queries"
    data_dir.mkdir(parents=True)
    queries_dir.mkdir(parents=True)
    lines = [",".join(ACCOUNT_COLUMNS)]
    lines.extend(
        f"{number},{holder},{region},{balance:.2f}"
        for number, holder, region, balance in ACCOUNT_ROWS
    )
    (data_dir / "accounts.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for name, sql in ACCOUNT_QUERIES.items():
        (queries_dir / f"{name}.sql").write_text(sql, encoding="utf-8")
    return data_dir, queries_dir


def _by_region(bound: Mapping[str, object]) -> list[tuple[object, ...]]:
    minimum = bound["min_balance"]
    assert isinstance(minimum, Decimal)  # noqa: S101 - the declared type guarantees it
    return [
        row for row in ACCOUNT_ROWS if row[2] == bound["region"] and Decimal(str(row[3])) >= minimum
    ]


def fake_accounts_source(name: str = "accounts", clock: Clock | None = None) -> FakeDataSource:
    """Return an in-memory data source over the standard dataset."""
    by_region = FakeQuery(
        description=QueryDescription(
            name="accounts_by_region",
            description="Accounts in one region, with their balances.",
            parameters=(
                QueryParameter(name="region", type=ParameterType.STRING),
                QueryParameter(
                    name="min_balance",
                    type=ParameterType.NUMBER,
                    required=False,
                    default=Decimal(0),
                ),
            ),
            max_rows=3,
            classification=Classification.RESTRICTED,
        ),
        columns=ACCOUNT_COLUMNS,
        rows=_by_region,
    )
    everything = FakeQuery.static("all_accounts", ACCOUNT_COLUMNS, ACCOUNT_ROWS)
    return FakeDataSource(name, [by_region, everything], clock=clock)


ACCOUNT_ENDPOINTS: Mapping[str, str] = MappingProxyType(
    {
        "accounts_by_region": (
            "description: Accounts in one region, with their balances.\n"
            "path: /v1/regions/{region}/accounts\n"
            "parameters:\n"
            "  region: {type: string, in: path}\n"
            "  min_balance: {type: number, default: 0}\n"
            "max_rows: 3\n"
            "classification: restricted\n"
            "rows: data.items\n"
            "columns: {account_id: id, holder: owner.name, region: region, balance: balance}\n"
            "as_of: data.as_of\n"
        ),
        "all_accounts": (
            "description: Every account.\n"
            "path: /v1/accounts\n"
            "max_rows: 100\n"
            "rows: data.items\n"
            "columns: {account_id: id, holder: owner.name, region: region, balance: balance}\n"
        ),
    }
)
"""The standard queries as endpoint definitions for the ``rest`` data source."""

_BY_REGION = re.compile(r"/v1/regions/([^/]+)/accounts$")


def write_accounts_endpoints(directory: Path, **extra: str) -> Path:
    """Write the standard endpoint definitions, plus any ``extra`` ones, and return the folder."""
    queries_dir = directory / "endpoints"
    queries_dir.mkdir(parents=True)
    for name, text in {**ACCOUNT_ENDPOINTS, **extra}.items():
        (queries_dir / f"{name}.yaml").write_text(text, encoding="utf-8")
    return queries_dir


def accounts_api(request: httpx.Request) -> httpx.Response:
    """Answer the two standard endpoints the way a real API would.

    Use it as the handler of an ``httpx.MockTransport``. A prefix before the
    paths is accepted, so the base URL may have a path of its own.
    """
    records = [
        {"id": number, "owner": {"name": holder}, "region": region, "balance": balance}
        for number, holder, region, balance in ACCOUNT_ROWS
    ]
    by_region = _BY_REGION.search(request.url.path)
    if by_region is not None:
        minimum = float(request.url.params.get("min_balance", "0"))
        records = [
            record
            for record in records
            if record["region"] == by_region.group(1) and record["balance"] >= minimum  # type: ignore[operator]
        ]
    elif not request.url.path.endswith("/v1/accounts"):
        return httpx.Response(404, json={"error": "no such resource"})
    return httpx.Response(
        200, json={"data": {"items": records, "as_of": "2026-03-01T06:00:00+00:00"}}
    )
