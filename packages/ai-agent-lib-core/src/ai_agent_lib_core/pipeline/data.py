"""Governed data access: every query is decided, narrowed and recorded.

An application never talks to a data source adapter directly. It gets a
:class:`GovernedDataSource`, which asks the policy before each query, hands
the policy's obligations to the adapter and writes the audit record. The
caller cannot supply or relax an obligation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Generic, TypeVar

from ai_agent_lib_core.contracts import (
    AuditSink,
    AuditValue,
    Clock,
    DataSource,
    IdGenerator,
    PolicyAction,
    PolicyDecisionPoint,
    PolicyDenied,
    PolicyRequest,
    PolicyResource,
    QueryDescription,
    QueryResult,
    RequestContext,
    Telemetry,
    ValidationFailed,
)
from ai_agent_lib_core.pipeline.audit import AuditInterceptor
from ai_agent_lib_core.pipeline.calls import DataCall
from ai_agent_lib_core.pipeline.context import bound_request_context
from ai_agent_lib_core.pipeline.deadline import DeadlineInterceptor
from ai_agent_lib_core.pipeline.identity import require_identified
from ai_agent_lib_core.pipeline.interceptor import Handler, compose
from ai_agent_lib_core.pipeline.policy import enforce_decision
from ai_agent_lib_core.pipeline.stages import DataStage, Pipeline

__all__ = ["DataPolicyInterceptor", "GovernedDataSource", "describe_query_result"]

ResponseT = TypeVar("ResponseT")


def describe_query_result(result: QueryResult) -> Mapping[str, AuditValue]:
    """Return what the audit record says about a result. Never a value from it."""
    return {
        "rows": len(result.rows),
        "truncated": result.truncated,
        "masked_columns": ",".join(sorted(result.masked_columns)),
    }


class DataPolicyInterceptor(Generic[ResponseT]):
    """Asks the policy whether this caller may run this query, and on what terms.

    Args:
        policy: The policy decision point.
        clock: The clock port.
        environment: Where the process is running.
    """

    def __init__(self, policy: PolicyDecisionPoint, clock: Clock, environment: str) -> None:
        self._policy = policy
        self._clock = clock
        self._environment = environment

    async def __call__(
        self, request: DataCall, call_next: Handler[DataCall, ResponseT]
    ) -> ResponseT:
        """Continue with the policy's obligations attached, or refuse."""
        context = require_identified(request.context, self._clock)
        if request.classification > context.classification_ceiling:
            raise PolicyDenied(
                f"query {request.query!r} returns {request.classification.name.lower()} data, "
                "which is above what this request may handle",
                reason_code="classification_exceeded",
            )
        question = PolicyRequest(
            principal=context.principal,
            action=PolicyAction.DATA_QUERY,
            resource=PolicyResource(
                kind="query",
                name=f"{request.source}.{request.query}",
                classification=request.classification,
                attributes={"source": request.source, "query": request.query},
            ),
            application=context.application,
            environment=self._environment,
        )
        decision = enforce_decision(await self._policy.decide(question), request.evidence, question)
        if context.principal.actor is not None:
            request.evidence.add(calling_agent=context.principal.actor)
        obligations = decision.obligations
        if obligations.require_approval:
            raise PolicyDenied(
                "the policy requires a person to approve this query, and no approval step "
                "is available",
                reason_code="approval_unavailable",
            )
        request.evidence.add(
            row_filter_columns=",".join(sorted(f.column for f in obligations.row_filters)),
            policy_max_rows=obligations.max_rows,
        )
        return await call_next(replace(request, obligations=obligations))


class GovernedDataSource:
    """A data source whose every query passes policy and leaves an audit record.

    Args:
        name: The name of the data source.
        source: The adapter that runs the queries.
        policy: The policy decision point.
        audit: The audit port.
        telemetry: The telemetry port.
        clock: The clock port.
        ids: The identifier port.
        environment: Where the process is running, as the policy is told.
    """

    def __init__(
        self,
        name: str,
        source: DataSource,
        *,
        policy: PolicyDecisionPoint,
        audit: AuditSink,
        telemetry: Telemetry,
        clock: Clock,
        ids: IdGenerator,
        environment: str,
    ) -> None:
        self._name = name
        self._source = source
        audit_stage: AuditInterceptor[DataCall, QueryResult] = AuditInterceptor(
            audit, telemetry, clock, ids, describe_query_result
        )
        policy_stage: DataPolicyInterceptor[QueryResult] = DataPolicyInterceptor(
            policy, clock, environment
        )
        pipeline = (
            Pipeline[DataStage, DataCall, QueryResult]()
            .with_stage(DataStage.AUDIT, audit_stage)
            .with_stage(DataStage.DEADLINE, DeadlineInterceptor(clock, "data query"))
            .with_stage(DataStage.POLICY, policy_stage)
        )
        self._handler = pipeline.bind(self._run)
        # An unknown query is refused before the policy is asked, and still recorded.
        self._refuse_unknown = compose([audit_stage], self._unknown)

    @property
    def name(self) -> str:
        """The name of the data source."""
        return self._name

    def describe(self) -> Mapping[str, QueryDescription]:
        """Return the queries this source offers, by name."""
        return self._source.describe()

    async def query(
        self,
        name: str,
        parameters: Mapping[str, object] | None = None,
        *,
        context: RequestContext | None = None,
    ) -> QueryResult:
        """Run the query called ``name`` for the caller in the request context.

        Args:
            name: The query to run.
            parameters: Values for the query's declared parameters.
            context: The request context. Defaults to the one bound with
                ``bind_request_context``.

        Raises:
            ValidationFailed: If the query is unknown or a parameter is invalid.
            PolicyDenied: If there is no caller, the policy says no, or an
                obligation cannot be enforced.
            TransientError: If the source timed out or was unavailable.
            IntegrityError: If the audit record could not be written.
        """
        caller = context if context is not None else bound_request_context()
        description = self._source.describe().get(name)
        if description is None:
            # The name is not recorded: it is whatever the caller made up.
            return await self._refuse_unknown(
                DataCall(context=caller, source=self._name, query=_UNKNOWN_QUERY)
            )
        return await self._handler(
            DataCall(
                context=caller,
                source=self._name,
                query=name,
                parameters=parameters or {},
                classification=description.classification,
            )
        )

    async def _run(self, call: DataCall) -> QueryResult:
        return await self._source.query(call.query, call.parameters, obligations=call.obligations)

    @staticmethod
    async def _unknown(call: DataCall) -> QueryResult:  # noqa: ARG004 - refused whatever it is
        raise ValidationFailed("unknown query; a caller may only name a loaded query")


_UNKNOWN_QUERY = "unknown"
