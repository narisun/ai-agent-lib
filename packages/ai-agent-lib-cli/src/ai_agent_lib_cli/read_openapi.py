"""Reading an OpenAPI document: which operations can be offered as queries.

An operation can be offered when it is a GET that answers with JSON records
and takes only plain values. For each one this reader writes the endpoint
definition the ``rest`` data source reads, and an example response built from
the document's schemas, so the generated tests can run without the API.

Nothing is called and nothing is written: the caller decides what to do with
what was found.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from ai_agent_lib_cli.dataplan import (
    MAX_DESCRIPTION,
    MAX_QUERY_NAME,
    Example,
    PlannedParameter,
    PlannedQuery,
)
from ai_agent_lib_cli.errors import CliError
from ai_agent_lib_cli.names import PLAIN_NAME
from ai_agent_lib_cli.proposals import is_sensitive, stand_in
from ai_agent_lib_core.contracts import ParameterType, describe

__all__ = ["OpenApiReading", "read_openapi", "snake"]

_PLACEHOLDER = re.compile(r"\{([^{}]*)\}")
_MAX_COLUMNS = 40
_MAX_ROWS = 100
_MAX_DEPTH = 20
_SKIPPED_SEGMENTS = frozenset({"api", "rest"})
_VERSION = re.compile(r"^v\d+([._]\d+)*$")
Schema = Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class OpenApiReading:
    """What an OpenAPI document offers.

    Attributes:
        title: The API's title.
        base_url: The first server the document names, if it names a usable one.
        queries: One query for each operation that can be offered.
        calls: For each query, by name, what a call with the example values
            asks for, as ``"GET /path"``, and an example response to it.
        notes: What was left out, and why, one line each.
        wants_credentials: Whether the document says callers must authenticate.
    """

    title: str
    base_url: str | None
    queries: tuple[PlannedQuery, ...]
    calls: Mapping[str, tuple[str, Any]]
    notes: tuple[str, ...]
    wants_credentials: bool


def snake(name: str) -> str:
    """Return ``name`` in lower case with underscores: ``customerId`` is ``customer_id``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return re.sub(r"[^a-z0-9]+", "_", spaced.lower()).strip("_")


class _Document:
    """An OpenAPI document, with its references followed."""

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self._raw = raw

    def follow(self, node: object, depth: int = 0) -> Schema | None:
        """Return ``node`` with a reference replaced by what it points at.

        ``allOf`` parts are merged. A reference out of the document, a loop
        and a choice between shapes give ``None``: none can be read as one shape.
        """
        if not isinstance(node, Mapping) or depth > _MAX_DEPTH:
            return None
        reference = node.get("$ref")
        if reference is not None:
            return self.follow(self._target(reference), depth + 1)
        if "oneOf" in node or "anyOf" in node:
            return None
        parts = node.get("allOf")
        if isinstance(parts, list):
            merged: dict[str, Any] = {k: v for k, v in node.items() if k != "allOf"}
            properties: dict[str, Any] = dict(merged.get("properties") or {})
            for part in parts:
                followed = self.follow(part, depth + 1)
                if followed is None:
                    return None
                properties.update(followed.get("properties") or {})
                merged.setdefault("type", followed.get("type"))
            merged["properties"] = properties
            return merged
        return node

    def _target(self, reference: object) -> object:
        if not isinstance(reference, str) or not reference.startswith("#/"):
            return None
        node: object = self._raw
        for step in reference[2:].split("/"):
            if not isinstance(node, Mapping):
                return None
            node = node.get(step.replace("~1", "/").replace("~0", "~"))
        return node


def _kind(schema: Schema | None) -> str | None:
    """Return the schema's type, looking past ``null`` in a list of types."""
    if schema is None:
        return None
    declared = schema.get("type")
    if isinstance(declared, list):
        named = [item for item in declared if item != "null"]
        declared = named[0] if len(named) == 1 else None
    if declared is None and "properties" in schema:
        return "object"
    return declared if isinstance(declared, str) else None


def _scalar(schema: Schema | None) -> ParameterType | None:
    kind = _kind(schema)
    if schema is None or kind is None:
        return None
    if kind == "string":
        by_format = {"date": ParameterType.DATE, "date-time": ParameterType.TIMESTAMP}
        return by_format.get(str(schema.get("format")), ParameterType.STRING)
    return {
        "integer": ParameterType.INTEGER,
        "number": ParameterType.NUMBER,
        "boolean": ParameterType.BOOLEAN,
    }.get(kind)


