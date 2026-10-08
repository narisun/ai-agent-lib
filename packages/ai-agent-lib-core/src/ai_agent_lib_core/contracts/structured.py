"""Structured output: a schema a model reply must satisfy before it is used."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as SchemaViolation
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from ai_agent_lib_core.contracts.errors import ValidationFailed, kind_of
from ai_agent_lib_core.contracts.redaction import redact, shorten

__all__ = ["SchemaMismatch", "StructuredOutput", "schema_mismatch"]

_NAME = re.compile(r"[^A-Za-z0-9_-]")
_MAX_NAME = 64


def model_safe_name(name: str) -> str:
    """Return ``name`` as model APIs accept a tool name: other characters become ``_``.

    ``directory.people_by_team`` becomes ``directory_people_by_team``, and the
    name is cut to 64 characters. MCP tools and structured outputs are offered
    to a model under these names.
    """
    return _NAME.sub("_", name)[:_MAX_NAME]


_MAX_REPORTED = 5
_MAX_DETAIL = 1_000

_BOUNDS = {
    "maxLength": "at most {} characters",
    "minLength": "at least {} characters",
    "maximum": "at most {}",
    "minimum": "at least {}",
    "exclusiveMaximum": "less than {}",
    "exclusiveMinimum": "more than {}",
    "maxItems": "at most {} items",
    "minItems": "at least {} items",
    "pattern": "text matching {}",
    "format": "text in the format {}",
}


def _wanted(violation: SchemaViolation) -> str:
    """Say what the schema wanted at one place, from the schema alone."""
    rule, value = violation.validator, violation.validator_value
    if rule == "enum" and isinstance(value, list):
        return "one of " + ", ".join(json.dumps(item) for item in value)
    if rule == "const":
        return json.dumps(value)
    if rule == "type":
        return " or ".join(value) if isinstance(value, list) else str(value)
    if rule == "required" and isinstance(value, list):
        present = violation.instance if isinstance(violation.instance, Mapping) else {}
        missing = [name for name in value if name not in present]
        return "the properties " + ", ".join(repr(name) for name in missing)
    if rule == "additionalProperties":
        return "no properties other than those in the schema"
    if rule == "items" and value is False:
        return "no items beyond the ones the schema lists"
    if rule in _BOUNDS:
        return _BOUNDS[str(rule)].format(json.dumps(value))
    return f"to satisfy {rule!r}"


def _found(violation: SchemaViolation) -> str:
    """Say what was there instead, by its kind, never its value."""
    if violation.validator == "required":
        return "they are missing"
    if violation.validator == "additionalProperties" and isinstance(violation.instance, Mapping):
        return f"an object with {len(violation.instance)} properties"
    return kind_of(violation.instance)


class SchemaMismatch:
    """What did not match a schema: places, wants and finds, and the raw messages.

    Attributes:
        expected: What the schema wants at each place, from the schema alone.
        actual: What was found at each place, by kind, never by value.
        detail: The validator's own messages, which repeat the values.
    """

    def __init__(self, violations: list[SchemaViolation]) -> None:
        ordered = sorted(violations, key=lambda v: (v.json_path, str(v.validator)))
        shown = ordered[:_MAX_REPORTED]
        more = len(ordered) - len(shown)
        tail = f"; and {more} more" if more else ""
        self.count = len(ordered)
        self.expected = "; ".join(f"{v.json_path}: {_wanted(v)}" for v in shown) + tail
        self.actual = "; ".join(f"{v.json_path}: {_found(v)}" for v in shown) + tail
        self.detail = shorten(
            redact("; ".join(f"{v.json_path}: {v.message}" for v in shown)), _MAX_DETAIL
        )


def schema_mismatch(validator: Draft202012Validator, value: object) -> SchemaMismatch | None:
    """Return what in ``value`` does not match the schema, or ``None`` if it matches."""
    violations = list(validator.iter_errors(value))
    return SchemaMismatch(violations) if violations else None


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("an object repeats a key")
    return dict(pairs)


def _references(node: object) -> Iterator[str]:
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in {"$ref", "$dynamicRef"} and isinstance(value, str):
                yield value
            else:
                yield from _references(value)
    elif isinstance(node, list | tuple):
        for item in node:
            yield from _references(item)


class StructuredOutput:
    """A JSON Schema, Draft 2020-12, that a model reply is validated against.

    Build it from a schema or from a Pydantic model::

        answer = StructuredOutput(AccountAnswer)
        model = services.model().with_structured_output(answer)

    Args:
        schema: A JSON Schema whose root describes an object, or a Pydantic
            model class.
        name: The name the schema is offered to a model under. Defaults to the
            model class name or the schema's title.

    Raises:
        ValueError: If the schema is not a valid Draft 2020-12 schema, does not
            describe an object, or refers to anything outside itself.
    """

    def __init__(
        self, schema: Mapping[str, Any] | type[BaseModel], *, name: str | None = None
    ) -> None:
        self._model: type[BaseModel] | None = None
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            self._model = schema
            document: dict[str, Any] = schema.model_json_schema()
            fallback = schema.__name__
        elif isinstance(schema, Mapping):
            document = json.loads(json.dumps(dict(schema)))
            title = document.get("title")
            fallback = title if isinstance(title, str) and title else "structured_output"
        else:
            raise TypeError("schema must be a JSON Schema mapping or a Pydantic model class")
        try:
            Draft202012Validator.check_schema(document)
        except SchemaError as exc:
            raise ValueError(f"the schema is not valid Draft 2020-12: {exc.message}") from None
        if document.get("type") != "object":
            raise ValueError('the root of the schema must have "type": "object"')
        external = sorted({ref for ref in _references(document) if not ref.startswith("#")})
        if external:
            raise ValueError("the schema may only refer to its own definitions")
        self._schema = document
        self._name = model_safe_name(name or fallback)
        self._validator = Draft202012Validator(
            document, format_checker=Draft202012Validator.FORMAT_CHECKER
        )

    @property
    def name(self) -> str:
        """The name the schema is offered to a model under."""
        return self._name

    @property
    def schema(self) -> dict[str, Any]:
        """A copy of the JSON Schema."""
        copy: dict[str, Any] = json.loads(json.dumps(self._schema))
        return copy

    def tool_definition(self) -> dict[str, Any]:
        """Return the schema as a function-style tool definition for a model."""
        description = self._schema.get("description")
        return {
            "type": "function",
            "function": {
                "name": self._name,
                "description": description if isinstance(description, str) else self._name,
                "parameters": self.schema,
            },
        }

    def validate(self, value: object) -> Any:
        """Check ``value`` against the schema and return it in its final form.

        Returns:
            An instance of the Pydantic model when one was given, otherwise the
            value itself.

        Raises:
            ValidationFailed: Naming the places that do not match. The values
                themselves are not repeated: they came from a model.
        """
        mismatch = schema_mismatch(self._validator, value)
        if mismatch is not None:
            raise ValidationFailed(
                f"the output does not match the schema {self._name!r} "
                f"({mismatch.count} problem{'s' if mismatch.count != 1 else ''})",
                expected=mismatch.expected,
                actual=mismatch.actual,
                fix=(
                    "make the instructions say what each field must hold, or loosen the "
                    "schema; the detail, shown on a developer's machine, has the values"
                ),
                detail=mismatch.detail,
            )
        if self._model is None:
            return value
        try:
            return self._model.model_validate(value)
        except PydanticValidationError as exc:
            problems = exc.errors(include_url=False)[:_MAX_REPORTED]
            places = [".".join(str(part) for part in error["loc"]) or "$" for error in problems]
            pairs = list(zip(places, problems, strict=True))
            raise ValidationFailed(
                f"the output does not pass the checks of {self._model.__name__}",
                expected="; ".join(f"{place}: {error['msg']}" for place, error in pairs),
                actual="; ".join(
                    f"{place}: {kind_of(error.get('input'))}" for place, error in pairs
                ),
                detail=shorten(redact(str(exc)), _MAX_DETAIL),
            ) from None

    def parse(self, text: str) -> Any:
        """Parse ``text`` as strict JSON, then validate it.

        Strict means the whole text is one JSON value: no surrounding prose,
        no code fence, no ``NaN`` or ``Infinity``, and no repeated keys.

        Raises:
            ValidationFailed: If the text is not strict JSON or does not match.
        """
        try:
            value = json.loads(
                text, parse_constant=_reject_constant, object_pairs_hook=_reject_duplicates
            )
        except (ValueError, RecursionError) as exc:
            raise ValidationFailed(
                "the output is not strict JSON",
                expected="one JSON value and nothing else: no prose, no code fence",
                actual=shorten(str(exc), 200),
                fix="answer through the schema's tool, which with_structured_output asks for",
                detail=shorten(redact(text), _MAX_DETAIL),
            ) from None
        return self.validate(value)

    def __repr__(self) -> str:
        return f"StructuredOutput(name={self._name!r})"
