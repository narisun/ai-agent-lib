"""Fakes for the ports. They behave like real adapters and need nothing external."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from ai_agent_lib_core.adapters.obligations import effective_row_cap, filter_rows, mask_rows
from ai_agent_lib_core.adapters.parameters import bind_parameters
from ai_agent_lib_core.contracts import (
    AgentEntry,
    AgentSnapshot,
    AuditRecord,
    AuditValue,
    Clock,
    ConfigurationError,
    Decision,
    GuardrailFinding,
    GuardrailPoint,
    GuardrailVerdict,
    IntegrityError,
    Obligations,
    PolicyDenied,
    PolicyRequest,
    Principal,
    QueryDescription,
    QueryParameter,
    QueryResult,
    RequestContext,
    ServerEntry,
    SourceMetadata,
    SpanNotes,
    ToolSnapshot,
    ValidationFailed,
)

__all__ = [
    "FakeDataSource",
    "FakeGuardrails",
    "FakeIdentityVerifier",
    "FakePolicyDecisionPoint",
    "FakeQuery",
    "FakeRegistry",
    "FakeSecretsProvider",
    "FrozenClock",
    "GuardrailAnswer",
    "InMemoryAuditSink",
    "PolicyAnswer",
    "RecordedSpan",
    "RecordingTelemetry",
    "SequentialIds",
]


class FrozenClock:
    """A clock that moves only when a test moves it."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start if start is not None else datetime(2026, 1, 1, tzinfo=UTC)
        if self._now.tzinfo is None:
            raise ValueError("start must be timezone-aware")
        self._monotonic = 0.0

    def now(self) -> datetime:
        """Return the frozen time."""
        return self._now

    def monotonic(self) -> float:
        """Return the frozen monotonic reading."""
        return self._monotonic

    def advance(self, seconds: float) -> None:
        """Move both clocks forward by ``seconds``."""
        if seconds < 0:
            raise ValueError("a clock cannot go backwards")
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


class SequentialIds:
    """Predictable identifiers: ``id-1``, ``id-2`` and so on."""

    def __init__(self, prefix: str = "id") -> None:
        self._prefix = prefix
        self._count = 0

    def new_id(self) -> str:
        """Return the next identifier."""
        self._count += 1
        return f"{self._prefix}-{self._count}"


class InMemoryAuditSink:
    """An audit sink that keeps records in a list.

    Set ``fail_with`` to make the next writes fail, to test fail-closed paths.
    """

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []
        self.fail_with: Exception | None = None

    async def write(self, record: AuditRecord) -> None:
        """Store the record, or fail as instructed."""
        if self.fail_with is not None:
            raise IntegrityError("the audit sink is unavailable") from self.fail_with
        self.records.append(record)


@dataclass
class RecordedSpan:
    """One span a :class:`RecordingTelemetry` was asked to open.

    Attributes:
        name: Its name.
        attributes: Its attributes, from when it opened and as it ended.
        error_type: The type of the error it ended with, if any.
    """

    name: str
    attributes: dict[str, AuditValue]
    error_type: str | None = None


