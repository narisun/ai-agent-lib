"""The rules document and the obligations a decision may carry.

A rules document is a list of grants. The first grant that matches a request
allows it and supplies its obligations; a request that no grant matches is
denied. The same document drives the in-process ``rules`` provider and, as
bundle data, the Rego policy that OPA runs, so the two cannot quietly drift
apart.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, ValidationError

from ai_agent_lib_core.contracts import (
    Classification,
    ConfigurationError,
    Obligations,
    OptionsModel,
    PolicyAction,
    PolicyRequest,
    PrincipalKind,
    RowFilter,
)

__all__ = [
    "RULES_SCHEMA",
    "Rule",
    "evaluate_rules",
    "parse_obligations",
    "parse_rules_document",
]

RULES_SCHEMA = "agentlib.rules/v1"
"""The schema identifier every rules document must declare."""

_OBLIGATION_KEYS = frozenset({"row_filter", "mask_columns", "max_rows", "require_approval"})
_RuleId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,62}$")]
_Name = Annotated[str, StringConstraints(min_length=1, max_length=200)]
_Pattern = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.*:/-]{1,200}$")]
_ClassificationName = Literal["public", "internal", "confidential", "restricted"]


def parse_obligations(raw: object) -> Obligations:
    """Turn the obligations of a decision into the typed form.

    The accepted shape is::

        {"row_filter": {"region": ["east"]}, "mask_columns": ["holder"],
         "max_rows": 50, "require_approval": false}

    Raises:
        ValueError: If a key is not recognised or a value has the wrong shape.
            An obligation the library cannot enforce must never be dropped, so
            the caller turns this error into a deny.
    """
    if raw is None:
        return Obligations()
    if not isinstance(raw, Mapping):
        raise ValueError("obligations must be an object")
    unknown = sorted(str(key) for key in raw if key not in _OBLIGATION_KEYS)
    if unknown:
        raise ValueError(f"unrecognised obligation(s): {unknown}")

    filters = []
    row_filter = raw.get("row_filter", {})
    if not isinstance(row_filter, Mapping):
        raise ValueError("row_filter must map a column to the values it may hold")
    for column, values in row_filter.items():
        if not isinstance(column, str) or not isinstance(values, list | tuple):
            raise ValueError("row_filter must map a column to a list of values")
        try:
            filters.append(RowFilter(column=column, values=tuple(values)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"row_filter: {exc}") from None

    mask_columns = raw.get("mask_columns", [])
    if not isinstance(mask_columns, list | tuple) or not all(
        isinstance(column, str) and column for column in mask_columns
    ):
        raise ValueError("mask_columns must be a list of column names")

    max_rows = raw.get("max_rows")
    if max_rows is not None and (isinstance(max_rows, bool) or not isinstance(max_rows, int)):
        raise ValueError("max_rows must be a whole number")

    require_approval = raw.get("require_approval", False)
    if not isinstance(require_approval, bool):
        raise ValueError("require_approval must be true or false")

    try:
        return Obligations(
            row_filters=tuple(filters),
            mask_columns=frozenset(mask_columns),
            max_rows=max_rows,
            require_approval=require_approval,
        )
    except TypeError as exc:
        raise ValueError(str(exc)) from None


class _RuleModel(OptionsModel):
    id: _RuleId
    actions: list[PolicyAction] = Field(min_length=1)
    roles: list[_Name] | None = None
    agents: list[_Name] | None = None
    kinds: list[PrincipalKind] | None = None
    applications: list[_Name] | None = None
    resources: list[_Pattern] = Field(default_factory=lambda: ["*"], min_length=1)
    max_classification: _ClassificationName | None = None
    obligations: dict[str, object] = Field(default_factory=dict)


class _RulesDocument(OptionsModel):
    schema_: Literal["agentlib.rules/v1"] = Field(alias="schema")
    rules: list[_RuleModel] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Rule:
    """One grant.

    Attributes:
        id: Names the grant. It is the reason code of the decisions it allows.
        actions: The actions the grant covers.
        roles: The caller needs at least one of these roles. ``None`` means any caller.
        agents: The application that presented the request for the caller must
            be one of these agents. ``None`` means any, including none.
        kinds: The caller must be of one of these kinds. ``None`` means any.
        applications: The asking application must be one of these. ``None`` means any.
        resources: Patterns for the resource name, where ``*`` matches any text.
        max_classification: The most sensitive resource the grant covers. With
            a limit, a resource whose classification is unknown is not covered.
        obligations: Conditions attached to the allow.
    """

    id: str
    actions: frozenset[PolicyAction]
    roles: frozenset[str] | None
    agents: frozenset[str] | None
    kinds: frozenset[PrincipalKind] | None
    applications: frozenset[str] | None
    resources: tuple[re.Pattern[str], ...]
    max_classification: Classification | None
    obligations: Obligations

    def matches(self, request: PolicyRequest) -> bool:
        """Return whether the grant covers ``request``."""
        if request.action not in self.actions:
            return False
        if self.roles is not None and not self.roles & request.principal.roles:
            return False
        if self.agents is not None and request.principal.actor not in self.agents:
            return False
        if self.kinds is not None and request.principal.kind not in self.kinds:
            return False
        if self.applications is not None and request.application not in self.applications:
            return False
        if not any(pattern.fullmatch(request.resource.name) for pattern in self.resources):
            return False
        if self.max_classification is not None:
            classification = request.resource.classification
            if classification is None or classification > self.max_classification:
                return False
        return True


def _pattern(text: str) -> re.Pattern[str]:
    """Compile a pattern in which ``*`` matches any text and nothing else is special."""
    return re.compile(".*".join(re.escape(part) for part in text.split("*")))


def parse_rules_document(raw: object, *, what: str = "policy rules") -> tuple[Rule, ...]:
    """Validate a parsed rules document and return its grants, in order.

    Raises:
        ConfigurationError: If the document does not match the schema, has an
            unknown field, repeats a rule ID or holds an obligation the library
            does not recognise.
    """
    try:
        document = _RulesDocument.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigurationError(f"{what}: {problems}") from None
    ids = [rule.id for rule in document.rules]
    repeated = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
    if repeated:
        raise ConfigurationError(f"{what}: rule ID used more than once: {repeated}")
    rules = []
    for rule in document.rules:
        try:
            obligations = parse_obligations(rule.obligations)
        except ValueError as exc:
            raise ConfigurationError(f"{what}: rule {rule.id!r}: {exc}") from None
        rules.append(
            Rule(
                id=rule.id,
                actions=frozenset(rule.actions),
                roles=frozenset(rule.roles) if rule.roles is not None else None,
                agents=frozenset(rule.agents) if rule.agents is not None else None,
                kinds=frozenset(rule.kinds) if rule.kinds is not None else None,
                applications=(
                    frozenset(rule.applications) if rule.applications is not None else None
                ),
                resources=tuple(_pattern(text) for text in rule.resources),
                max_classification=(
                    Classification[rule.max_classification.upper()]
                    if rule.max_classification is not None
                    else None
                ),
                obligations=obligations,
            )
        )
    return tuple(rules)


def evaluate_rules(rules: tuple[Rule, ...], request: PolicyRequest) -> Rule | None:
    """Return the first grant that covers ``request``, or ``None``."""
    return next((rule for rule in rules if rule.matches(request)), None)
