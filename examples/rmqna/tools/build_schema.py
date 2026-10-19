"""Render the schema document the rmqna agent reasons over.

Run from the workspace root::

    python -m tools.build_schema

It writes ``agents/rmqna/src/rmqna/prompts/schema_v1.md`` from three sources, each used for what
it knows best:

* ``data/data_model.csv``, the column-level dictionary: columns, keys, foreign-key edges and the
  prose that describes each column;
* the content CSVs: the type DuckDB gives each column, row counts, and the values each column
  holds (complete lists for categorical columns, samples otherwise);
* ``tools/schema_notes.toml``: what a table is for, how a domain hangs together, and the metric
  definitions. The dictionary is strictly column-level and cannot say these.

Reading the data here is build-time work. The rendered file is committed, a test regenerates it
and compares it byte for byte, and at run time the engine can never read a file.
"""

from __future__ import annotations

import csv
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import duckdb

WORKSPACE = Path(__file__).resolve().parents[1]
DATA_DIR = WORKSPACE / "data"
NOTES_PATH = Path(__file__).resolve().with_name("schema_notes.toml")
OUTPUT_PATH = WORKSPACE / "agents" / "rmqna" / "src" / "rmqna" / "prompts" / "schema_v1.md"

VERSION = 1

# The dictionary describes rmprep's eval answer key as a table. The key is not data, so the
# row is dropped here and the copy of the data does not carry the file.
DICTIONARY_ONLY = frozenset({"rm_scenarios"})

# A column with at most this many distinct values is categorical: the document lists them all.
MAX_LISTED_VALUES = 24
SAMPLE_COUNT = 3
# A value longer than this is prose, not a code. A column holding any is described as free text,
# so no sentence is cut short or quoted as if it were a value to filter on.
MAX_VALUE_CHARS = 60

# Person contact details are never sampled, wherever the column appears. Person-name columns of
# another kind are listed in the notes ([sampling] no_values).
PERSON_COLUMNS = frozenset({"email", "phone", "first_name", "last_name"})

REQUIRED_SECTIONS = ("document", "snapshot", "sampling", "domains", "tables", "metrics")
REQUIRED_METRICS = (
    "exposure",
    "utilization",
    "weighted pipeline",
    "covenant breach",
    "cash conversion cycle",
    "as-of rule",
)

# What the dictionary may call a column, mapped to the DuckDB types that agree with it. A
# column loaded as anything else is shown with the type it was declared.
COMPATIBLE_TYPES = {
    "string": {"VARCHAR"},
    "integer": {"BIGINT", "INTEGER", "HUGEINT"},
    "decimal": {"DOUBLE", "BIGINT", "INTEGER", "HUGEINT"},
    "date": {"DATE"},
    "boolean": {"BOOLEAN"},
}

_FOREIGN_KEY = re.compile(
    r"^(?P<domain>\w+)\.(?P<table>\w+)(?:\.(?P<column>\w+))?(?: \((?P<qualifier>.+)\))?$"
)


@dataclass(frozen=True)
class Column:
    """One row of the data dictionary."""

    domain: str
    table: str
    name: str
    datatype: str
    nullable: bool
    primary_key: bool
    unique: bool
    foreign_key: str
    description: str


@dataclass(frozen=True)
class Edge:
    """A foreign key of the dictionary, taken apart."""

    source_table: str
    source_column: str
    target_table: str
    target_column: str | None
    qualifier: str | None

    @property
    def target(self) -> str:
        """The target as the document spells it, with the qualifier the dictionary gave."""
        if self.target_table == "xref" and self.target_column is None and self.qualifier:
            # Vocabulary edges name the xref_type; the column holds canonical names.
            return f"xref.xref_name (xref_type = '{self.qualifier}')"
        where = self.target_table + (f".{self.target_column}" if self.target_column else "")
        return where + (f" ({self.qualifier})" if self.qualifier else "")


@dataclass(frozen=True)
class Profile:
    """What the loaded data says about one column."""

    loaded_type: str
    values: str


def render(data_dir: Path, notes_path: Path) -> str:
    """Return the schema document for the data in ``data_dir`` and the notes at ``notes_path``."""
    columns = _read_dictionary(data_dir)
    tables = _tables(columns)
    notes = _read_notes(notes_path, tables, _domains(columns), columns)
    edges = _edges(columns, set(tables))

    connection = duckdb.connect()
    try:
        for table in tables:
            connection.read_csv(str(data_dir / f"{table}.csv")).create_view(table)
        snapshot = _snapshot_date(connection, notes["snapshot"]["date_column"], columns)
        blocks = {
            table: _render_table(connection, table, cols, notes, edges.get(table, []))
            for table, cols in tables.items()
        }
    finally:
        connection.close()
    return _assemble(tables, blocks, notes, edges, snapshot)


def main() -> None:
    """Write the document to the agent's prompts package."""
    document = render(DATA_DIR, NOTES_PATH)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    # "\n" on every platform, so the committed bytes are the same everywhere.
    OUTPUT_PATH.write_text(document, encoding="utf-8", newline="\n")
    sys.stdout.write(f"wrote {OUTPUT_PATH.relative_to(WORKSPACE)}: {len(document)} characters\n")