class RecordingTelemetry:
    """Telemetry that remembers every signal."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, AuditValue]]] = []
        self.durations: list[tuple[str, float, dict[str, AuditValue]]] = []
        self.spans: list[RecordedSpan] = []

    @contextmanager
    def span(self, name: str, attributes: Mapping[str, AuditValue]) -> Iterator[SpanNotes]:
        """Remember the span and what it is told as it ends."""
        recorded = RecordedSpan(name, dict(attributes))
        self.spans.append(recorded)

        def note(attributes: Mapping[str, AuditValue], error: BaseException | None = None) -> None:
            recorded.attributes.update(attributes)
            if error is not None:
                recorded.error_type = type(error).__name__

        yield note

    def event(self, name: str, attributes: Mapping[str, AuditValue]) -> None:
        """Remember the event."""
        self.events.append((name, dict(attributes)))

    def duration(self, name: str, seconds: float, attributes: Mapping[str, AuditValue]) -> None:
        """Remember the duration."""
        self.durations.append((name, seconds, dict(attributes)))


class FakeSecretsProvider:
    """A secrets provider backed by a dictionary."""

    def __init__(self, secrets: Mapping[str, str] | None = None) -> None:
        self._secrets = {name: SecretStr(value) for name, value in (secrets or {}).items()}

    async def get_secret(self, name: str) -> SecretStr:
        """Return the secret, or raise ``ConfigurationError`` if it is unknown."""
        try:
            return self._secrets[name]
        except KeyError:
            raise ConfigurationError(f"secret {name!r} is not configured") from None

    def names(self) -> Iterable[str]:
        """Return the names of the secrets held."""
        return tuple(self._secrets)


class FakeIdentityVerifier:
    """An identity verifier with a fixed table of credentials."""

    def __init__(self, principals: Mapping[str, Principal] | None = None) -> None:
        self._principals = dict(principals or {})

    async def verify(self, credential: str | None) -> Principal:
        """Return the principal for a known credential, otherwise deny."""
        if credential is None or credential not in self._principals:
            raise PolicyDenied("the credential is not trusted", reason_code="identity_untrusted")
        return self._principals[credential]


RowFunction = Callable[[Mapping[str, object]], Iterable[Sequence[object]]]
"""Given the bound parameters, returns the rows a fake query produces."""


@dataclass(frozen=True, slots=True)
class FakeQuery:
    """One named query of a :class:`FakeDataSource`.

    Attributes:
        description: The query's declared interface.
        columns: The column names, in order.
        rows: Returns the rows for a set of bound parameters.
    """

    description: QueryDescription
    columns: tuple[str, ...]
    rows: RowFunction

    @classmethod
    def static(
        cls,
        name: str,
        columns: Sequence[str],
        rows: Iterable[Sequence[object]],
        *,
        parameters: Sequence[QueryParameter] = (),
        max_rows: int = 100,
    ) -> FakeQuery:
        """Return a query that gives the same rows whatever its parameters are."""
        fixed = [tuple(row) for row in rows]

        def all_rows(bound: Mapping[str, object]) -> list[tuple[object, ...]]:  # noqa: ARG001 - fixed rows
            return fixed

        return cls(
            description=QueryDescription(
                name=name, description=name, parameters=tuple(parameters), max_rows=max_rows
            ),
            columns=tuple(columns),
            rows=all_rows,
        )


class FakeDataSource:
    """A data source that answers named queries from Python functions.

    It checks parameters and enforces obligations the way a real adapter does,
    so a test of an agent or a tool sees the same limits, filters and masks.

    Attributes:
        calls: Every call made, as ``(query, bound parameters, obligations)``.
    """

    def __init__(
        self, name: str, queries: Iterable[FakeQuery], *, clock: Clock | None = None
    ) -> None:
        self._name = name
        self._queries = {query.description.name: query for query in queries}
        self._clock: Clock = clock if clock is not None else FrozenClock()
        self.calls: list[tuple[str, dict[str, object], Obligations | None]] = []

    def describe(self) -> Mapping[str, QueryDescription]:
        """Return the queries this source offers, by name."""
        return {name: query.description for name, query in sorted(self._queries.items())}

    async def query(
        self,
        name: str,
        parameters: Mapping[str, object] | None = None,
        *,
        obligations: Obligations | None = None,
    ) -> QueryResult:
        """Run the query called ``name``."""
        query = self._queries.get(name)
        if query is None:
            raise ValidationFailed("unknown query; a caller may only name a loaded query")
        bound = bind_parameters(query.description, parameters)
        self.calls.append((name, bound, obligations))
        wanted = obligations if obligations is not None else Obligations()
        rows = filter_rows(query.columns, query.rows(bound), wanted.row_filters)
        cap = effective_row_cap(query.description.max_rows, wanted)
        kept, masked = mask_rows(query.columns, rows[:cap], wanted.mask_columns)
        return QueryResult(
            columns=query.columns,
            rows=kept,
            truncated=len(rows) > cap,
            masked_columns=masked,
            source=SourceMetadata(
                source=self._name,
                retrieved_at=self._clock.now(),
                uri=f"datasource://{self._name}/{name}",
            ),
        )


class FakeRegistry:
    """Registries built from entries given in code.

    Args:
        agents: The registered agents.
        servers: The registered MCP servers.
        revision: The revision both registries report.
    """

    def __init__(
        self,
        agents: Iterable[AgentEntry] = (),
        servers: Iterable[ServerEntry] = (),
        *,
        revision: str = "test",
    ) -> None:
        self._agents = AgentSnapshot(agents, revision=revision)
        self._tools = ToolSnapshot(servers, revision=revision)

    @property
    def agents(self) -> AgentSnapshot:
        """The agent registry."""
        return self._agents

    @property
    def tools(self) -> ToolSnapshot:
        """The MCP tool registry."""
        return self._tools


PolicyAnswer = Decision | Obligations | bool | str
"""What a test may answer a policy question with.

``True`` allows, ``False`` denies, a string denies with that reason code,
obligations allow with those conditions, and a decision is returned as it is.
"""


class FakePolicyDecisionPoint:
    """Allows everything unless told otherwise, and records every question.

    Args:
        answer: Decides each request. Without it every request is allowed.

    Attributes:
        requests: Every question asked, in order.
    """

    def __init__(self, answer: Callable[[PolicyRequest], PolicyAnswer] | None = None) -> None:
        self._answer = answer
        self.requests: list[PolicyRequest] = []

    async def decide(self, request: PolicyRequest) -> Decision:
        """Return the scripted decision for ``request``."""
        self.requests.append(request)
        decision_id = f"decision-{len(self.requests)}"
        answer = self._answer(request) if self._answer is not None else True
        if isinstance(answer, Decision):
            return answer
        if isinstance(answer, Obligations):
            return Decision.allowed(decision_id, obligations=answer, bundle_revision="fake")
        if answer is True:
            return Decision.allowed(decision_id, bundle_revision="fake")
        reason = answer if isinstance(answer, str) else "denied"
        return Decision.denied(decision_id, reason, bundle_revision="fake")


GuardrailAnswer = GuardrailVerdict | str | None
"""What a test may answer a guardrail check with.

``None`` finds nothing, a string blocks with that finding code, and a verdict
is returned as it is.
"""


class FakeGuardrails:
    """Finds nothing unless told otherwise, and records what it was shown.

    Args:
        answer: Decides each check from the point and the text.

    Attributes:
        checked: Every text checked, with the point it was checked at.
    """

    def __init__(
        self, answer: Callable[[GuardrailPoint, str], GuardrailAnswer] | None = None
    ) -> None:
        self._answer = answer
        self.checked: list[tuple[GuardrailPoint, str]] = []

    async def check(
        self,
        point: GuardrailPoint,
        text: str,
        context: RequestContext | None,  # noqa: ARG002 - the script decides from the text
    ) -> GuardrailVerdict:
        """Return the scripted verdict for ``text``."""
        self.checked.append((GuardrailPoint(point), text))
        answer = self._answer(GuardrailPoint(point), text) if self._answer is not None else None
        if isinstance(answer, GuardrailVerdict):
            return answer
        if answer is None:
            return GuardrailVerdict()
        return GuardrailVerdict((GuardrailFinding(answer, blocked=True),))
