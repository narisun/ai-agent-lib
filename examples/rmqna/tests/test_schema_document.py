"""The schema document the agent reasons over: rendered, complete, and honest about values.

Everything here reads files of this workspace only (the data copy, the notes, the committed
document), so it runs the same under the workspace's pytest and the repository root's. The
build script is loaded by path under a private module name: it is a tool, not a package of
the agent, and neither pytest invocation puts the workspace root on ``sys.path``.
"""

from __future__ import annotations

import csv
import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import duckdb
import pytest

from rmqna.prompts import schema_document

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
NOTES = ROOT / "tools" / "schema_notes.toml"
COMMITTED = ROOT / "agents" / "rmqna" / "src" / "rmqna" / "prompts" / "schema_v1.md"

# Never documented as tables: the dictionary itself, and rmprep's eval answer key.
NOT_CONTENT = {"data_model", "rm_scenarios"}


def _build_schema() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rmqna_build_schema", ROOT / "tools" / "build_schema.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their own module by name
    spec.loader.exec_module(module)
    return module


def _render(data_dir: Path = DATA, notes_path: Path = NOTES) -> str:
    return str(_build_schema().render(data_dir, notes_path))


def _dictionary() -> list[dict[str, str]]:
    with (DATA / "data_model.csv").open(newline="", encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle) if row["table"] not in NOT_CONTENT]


def _distinct(table: str, column: str) -> list[Any]:
    connection = duckdb.connect()
    try:
        relation = connection.read_csv(str(DATA / f"{table}.csv"))
        relation.create_view("t")
        rows = connection.execute(
            f'SELECT DISTINCT "{column}" FROM t WHERE "{column}" IS NOT NULL ORDER BY 1'
        ).fetchall()
    finally:
        connection.close()
    return [row[0] for row in rows]


def _literal(value: Any) -> str:
    """How the document spells a string value: as a SQL literal."""
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return str(value)


