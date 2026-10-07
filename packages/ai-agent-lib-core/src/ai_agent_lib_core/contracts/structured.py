"""Structured output: a schema a model reply must satisfy before it is used."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from ai_agent_lib_core.contracts.errors import ValidationFailed

__all__ = ["StructuredOutput"]

_NAME = re.compile(r"[^A-Za-z0-9_-]")
_MAX_REPORTED = 5


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
        self._name = _NAME.sub("_", name or fallback)[:64]
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
        problems = sorted(
            {
                f"{error.json_path}: {error.validator}"
                for error in self._validator.iter_errors(value)
            }
        )
        if problems:
            shown = "; ".join(problems[:_MAX_REPORTED])
            more = (
                f" and {len(problems) - _MAX_REPORTED} more"
                if len(problems) > _MAX_REPORTED
                else ""
            )
            raise ValidationFailed(f"the output does not match the schema ({shown}{more})")
        if self._model is None:
            return value
        try:
            return self._model.model_validate(value)
        except PydanticValidationError as exc:
            places = sorted(
                {".".join(str(part) for part in error["loc"]) or "<root>" for error in exc.errors()}
            )
            raise ValidationFailed(
                f"the output does not match the model ({', '.join(places[:_MAX_REPORTED])})"
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
        except (ValueError, RecursionError):
            raise ValidationFailed("the output is not strict JSON") from None
        return self.validate(value)

    def __repr__(self) -> str:
        return f"StructuredOutput(name={self._name!r})"