def _fits(kind: ParameterType, value: object) -> bool:
    if kind is ParameterType.BOOLEAN:
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if kind is ParameterType.INTEGER:
        return isinstance(value, int)
    if kind is ParameterType.NUMBER:
        return isinstance(value, int | float)
    return isinstance(value, str) and bool(value)


def _example(kind: ParameterType, name: str, number: int, *sources: Schema | None) -> Example:
    """Return an example value: the document's own if it gives a usable one."""
    for source in sources:
        if source is None:
            continue
        candidates = [source.get("example"), source.get("default")]
        enum = source.get("enum")
        if isinstance(enum, list) and enum:
            candidates.append(enum[min(number - 1, len(enum) - 1)])
        for candidate in candidates:
            if _fits(kind, candidate) and isinstance(candidate, str | int | float | bool):
                return candidate
    return stand_in(kind, snake(name) or "value", number)


@dataclass(frozen=True, slots=True)
class _Column:
    name: str
    path: tuple[str, ...]
    kind: ParameterType
    schema: Schema


@dataclass(frozen=True, slots=True)
class _Parameter:
    name: str
    sent_as: str
    location: str
    kind: ParameterType
    example: Example
    default: Example | None = None

    def declared(self) -> str:
        parts = [f"type: {self.kind.value}", f"in: {self.location}"]
        if self.default is not None:
            parts.append(f"default: {json.dumps(self.default)}")
        if self.location != "path" and self.sent_as != self.name:
            parts.append(f"name: {json.dumps(self.sent_as)}")
        return f"  {json.dumps(self.name)}: {{{', '.join(parts)}}}"


class _LeftOutError(Exception):
    """An operation cannot be offered; the message says why."""