# --------------------------------------------------------------------------- reading the inputs


def _read_dictionary(data_dir: Path) -> list[Column]:
    with (data_dir / "data_model.csv").open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if row["table"] not in DICTIONARY_ONLY]
    columns = [
        Column(
            domain=row["domain"],
            table=row["table"],
            name=row["column"],
            datatype=row["datatype"],
            nullable=row["nullable"] == "Y",
            primary_key=row["pk"] == "Y",
            unique=row["uk"] == "Y",
            foreign_key=row["fk"],
            description=" ".join(row["description"].split()),
        )
        for row in rows
    ]
    on_disk = {path.stem for path in data_dir.glob("*.csv")} - {"data_model"} - DICTIONARY_ONLY
    documented = {column.table for column in columns}
    if on_disk != documented:
        raise ValueError(
            "the dictionary and the data disagree on the tables: "
            f"only in the dictionary {sorted(documented - on_disk)}, "
            f"only on disk {sorted(on_disk - documented)}"
        )
    return columns


def _tables(columns: list[Column]) -> dict[str, list[Column]]:
    """The columns of each table, tables and columns in dictionary order."""
    tables: dict[str, list[Column]] = {}
    for column in columns:
        tables.setdefault(column.table, []).append(column)
    return tables


def _domains(columns: list[Column]) -> list[str]:
    return list(dict.fromkeys(column.domain for column in columns))


def _edges(columns: list[Column], tables: set[str]) -> dict[str, list[Edge]]:
    """Every foreign key of the dictionary, by source table, checked against the tables."""
    names = {(column.table, column.name) for column in columns}
    edges: dict[str, list[Edge]] = {}
    for column in columns:
        if not column.foreign_key:
            continue
        match = _FOREIGN_KEY.match(column.foreign_key)
        if match is None:
            raise ValueError(f"{column.table}.{column.name}: cannot read fk {column.foreign_key!r}")
        target_table = match["table"]
        target_column = match["column"]
        if target_table not in tables or (
            target_column and (target_table, target_column) not in names
        ):
            raise ValueError(
                f"{column.table}.{column.name} points at {column.foreign_key!r}, "
                "which is not in the documented tables"
            )
        edges.setdefault(column.table, []).append(
            Edge(column.table, column.name, target_table, target_column, match["qualifier"])
        )
    return edges


def _read_notes(
    path: Path, tables: dict[str, list[Column]], domains: list[str], columns: list[Column]
) -> dict[str, Any]:
    """Load the notes and fail where they and the data have drifted apart."""
    with path.open("rb") as handle:
        notes: dict[str, Any] = tomllib.load(handle)
    absent = [section for section in REQUIRED_SECTIONS if section not in notes]
    if absent:
        raise ValueError(f"schema_notes.toml has no [{']/['.join(absent)}] section")
    problems = [
        *_mismatch("[tables.*]", set(notes["tables"]), set(tables)),
        *_mismatch("[domains.*]", set(notes["domains"]), set(domains)),
        *(
            f"[metrics] has no definition of {name!r}"
            for name in REQUIRED_METRICS
            if name not in notes["metrics"]
        ),
        *(
            f"[sampling] no_values names {entry!r}, which is not a column"
            for entry in notes["sampling"]["no_values"]
            if entry not in {f"{column.table}.{column.name}" for column in columns}
        ),
    ]
    if problems:
        raise ValueError("schema_notes.toml does not match the data: " + "; ".join(problems))
    return notes


def _mismatch(label: str, noted: set[str], actual: set[str]) -> list[str]:
    problems = []
    if actual - noted:
        problems.append(f"{label} is missing {sorted(actual - noted)}")
    if noted - actual:
        problems.append(
            f"{label} has entries for {sorted(noted - actual)}, which are not documented"
        )
    return problems


def _snapshot_date(
    connection: duckdb.DuckDBPyConnection, date_column: str, columns: list[Column]
) -> str:
    table, _, column = date_column.partition(".")
    if not any(c.table == table and c.name == column for c in columns):
        raise ValueError(f"[snapshot] date_column {date_column!r} is not a column")
    row = connection.execute(f'SELECT max("{column}") FROM "{table}"').fetchone()
    if row is None or row[0] is None:
        raise ValueError(f"[snapshot] date_column {date_column!r} holds no dates")
    return str(row[0])


# ------------------------------------------------------------------------------- profiling


