"""What an adapter's options are, read from its options model, for people.

The command line and the developer guide both show each option with its type,
its default and what it means. They read all of it from the options model and
its docstring here, so neither can drift from the code.
"""

from __future__ import annotations

import enum
import inspect
import json
import re
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, SecretStr
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

__all__ = ["OptionHelp", "options_help", "options_summary", "type_words"]


@dataclass(frozen=True, slots=True)
class OptionHelp:
    """One option of an adapter.

    Attributes:
        name: Its key in the options object; nested options are dotted.
        type: Its type, in words such as ``"true | false"`` or ``"list of text"``.
        default: Its default as JSON text, or ``None`` when it has none.
        required: Whether it must be given.
        meaning: What it does, from the options model's docstring.
        group: Whether it is an object whose keys are listed after it.
    """

    name: str
    type: str
    default: str | None
    required: bool
    meaning: str
    group: bool = False


def options_summary(model: type[BaseModel]) -> str:
    """Return the first paragraph of the options model's docstring, on one line."""
    return " ".join((inspect.getdoc(model) or "").split("\n\n", 1)[0].split())


def options_help(
    model: type[BaseModel], prefix: str = "", defaults: BaseModel | None = None
) -> list[OptionHelp]:
    """Return every option of ``model``, nested options included, in declaration order.

    A nested option's default is the one in effect: taken from the default of
    the object it belongs to, when that object has one.
    """
    docs = _attribute_docs(model)
    found: list[OptionHelp] = []
    for name, field in model.model_fields.items():
        key = field.alias or name
        nested = _nested_models(field.annotation)
        effective = _effective_default(field, defaults, name)
        found.append(
            OptionHelp(
                name=prefix + key,
                type=type_words(field.annotation),
                default=None
                if field.is_required() or (nested and isinstance(effective, BaseModel))
                else _shown(effective),
                required=field.is_required() and defaults is None,
                meaning=" ".join(docs.get(name, "").split()),
                group=bool(nested),
            )
        )
        for child in nested:
            inner = effective if isinstance(effective, child) else None
            found += options_help(child, prefix=f"{prefix}{key}.", defaults=inner)
    return found


def _effective_default(field: FieldInfo, defaults: BaseModel | None, name: str) -> Any:
    if defaults is not None:
        return getattr(defaults, name)
    if field.is_required():
        return PydanticUndefined
    if field.default is PydanticUndefined and field.default_factory is not None:
        return field.default_factory()  # type: ignore[call-arg]
    return field.default


def _shown(value: Any) -> str | None:
    if value is None or value is PydanticUndefined:
        return None
    return json.dumps(_plain(value))


def _attribute_docs(model: type[BaseModel]) -> dict[str, str]:
    """Read the ``Attributes:`` sections of the model's docstrings, base classes first."""
    docs: dict[str, str] = {}
    for klass in reversed(model.__mro__):
        text = inspect.getdoc(klass) or ""
        if "Attributes:" not in text or klass in (BaseModel, object):
            continue
        current = None
        for line in text.split("Attributes:", 1)[1].splitlines():
            match = re.match(r"^    (\w+)(?: \([^)]*\))?: ?(.*)$", line)
            if match:
                current = match.group(1)
                docs[current] = match.group(2).strip()
            elif current and line.startswith("        "):
                docs[current] += " " + line.strip()
            elif line.strip() and not line.startswith(" "):
                break
    return docs


_PLAIN = {"str": "text", "int": "integer", "float": "number", "bool": "true | false"}


def type_words(annotation: Any) -> str:
    """Describe a type annotation in words a configuration author reads."""
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin is typing.Annotated:
        return type_words(args[0])
    if origin is typing.Literal:
        return " | ".join(json.dumps(a.value if isinstance(a, enum.Enum) else a) for a in args)
    if origin in (typing.Union, types.UnionType):
        return " | ".join(type_words(a) for a in args if a is not type(None))
    if origin in (tuple, list, frozenset, set):
        return f"list of {type_words(args[0])}" if args else "list"
    if origin in (dict, Mapping):
        return f"object of {type_words(args[1])}" if args else "object"
    if isinstance(annotation, type):
        if issubclass(annotation, enum.Enum):
            return " | ".join(json.dumps(member.value) for member in annotation)
        if issubclass(annotation, BaseModel):
            return "object"
        if issubclass(annotation, SecretStr):
            return "secret"
        if issubclass(annotation, Path):
            return "path"
        return _PLAIN.get(annotation.__name__, annotation.__name__)
    return str(annotation).replace("typing.", "")


def _plain(value: object) -> object:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, frozenset | set):
        return sorted(_plain(item) for item in value)  # type: ignore[type-var]
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _nested_models(annotation: Any) -> list[type[BaseModel]]:
    return [
        arg
        for arg in (annotation, *typing.get_args(annotation))
        if isinstance(arg, type) and issubclass(arg, BaseModel)
    ]
