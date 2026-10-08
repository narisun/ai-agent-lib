"""Checking the values a caller supplies for a named query's parameters.

Every data source, whatever it talks to, accepts parameters the same way: a
value is converted to its declared type or the call is refused.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from ai_agent_lib_core.contracts import (
    ParameterType,
    QueryDescription,
    QueryParameter,
    ValidationFailed,
    kind_of,
)
from ai_agent_lib_core.contracts.redaction import redact, shorten

__all__ = ["bind_parameters", "coerce_parameter"]


def _a(word: str) -> str:
    return f"an {word}" if word[:1] in "aeiou" else f"a {word}"


_TRUE, _FALSE = frozenset({"true", "yes"}), frozenset({"false", "no"})


class _Refused:
    """Marks a value that is not of the declared type."""


_REFUSED = _Refused()


def _string(value: object) -> object:
    return value if isinstance(value, str) else _REFUSED


def _boolean(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in _TRUE | _FALSE:
        return value.lower() in _TRUE
    return _REFUSED


def _integer(value: object) -> object:
    if isinstance(value, bool):
        return _REFUSED
    if isinstance(value, int):
        return value
    return int(value, 10) if isinstance(value, str) else _REFUSED


def _number(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal | str):
        return _REFUSED
    number = Decimal(str(value))
    return number if number.is_finite() else _REFUSED


def _timestamp(value: object) -> object:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value) if isinstance(value, str) else _REFUSED


def _date(value: object) -> object:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    return date.fromisoformat(value) if isinstance(value, str) else _REFUSED


_COERCIONS: Mapping[ParameterType, Callable[[object], object]] = {
    ParameterType.STRING: _string,
    ParameterType.BOOLEAN: _boolean,
    ParameterType.INTEGER: _integer,
    ParameterType.NUMBER: _number,
    ParameterType.TIMESTAMP: _timestamp,
    ParameterType.DATE: _date,
}


def coerce_parameter(parameter: QueryParameter, value: object) -> object:
    """Return ``value`` converted to the parameter's declared type.

    Raises:
        ValidationFailed: If the value is not of that type. The value is not
            repeated in the message: it came from a model or a user.
    """
    try:
        coerced = _COERCIONS[parameter.type](value)
    except (ValueError, InvalidOperation):
        coerced = _REFUSED
    if coerced is _REFUSED:
        raise ValidationFailed(
            f"parameter {parameter.name!r} is not {_a(parameter.type.value)}",
            expected=_a(parameter.type.value),
            actual=kind_of(value),
            detail=f"the value was {shorten(redact(repr(value)), 200)}",
        )
    return coerced


def bind_parameters(
    description: QueryDescription, parameters: Mapping[str, object] | None
) -> dict[str, object]:
    """Validate ``parameters`` against a query's declaration and convert them.

    Returns:
        One value for every declared parameter, defaults included.

    Raises:
        ValidationFailed: If a parameter is unknown, missing or of the wrong type.
    """
    declared = {parameter.name: parameter for parameter in description.parameters}
    given = dict(parameters or {})
    unknown = sorted(set(given) - set(declared))
    if unknown:
        raise ValidationFailed(
            f"query {description.name!r} was given parameters it does not declare",
            expected="only " + (", ".join(repr(name) for name in declared) or "no parameters"),
            actual="also " + ", ".join(repr(name) for name in unknown),
        )
    bound: dict[str, object] = {}
    for parameter in declared.values():
        if given.get(parameter.name) is not None:
            bound[parameter.name] = coerce_parameter(parameter, given[parameter.name])
        elif parameter.required:
            raise ValidationFailed(
                f"query {description.name!r} needs parameter {parameter.name!r}",
                expected=f"{_a(parameter.type.value)} for {parameter.name!r}",
                actual="no value for it",
            )
        else:
            bound[parameter.name] = parameter.default
    return bound