def _profile(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    column: Column,
    loaded_type: str,
    private: bool,
) -> Profile:
    quoted = f'"{column.name}"'
    if private:
        return Profile(loaded_type, "Values: personal data, no example values")
    present, distinct, longest = connection.execute(
        f"SELECT count({quoted}), count(DISTINCT {quoted}), max(length(CAST({quoted} AS VARCHAR))) "
        f'FROM "{table}"'
    ).fetchone() or (0, 0, 0)
    if present == 0:
        return Profile(loaded_type, "Values: all NULL")
    if longest > MAX_VALUE_CHARS:
        return Profile(loaded_type, "Values: free text")
    if distinct <= MAX_LISTED_VALUES:
        rows = connection.execute(
            f'SELECT DISTINCT {quoted} FROM "{table}" WHERE {quoted} IS NOT NULL ORDER BY 1'
        ).fetchall()
        return Profile(loaded_type, "Values (all): " + ", ".join(_literal(r[0]) for r in rows))
    # Samples are spread over the sorted distinct values (first, middle, last), so numbers and
    # dates show their range and not just their low end.
    ranks = sorted({1, (distinct + 1) // 2, distinct})[:SAMPLE_COUNT]
    wanted = ", ".join(str(rank) for rank in ranks)
    rows = connection.execute(
        f'WITH d AS (SELECT DISTINCT {quoted} AS v FROM "{table}" WHERE {quoted} IS NOT NULL), '
        "r AS (SELECT v, row_number() OVER (ORDER BY v) AS rn FROM d) "
        f"SELECT v FROM r WHERE rn IN ({wanted}) ORDER BY rn"
    ).fetchall()
    return Profile(loaded_type, "Values (e.g.): " + ", ".join(_literal(r[0]) for r in rows))


def _literal(value: Any) -> str:
    """Spell a value as DuckDB reads it, so the model can paste it into a filter."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = repr(value)
        return text.removesuffix(".0")
    if isinstance(value, int):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


# ------------------------------------------------------------------------------- rendering


def _render_table(
    connection: duckdb.DuckDBPyConnection,
    table: str,
    columns: list[Column],
    notes: dict[str, Any],
    edges: list[Edge],
) -> str:
    loaded = {row[0]: row[1] for row in connection.execute(f'DESCRIBE "{table}"').fetchall()}
    if set(loaded) != {column.name for column in columns}:
        raise ValueError(f"{table}: the dictionary's columns are not the CSV's columns")
    count = connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()
    no_values = set(notes["sampling"]["no_values"])
    edge_of = {edge.source_column: edge for edge in edges}

    keys = [column.name for column in columns if column.primary_key]
    uniques = [column.name for column in columns if column.unique]
    facts = [f"Rows: {count[0] if count else 0}."]
    if keys:
        facts.append(f"Key: {', '.join(keys)}{' (composite)' if len(keys) > 1 else ''}.")
    if uniques:
        facts.append(f"Unique: {', '.join(uniques)}.")

    lines = [
        f"### {table}",
        f"Domain: {columns[0].domain}. Role: {notes['tables'][table]['role'].strip()}",
        " ".join(facts),
    ]
    for column in columns:
        private = column.name in PERSON_COLUMNS or f"{table}.{column.name}" in no_values
        profile = _profile(connection, table, column, loaded[column.name], private)
        lines.append(_render_column(column, profile, edge_of.get(column.name)))
    return "\n".join(lines)


def _render_column(column: Column, profile: Profile, edge: Edge | None) -> str:
    parts = [f"- {column.name}", profile.loaded_type]
    if profile.loaded_type not in COMPATIBLE_TYPES.get(column.datatype, set()):
        parts[-1] += f" (dictionary: {column.datatype})"
    if column.primary_key:
        parts.append("PK")
    if column.unique:
        parts.append("UK")
    if edge is not None:
        parts.append(f"FK→{edge.target}")
    if column.nullable:
        parts.append("nullable")
    description = column.description
    if not description.endswith((".", "!", "?")):
        description += "."
    return f"{' '.join(parts)} — {description} {profile.values}"


def _render_relationships(edges: list[Edge]) -> str:
    lines = ["#### [Relationships]"]
    if not edges:
        lines.append("- none declared from the tables of this domain")
    for edge in edges:
        lines.append(f"- {edge.source_table}.{edge.source_column} → {edge.target}")
    return "\n".join(lines)


def _assemble(
    tables: dict[str, list[Column]],
    blocks: dict[str, str],
    notes: dict[str, Any],
    edges: dict[str, list[Edge]],
    snapshot: str,
) -> str:
    document = notes["document"]
    domains = list(dict.fromkeys(cols[0].domain for cols in tables.values()))
    parts = [
        f"# Schema document v{VERSION}",
        f"{document['intro'].strip()} {len(tables)} tables in {len(domains)} domains.",
        "## Conventions\n" + "\n".join(f"- {item.strip()}" for item in document["conventions"]),
        "## Join paths\n" + document["join_rule"].strip(),
        "## Metric definitions\n"
        + "\n".join(
            f"- **{name}** — {text.strip().replace('{snapshot_date}', snapshot)}"
            for name, text in notes["metrics"].items()
        ),
    ]
    for domain in domains:
        members = [table for table, cols in tables.items() if cols[0].domain == domain]
        overview = notes["domains"][domain]["overview"].strip()
        parts.append(
            f"## Domain: {domain}\n{overview}\n\n"
            + "\n\n".join(blocks[table] for table in members)
            + "\n\n"
            + _render_relationships([edge for table in members for edge in edges.get(table, [])])
        )
    return "\n\n".join(parts) + "\n"


if __name__ == "__main__":
    main()
