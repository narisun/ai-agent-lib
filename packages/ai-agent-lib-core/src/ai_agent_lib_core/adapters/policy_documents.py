"""The rules document and the obligations a decision may carry.

A rules document is a list of grants. The first grant that matches a request
allows it and supplies its obligations; a request that no grant matches is
denied. The same document drives the in-process ``rules`` provider and, as
bundle data, the Rego policy that OPA runs, so the two cannot quietly drift
apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
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
    options_error,
)

__all__ = [
    "RULES_SCHEMA",
    "Rule",
    "evaluate_rules",
    "explain_no_match",
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
        return not self.mismatches(request)

    def mismatches(self, request: PolicyRequest) -> list[str]:
        """Return each way in which the grant does not cover ``request``, for a developer.

        The texts name actions, applications, resources, roles and agents,
        never who the caller is.
        """
        checks = (
            self._action_gap,
            self._application_gap,
            self._resource_gap,
            self._role_gap,
            self._agent_gap,
            self._kind_gap,
            self._classification_gap,
        )
        return [gap for gap in (check(request) for check in checks) if gap is not None]

    def _action_gap(self, request: PolicyRequest) -> str | None:
        if request.action in self.actions:
            return None
        return f"it covers {_listed(a.value for a in self.actions)}, not {request.action.value}"

    def _application_gap(self, request: PolicyRequest) -> str | None:
        if self.applications is None or request.application in self.applications:
            return None
        return f"it covers application {_listed(self.applications)}, not {request.application!r}"

    def _resource_gap(self, request: PolicyRequest) -> str | None:
        if any(pattern.fullmatch(request.resource.name) for pattern in self.resources):
            return None
        shown = _listed(_unpattern(pattern) for pattern in self.resources)
        return f"it covers resources {shown}, not {request.resource.name!r}"

    def _role_gap(self, request: PolicyRequest) -> str | None:
        if self.roles is None or self.roles & request.principal.roles:
            return None
        has = _listed(request.principal.roles) if request.principal.roles else "no roles"
        return f"it needs one of the roles {_listed(self.roles)}; the caller has {has}"

    def _agent_gap(self, request: PolicyRequest) -> str | None:
        actor = request.principal.actor
        if self.agents is None or actor in self.agents:
            return None
        came = f"through {actor!r}" if actor is not None else "directly, not through an agent"
        return f"it needs the request to come through {_listed(self.agents)}; it came {came}"

    def _kind_gap(self, request: PolicyRequest) -> str | None:
        if self.kinds is None or request.principal.kind in self.kinds:
            return None
        kinds = _listed(kind.value for kind in self.kinds)
        return f"it covers {kinds} callers, not {request.principal.kind.value}"

    def _classification_gap(self, request: PolicyRequest) -> str | None:
        limit, found = self.max_classification, request.resource.classification
        if limit is None or (found is not None and found <= limit):
            return None
        level = found.name.lower() if found is not None else "unknown"
        return f"it covers data up to {limit.name.lower()}; the resource is {level}"


def _unpattern(pattern: re.Pattern[str]) -> str:
    """Show a compiled resource pattern as it was written, with ``*``."""
    return re.sub(r"\\(.)", r"\1", pattern.pattern.replace(".*", "*"))


def _listed(names: Iterable[str]) -> str:
    return ", ".join(repr(name) for name in sorted(names))


def explain_no_match(rules: tuple[Rule, ...], request: PolicyRequest, *, closest: int = 2) -> str:
    """Say why no rule allowed ``request``: which rules came closest and what each needs."""
    if not rules:
        return "the rules file has no rules"
    ranked = sorted(
        ((len(rule.mismatches(request)), index, rule) for index, rule in enumerate(rules)),
        key=lambda item: (item[0], item[1]),
    )
    near = [rule for count, _, rule in ranked if count][:closest]
    if all(request.action not in rule.actions for rule in rules):
        return f"no rule covers the action {request.action.value}"
    parts = [f"rule {rule.id!r}: " + "; ".join(rule.mismatches(request)) for rule in near]
    return "no rule matched; the closest were " + " | ".join(parts)


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
        found = options_error(
            what, _RulesDocument, exc, whole="the file", noun="field", show_values=False
        )
        raise ConfigurationError(
            f"{what} do not match the agentlib.rules/v1 schema",
            expected=found.expected,
            actual=found.actual,
            fix="correct the file, then run 'agentlib policy test'",
        ) from None
    ids = [rule.id for rule in document.rules]
    repeated = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
    if repeated:
        raise ConfigurationError(
            f"{what}: two rules share an ID",
            expected="a distinct id per rule: it is the reason code of the decisions it allows",
            actual=f"{', '.join(repeated)} used more than once",
            fix="rename one of them",
        )
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