class _Operation:
    """One GET operation, read into a query, or refused with a reason."""

    def __init__(self, document: _Document, path: str, item: Schema, operation: Schema) -> None:
        self._document = document
        self._path = path
        self._item = item
        self._operation = operation

    def name(self) -> str:
        given = self._operation.get("operationId")
        if isinstance(given, str) and PLAIN_NAME.match(snake(given)):
            return snake(given)
        fixed = [
            snake(segment)
            for segment in self._path.split("/")
            if segment
            and not segment.startswith("{")
            and snake(segment) not in _SKIPPED_SEGMENTS
            and not _VERSION.match(segment.lower())
        ]
        keys = [snake(name) for name in _PLACEHOLDER.findall(self._path)]
        return "_".join([*fixed, *(f"by_{key}" for key in keys)]) or "root"

    def description(self) -> str:
        for key in ("summary", "description"):
            text = self._operation.get(key)
            if isinstance(text, str) and text.strip():
                line = " ".join(text.strip().splitlines()[0].split())
                if len(line) > MAX_DESCRIPTION:
                    line = line[: MAX_DESCRIPTION - 3].rsplit(" ", 1)[0] + "..."
                return line
        return f"GET {self._path}"[:MAX_DESCRIPTION]

    def parameters(self) -> tuple[list[_Parameter], int]:
        """Return the parameters a caller supplies, and how many optional ones were left out."""
        declared: dict[tuple[str, str], Schema] = {}
        for source in (self._item, self._operation):
            for raw in source.get("parameters") or ():
                parameter = self._document.follow(raw)
                if parameter is not None:
                    key = (str(parameter.get("name")), str(parameter.get("in")))
                    declared[key] = parameter
        found: list[_Parameter] = []
        left_out = 0
        for (sent_as, location), parameter in declared.items():
            required = bool(parameter.get("required")) or location == "path"
            schema = self._document.follow(parameter.get("schema"))
            kind = _scalar(schema)
            default = schema.get("default") if schema is not None else None
            usable = (
                location in {"path", "query"}
                and kind is not None
                and bool(PLAIN_NAME.match(snake(sent_as)))
            )
            if not required and not (usable and kind is not None and _fits(kind, default)):
                left_out += 1
                continue
            if location not in {"path", "query"}:
                raise _LeftOutError(
                    f"it needs the {location} parameter {sent_as!r}, which a definition cannot send"
                )
            if not usable or kind is None:
                raise _LeftOutError(
                    f"it needs the {location} parameter {sent_as!r}, which is not a plain value"
                )
            found.append(
                _Parameter(
                    name=snake(sent_as),
                    sent_as=sent_as,
                    location=location,
                    kind=kind,
                    example=_example(kind, sent_as, 1, parameter, schema),
                    default=None if required else default,
                )
            )
        names = [parameter.name for parameter in found]
        if len(names) != len(set(names)):
            raise _LeftOutError("two of its parameters would get the same name")
        in_path = {snake(name) for name in _PLACEHOLDER.findall(self._path)}
        if in_path != {p.name for p in found if p.location == "path"}:
            raise _LeftOutError("its path placeholders and its path parameters do not match")
        return found, left_out

    def shape(self) -> tuple[tuple[str, ...], Schema, bool]:
        """Return where the records are, one record's schema, and whether there is only one.

        The first is the path to the records in the response.
        """
        schema = self._document.follow(self._response_schema())
        if schema is None:
            raise _LeftOutError("its JSON response has no schema this reader can follow")
        if _kind(schema) == "array":
            record = self._document.follow(schema.get("items"))
            if _kind(record) == "object" and record is not None:
                return (), record, False
            raise _LeftOutError("it answers with a list of plain values, not of records")
        if _kind(schema) != "object":
            raise _LeftOutError("it does not answer with records")
        lists = list(self._lists(schema, ()))
        if len(lists) == 1:
            return (*lists[0][0],), lists[0][1], False
        if not lists:
            return (), schema, True
        raise _LeftOutError("its response holds more than one list of records")

    def _lists(
        self, schema: Schema, prefix: tuple[str, ...]
    ) -> Iterator[tuple[tuple[str, ...], Schema]]:
        """Yield each list of records in ``schema``, down to one level of wrapping."""
        wrappers = []
        for name, raw in (schema.get("properties") or {}).items():
            member = self._document.follow(raw)
            if _kind(member) == "array" and member is not None:
                record = self._document.follow(member.get("items"))
                if _kind(record) == "object" and record is not None:
                    yield (*prefix, str(name)), record
            elif _kind(member) == "object" and member is not None and not prefix:
                wrappers.append((str(name), member))
        if not prefix:
            for name, member in wrappers:
                yield from self._lists(member, (name,))

    def _response_schema(self) -> object:
        responses = self._operation.get("responses") or {}
        codes = sorted(str(code) for code in responses if str(code).startswith("2"))
        chosen = "200" if "200" in codes else (codes[0] if codes else "default")
        response = self._document.follow(_get(responses, chosen))
        content = (response or {}).get("content") or {}
        for media, body in content.items():
            if "json" in str(media).lower() or str(media) == "*/*":
                return (body or {}).get("schema")
        raise _LeftOutError("it does not answer with JSON")

    def columns(self, record: Schema) -> list[_Column]:
        found: dict[str, _Column] = {}

        def add(path: tuple[str, ...], schema: Schema | None) -> None:
            kind = _scalar(schema)
            name = "_".join(snake(step) for step in path)
            usable = all(step and "." not in step for step in path) and PLAIN_NAME.match(name)
            if schema is not None and kind is not None and usable and name not in found:
                found[name] = _Column(name, path, kind, schema)

        for name, raw in (record.get("properties") or {}).items():
            member = self._document.follow(raw)
            if _kind(member) == "object" and member is not None:
                for inner, inner_raw in (member.get("properties") or {}).items():
                    add((str(name), str(inner)), self._document.follow(inner_raw))
            else:
                add((str(name),), member)
        if not found:
            raise _LeftOutError("its records have no plain fields")
        return list(found.values())[:_MAX_COLUMNS]


def _get(mapping: Mapping[Any, Any], key: str) -> object:
    """Return a response by its code, which YAML may have read as a number."""
    for candidate, value in mapping.items():
        if str(candidate) == key:
            return value
    return None


def _record(columns: Sequence[_Column], number: int) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for column in columns:
        target = record
        for step in column.path[:-1]:
            target = target.setdefault(step, {})
        target[column.path[-1]] = _example(column.kind, column.path[-1], number, column.schema)
    return record


def _wrapped(rows: tuple[str, ...], records: object) -> object:
    for step in reversed(rows):
        records = {step: records}
    return records


def _definition(
    description: str,
    path: str,
    parameters: Sequence[_Parameter],
    rows: tuple[str, ...],
    columns: Sequence[_Column],
    classification: str,
    single: bool,
) -> str:
    lines = [f"description: {json.dumps(description)}", "method: GET", f"path: {json.dumps(path)}"]
    if parameters:
        lines += ["parameters:", *(parameter.declared() for parameter in parameters)]
    lines += [f"max_rows: {1 if single else _MAX_ROWS}", f"classification: {classification}"]
    if rows:
        lines.append(f"rows: {json.dumps('.'.join(rows))}")
    lines.append("columns:")
    lines += [f"  {json.dumps(c.name)}: {json.dumps('.'.join(c.path))}" for c in columns]
    if single and any(parameter.location == "path" for parameter in parameters):
        # Looking up something that is not there is an empty table, not a failure.
        lines.append("empty_on_not_found: true")
    return "\n".join(lines) + "\n"


