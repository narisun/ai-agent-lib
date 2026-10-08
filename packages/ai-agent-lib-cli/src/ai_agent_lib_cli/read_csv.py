"""Reading a folder of CSV files: what tables are there and what is in them.

The files are loaded the way the local data source loads them, so what this
reader sees is what a generated server will see. Nothing is written: the
caller decides what to do with what was found.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from ai_agent_lib_cli.dataplan import Example
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.proposals import ColumnInfo, TableInfo, is_plain_name
from ai_agent_lib_core.contracts import ParameterType

if TYPE_CHECKING:
    import duckdb

__all__ = [
    "CsvFolder",
    "read_csv_folder",
    "stage_csv_files",
    "table_name_for",
]

_EXAMPLES = 3
_INTEGERS = frozenset(
    {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER"}
)
_NUMBERS = frozenset({"DOUBLE", "FLOAT", "REAL"})
_TEXTS = frozenset({"VARCHAR", "TEXT", "UUID"})


@dataclass(frozen=True, slots=True)
class CsvFolder:
    """What a folder of CSV files holds.

    Attributes:
        tables: The tables, one per file, by name.
        files: The file each table was read from, by table name.
        notes: What was left out, and why, one line each.
    """

    tables: tuple[TableInfo, ...]
    files: Mapping[str, Path]
    notes: tuple[str, ...] = ()


def table_name_for(path: Path) -> str:
    """Return the table name a CSV file gets: its name in lower case, made plain."""
    name = re.sub(r"[^a-z0-9_]+", "_", path.stem.lower()).strip("_")
    return f"t_{name}" if not name or name[0].isdigit() else name


def _parameter_type(engine_type: str) -> ParameterType | None:
    kind = engine_type.upper()
    if kind in _TEXTS:
        return ParameterType.STRING
    if kind in _INTEGERS:
        return ParameterType.INTEGER
    if kind in _NUMBERS or kind.startswith("DECIMAL"):
        return ParameterType.NUMBER
    if kind == "BOOLEAN":
        return ParameterType.BOOLEAN
    if kind == "DATE":
        return ParameterType.DATE
    if kind.startswith("TIMESTAMP"):
        return ParameterType.TIMESTAMP
    return None


def _example(value: object) -> Example:
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date | datetime):
        return value.isoformat()
    return str(value)


def _column(
    connection: duckdb.DuckDBPyConnection, table: str, name: str, engine_type: str
) -> ColumnInfo:
    quoted = '"' + name.replace('"', '""') + '"'
    counted = connection.execute(
        f"SELECT count(DISTINCT {quoted}), count(*) - count({quoted}) FROM {table}"  # noqa: S608
    ).fetchone()
    distinct, nulls = (int(counted[0]), int(counted[1])) if counted is not None else (0, 0)
    common = connection.execute(
        f"SELECT {quoted} FROM {table} WHERE {quoted} IS NOT NULL "  # noqa: S608
        f"GROUP BY 1 ORDER BY count(*) DESC, 1 LIMIT {_EXAMPLES}"
    ).fetchall()
    return ColumnInfo(
        name=name.lower(),
        type=_parameter_type(engine_type),
        distinct=distinct,
        nulls=nulls,
        examples=tuple(_example(row[0]) for row in common),
    )


def read_csv_folder(folder: Path) -> CsvFolder:
    """Read every CSV file in ``folder``.

    Raises:
        CliError: If the folder is missing, holds no CSV file, two files would
            be the same table, or a file cannot be read as CSV.
    """
    import duckdb

    if not folder.is_dir():
        raise CliError(f"{folder} is not a folder")
    paths = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".csv")
    if not paths:
        raise CliError(f"{folder} holds no .csv file")
    files: dict[str, Path] = {}
    for path in paths:
        name = table_name_for(path)
        if name in files:
            raise CliError(f"{files[name].name} and {path.name} would both be the table {name!r}")
        files[name] = path

    tables: list[TableInfo] = []
    notes: list[str] = []
    connection = duckdb.connect(":memory:")
    try:
        for name, path in files.items():
            try:
                connection.read_csv(str(path)).create(name)
                described = connection.execute(f"DESCRIBE {name}").fetchall()
                counted = connection.execute(f"SELECT count(*) FROM {name}").fetchone()  # noqa: S608
            except duckdb.Error as exc:
                raise CliError(
                    f"{path.name} could not be read as CSV ({type(exc).__name__})"
                ) from None
            columns, left_out = [], []
            for column_name, engine_type, *_ in described:
                if is_plain_name(str(column_name).lower()):
                    columns.append(_column(connection, name, str(column_name), str(engine_type)))
                else:
                    left_out.append(str(column_name))
            if left_out:
                notes.append(
                    f"{path.name}: left out {len(left_out)} column(s) whose names a query cannot "
                    f"use as they are: {', '.join(left_out)}"
                )
            tables.append(TableInfo(name, tuple(columns), int(counted[0]) if counted else 0))
    finally:
        connection.close()
    return CsvFolder(tuple(tables), files, tuple(notes))


def stage_csv_files(files: Mapping[str, Path], target: Path) -> None:
    """Copy each table's file into ``target`` under the table's name."""
    target.mkdir(parents=True, exist_ok=True)
    for name, path in files.items():
        shutil.copyfile(path, target / f"{name}.csv")