def _section(document: str, title: str) -> str:
    """The text under one ``## title`` heading, up to the next ``## `` heading."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", document, re.S | re.M)
    assert match, f"no section {title!r}"
    return match.group(1)


def _table_block(document: str, table: str) -> str:
    match = re.search(rf"^### {re.escape(table)}\n(.*?)(?=^#{{2,4}} |\Z)", document, re.S | re.M)
    assert match, f"no block for table {table!r}"
    return match.group(1)


def _column_line(document: str, table: str, column: str) -> str:
    for line in _table_block(document, table).splitlines():
        if line.startswith(f"- {column} "):
            return line
    raise AssertionError(f"{table}.{column} is not documented")


def test_rendered_document_matches_the_committed_file() -> None:
    # The generated-docs convention: regenerate, compare byte for byte. A change to the data,
    # the dictionary or the notes that was not re-rendered fails here.
    assert _render() == COMMITTED.read_text(encoding="utf-8")


def test_the_agent_loads_the_committed_document_as_a_packaged_resource() -> None:
    assert schema_document() == COMMITTED.read_text(encoding="utf-8")


def test_every_content_table_is_documented_and_only_those() -> None:
    document = COMMITTED.read_text(encoding="utf-8")
    on_disk = {path.stem for path in DATA.glob("*.csv")} - {"data_model"}
    documented = set(re.findall(r"^### (\w+)$", document, re.M))

    assert len(documented) == 31
    assert documented == on_disk
    assert documented == {row["table"] for row in _dictionary()}
    # Metadata and the answer key are not queryable data, so they are not even mentioned.
    for name in NOT_CONTENT:
        assert name not in document


def _expected_edge(row: dict[str, str]) -> str:
    match = re.fullmatch(r"\w+\.(\w+)(?:\.(\w+))?(?: \((.+)\))?", row["fk"])
    assert match, row["fk"]
    target_table, target_column, qualifier = match.groups()
    source = f"{row['table']}.{row['column']}"
    if target_table == "xref":
        return f"- {source} → xref.xref_name (xref_type = '{qualifier}')"
    target = f"{target_table}.{target_column}" if target_column else target_table
    return f"- {source} → {target}" + (f" ({qualifier})" if qualifier else "")


def test_fk_edges_cover_the_dictionary() -> None:
    document = COMMITTED.read_text(encoding="utf-8")
    edges = [row for row in _dictionary() if row["fk"]]
    assert len(edges) == 57  # the dictionary's 58 foreign keys, less rm_scenarios.uen

    for row in edges:
        domain = _section(document, f"Domain: {row['domain']}")
        relationships = domain.split("#### [Relationships]", 1)[1]
        assert _expected_edge(row) in relationships.splitlines(), row

    # The standing rule is stated once, in a section of its own.
    assert document.count("Join only along the paths declared") == 1


def test_low_cardinality_columns_list_all_values() -> None:
    document = COMMITTED.read_text(encoding="utf-8")

    # A stage lists exactly the canonical names xref holds for its vocabulary.
    stages = {
        row[0]
        for row in duckdb.sql(
            f"SELECT xref_name FROM read_csv('{DATA / 'xref.csv'}') "
            "WHERE xref_type = 'opportunity_stage'"
        ).fetchall()
    }
    assert set(_distinct("opportunities", "stage")) == stages
    stage_line = _column_line(document, "opportunities", "stage")
    assert "Values (all): " in stage_line
    for stage in stages:
        assert _literal(stage) in stage_line

    for table, column in [
        ("accounts", "currency"),
        ("accounts", "segment"),
        ("facilities", "facility_type"),
        ("payment_summaries", "rail"),
        ("covenants", "name"),
        ("credit_metrics", "risk_rating"),
        ("contacts", "is_primary"),
        ("catalog", "family"),
        ("service_requests", "status"),
    ]:
        line = _column_line(document, table, column)
        assert "Values (all): " in line, (table, column)
        for value in _distinct(table, column):
            rendered = str(value).lower() if isinstance(value, bool) else _literal(value)
            assert rendered in line, (table, column, value)

    # Above 24 distinct values the document gives three samples instead of a list.
    name_line = _column_line(document, "accounts", "name")
    assert "Values (e.g.): " in name_line
    assert "Values (all)" not in name_line


def test_sensitive_columns_carry_no_samples() -> None:
    document = COMMITTED.read_text(encoding="utf-8")

    for table, column in [
        ("contacts", "first_name"),
        ("contacts", "last_name"),
        ("contacts", "email"),
        ("contacts", "phone"),
        ("accounts", "relationship_manager"),
        ("opportunities", "owner"),
    ]:
        line = _column_line(document, table, column)
        assert "no example values" in line, (table, column)
        assert "Values (" not in line, (table, column)

    # The structure is still documented, and none of the values escaped into the document.
    assert _column_line(document, "contacts", "email").startswith("- email VARCHAR")
    for table, column in [
        ("contacts", "email"),
        ("contacts", "phone"),
        ("accounts", "relationship_manager"),
    ]:
        for value in _distinct(table, column):
            assert str(value) not in document, (table, column, value)


def test_columns_show_the_type_the_engine_loads() -> None:
    document = COMMITTED.read_text(encoding="utf-8")

    # naics_code is declared a string and loads as a number: SQL written for the declared type
    # (substr) would fail, so the document says what the engine sees and what was declared.
    naics = _column_line(document, "accounts", "naics_code")
    assert naics.startswith("- naics_code BIGINT (dictionary: string) ")
    assert _column_line(document, "accounts", "sic_code").startswith("- sic_code VARCHAR ")
    assert _column_line(document, "utilization", "as_of").startswith("- as_of DATE PK ")
    assert " nullable " in naics
    assert " nullable " not in _column_line(document, "accounts", "uen")


def test_the_as_of_rule_names_the_snapshot_date_of_the_data() -> None:
    document = COMMITTED.read_text(encoding="utf-8")
    latest = max(_distinct("utilization", "as_of"))
    rule = next(
        line
        for line in _section(document, "Metric definitions").splitlines()
        if line.startswith("- **as-of rule**")
    )
    assert str(latest) in rule
    assert "{snapshot_date}" not in document


def test_a_table_without_notes_fails_the_build(tmp_path: Path) -> None:
    # The notes are hand-authored: a table added to the data without a role must not render
    # silently with a blank one.
    notes = NOTES.read_text(encoding="utf-8")
    start = notes.index("[tables.xref]")
    trimmed = tmp_path / "schema_notes.toml"
    trimmed.write_text(notes[:start], encoding="utf-8")

    with pytest.raises(ValueError, match="xref"):
        _render(DATA, trimmed)


def _mini_workspace(tmp_path: Path, rows: list[dict[str, str]]) -> tuple[Path, Path]:
    """A one-table data directory and notes, to exercise the rendering rules directly."""
    data = tmp_path / "data"
    data.mkdir()
    header = "domain,table,column,datatype,nullable,pk,uk,fk,description\n"
    columns = ["id", "label", "email", "story", "gone", "n"]
    types = ["string", "string", "string", "string", "string", "integer"]
    (data / "data_model.csv").write_text(
        header
        + "".join(
            f"demo,things,{name},{kind},Y,{'Y' if name == 'id' else ''},,,About {name}.\n"
            for name, kind in zip(columns, types, strict=True)
        ),
        encoding="utf-8",
    )
    with (data / "things.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    notes = tmp_path / "notes.toml"
    notes.write_text(
        "\n".join(
            [
                "[document]",
                'intro = "Demo."',
                'conventions = ["One."]',
                'join_rule = "None."',
                "[snapshot]",
                'date_column = "things.n"',
                "[sampling]",
                "no_values = []",
                "[domains.demo]",
                'overview = "Demo domain."',
                "[tables.things]",
                'role = "Things."',
                "[metrics]",
                *(
                    f'"{name}" = "Defined on {{snapshot_date}}."'
                    for name in _build_schema().REQUIRED_METRICS
                ),
            ]
        ),
        encoding="utf-8",
    )
    return data, notes


def test_value_rules_on_a_small_table(tmp_path: Path) -> None:
    long_text = "x" * 61
    rows = [
        {
            "id": f"id-{number:02d}",
            "label": "O'Brien, Inc." if number == 1 else f"label-{number:02d}",
            "email": f"person{number}@example.com",
            "story": long_text,
            "gone": "",
            "n": str(number),
        }
        for number in range(1, 26)  # 25 distinct ids and numbers, 24 labels after the next line
    ]
    rows[24]["label"] = rows[23]["label"]
    data, notes = _mini_workspace(tmp_path, rows)
    document = _render(data, notes)

    # 24 distinct labels: all of them, as literals a filter can paste, quotes doubled.
    label = _column_line(document, "things", "label")
    assert "Values (all): 'O''Brien, Inc.', 'label-02', " in label
    assert label.count("'label-") == 23
    # 25 distinct: three samples spread over the sorted values (first, middle, last).
    assert _column_line(document, "things", "n").endswith("Values (e.g.): 1, 13, 25")
    assert _column_line(document, "things", "id").endswith(
        "Values (e.g.): 'id-01', 'id-13', 'id-25'"
    )
    # Prose, an empty column and contact details never show a value.
    assert _column_line(document, "things", "story").endswith("Values: free text")
    assert _column_line(document, "things", "gone").endswith("Values: all NULL")
    assert _column_line(document, "things", "email").endswith(
        "Values: personal data, no example values"
    )
    assert "example.com" not in document
    # The latest n is the snapshot date the metrics quote.
    assert "Defined on 25." in document