def _read_operation(
    document: _Document, path: str, item: Schema, operation: Schema
) -> tuple[PlannedQuery, str, object, int]:
    """Return the query, the path an example call asks for, and an example response."""
    reader = _Operation(document, path, item, operation)
    name = reader.name()
    if not PLAIN_NAME.match(name) or len(name) > MAX_QUERY_NAME:
        raise _LeftOutError("no usable name can be made for it; give it an operationId")
    parameters, left_out = reader.parameters()
    rows, record, single = reader.shape()
    columns = reader.columns(record)
    masked = tuple(column.name for column in columns if is_sensitive(column.name))
    classification = "confidential" if masked else "internal"
    ours = _PLACEHOLDER.sub(lambda match: "{" + snake(match.group(1)) + "}", path)
    description = reader.description()
    examples = {p.name: p.example for p in parameters}
    called = _PLACEHOLDER.sub(lambda match: _in_a_path(examples[match.group(1)]), ours)
    records = [_record(columns, number) for number in ((1,) if single else (1, 2))]
    query = PlannedQuery(
        name=name,
        description=description,
        parameters=tuple(PlannedParameter(p.name, p.kind.value, p.example) for p in parameters),
        columns=tuple(column.name for column in columns),
        masked=masked,
        classification=classification,
        max_rows=1 if single else _MAX_ROWS,
        definition=_definition(
            description, ours, parameters, rows, columns, classification, single
        ),
    )
    return query, called, _wrapped(rows, records[0] if single else records), left_out


def _in_a_path(value: Example) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _load(path: Path) -> Mapping[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text) if path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise CliError(f"{path} cannot be read as JSON or YAML ({describe(exc)})") from exc
    if not isinstance(raw, Mapping) or not isinstance(raw.get("paths"), Mapping):
        raise CliError(f"{path} is not an OpenAPI document: it has no paths")
    if not str(raw.get("openapi", "")).startswith("3."):
        raise CliError(f"{path} is not an OpenAPI 3 document; older formats are not read")
    return raw


def _base_url(raw: Mapping[str, Any]) -> str | None:
    servers = raw.get("servers")
    first = servers[0] if isinstance(servers, list) and servers else None
    url = first.get("url") if isinstance(first, Mapping) else None
    if not isinstance(url, str) or "{" in url:
        return None
    parts = urlsplit(url)
    return url.rstrip("/") if parts.scheme in {"http", "https"} and parts.netloc else None


def read_openapi(path: Path, *, base_url: str | None = None) -> OpenApiReading:
    """Read the OpenAPI document at ``path``.

    Args:
        path: A JSON or YAML file.
        base_url: Where the API is. By default the first server the document names.

    Raises:
        CliError: If the file is not an OpenAPI 3 document.
    """
    raw = _load(path)
    document = _Document(raw)
    where = base_url or _base_url(raw)
    prefix = urlsplit(where).path.rstrip("/") if where else ""
    queries: dict[str, PlannedQuery] = {}
    calls: dict[str, tuple[str, Any]] = {}
    notes: list[str] = []
    optional = 0
    for route, raw_item in raw["paths"].items():
        item = document.follow(raw_item)
        operation = document.follow(item.get("get")) if item is not None else None
        if item is None or operation is None:
            continue
        try:
            query, called, response, left_out = _read_operation(
                document, str(route), item, operation
            )
            if query.name in queries:
                raise _LeftOutError(f"another operation is already called {query.name}")
        except _LeftOutError as reason:
            notes.append(f"left out GET {route}: {reason}")
            continue
        queries[query.name] = query
        calls[query.name] = (f"GET {prefix}{called}", response)
        optional += left_out
    if optional:
        notes.append(
            f"left out {optional} optional parameter(s) that have no default; "
            "add the ones you need to the definitions in queries/"
        )
    info = raw.get("info")
    title = info.get("title") if isinstance(info, Mapping) else None
    schemes = (raw.get("components") or {}).get("securitySchemes")
    return OpenApiReading(
        title=title if isinstance(title, str) and title.strip() else path.stem,
        base_url=where,
        queries=tuple(queries.values()),
        calls=calls,
        notes=tuple(notes),
        wants_credentials=bool(schemes) or bool(raw.get("security")),
    )
