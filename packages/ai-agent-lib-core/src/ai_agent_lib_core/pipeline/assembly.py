"""Assembly of the standard pipelines.

This module is the one place that decides which stages are active. A stage is
added here when its interceptor exists; its position always comes from the
stage enum.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeVar

from ai_agent_lib_core.contracts import (
    AuditSink,
    AuditValue,
    Clock,
    GuardrailCheck,
    GuardrailPoint,
    IdGenerator,
    PolicyDecisionPoint,
    RegistrySource,
    Telemetry,
)
from ai_agent_lib_core.pipeline.audit import AuditInterceptor
from ai_agent_lib_core.pipeline.calls import ModelCall, ToolCall
from ai_agent_lib_core.pipeline.guardrails import (
    FramingInterceptor,
    InputGuardrailInterceptor,
    OutputGuardrailInterceptor,
    result_text,
    tool_arguments_text,
)
from ai_agent_lib_core.pipeline.identity import IdentityInterceptor
from ai_agent_lib_core.pipeline.policy import ModelPolicyInterceptor, ToolPolicyInterceptor
from ai_agent_lib_core.pipeline.registry import AgentCheck, RegistryInterceptor
from ai_agent_lib_core.pipeline.stages import ModelStage, Pipeline, ToolStage
from ai_agent_lib_core.pipeline.structured import StructuredOutputInterceptor

__all__ = ["build_model_pipeline", "build_tool_pipeline"]

ResponseT = TypeVar("ResponseT")


def build_model_pipeline(
    *,
    audit: AuditSink,
    telemetry: Telemetry,
    clock: Clock,
    ids: IdGenerator,
    describe_response: Callable[[ResponseT], Mapping[str, AuditValue]] | None = None,
    policy: PolicyDecisionPoint | None = None,
    environment: str = "local",
    guardrails: GuardrailCheck | None = None,
    response_text: Callable[[ResponseT], str] = result_text,
    structured_payload: Callable[[ResponseT, str], object] | None = None,
) -> Pipeline[ModelStage, ModelCall, ResponseT]:
    """Return the model pipeline with every stage that is implemented so far.

    Args:
        audit: The audit port.
        telemetry: The telemetry port.
        clock: The clock port.
        ids: The identifier port.
        describe_response: Returns metadata about a successful response.
        policy: The policy decision point. Without one the policy stage is left out.
        environment: Where the process is running, as the policy is told.
        guardrails: The guardrail port. Without it both guardrail stages are left out.
        response_text: Returns the text of a response, for the output guardrails.
        structured_payload: Returns what a response holds for a named schema.
            Without it the structured output stage is left out.
    """
    audit_stage: AuditInterceptor[ModelCall, ResponseT] = AuditInterceptor(
        audit, telemetry, clock, ids, describe_response
    )
    identity_stage: IdentityInterceptor[ModelCall, ResponseT] = IdentityInterceptor(clock)
    pipeline = (
        Pipeline[ModelStage, ModelCall, ResponseT]()
        .with_stage(ModelStage.AUDIT, audit_stage)
        .with_stage(ModelStage.IDENTITY, identity_stage)
    )
    if policy is not None:
        policy_stage: ModelPolicyInterceptor[ResponseT] = ModelPolicyInterceptor(
            policy, clock, environment
        )
        pipeline = pipeline.with_stage(ModelStage.POLICY, policy_stage)
    if guardrails is not None:
        input_stage: InputGuardrailInterceptor[ModelCall, ResponseT] = InputGuardrailInterceptor(
            guardrails, GuardrailPoint.MODEL_INPUT, _model_text
        )
        output_stage: OutputGuardrailInterceptor[ModelCall, ResponseT] = OutputGuardrailInterceptor(
            guardrails, GuardrailPoint.MODEL_OUTPUT, response_text
        )
        pipeline = pipeline.with_stage(ModelStage.INPUT_GUARDRAILS, input_stage).with_stage(
            ModelStage.OUTPUT_GUARDRAILS, output_stage
        )
    if structured_payload is not None:
        structured_stage: StructuredOutputInterceptor[ResponseT] = StructuredOutputInterceptor(
            structured_payload
        )
        pipeline = pipeline.with_stage(ModelStage.STRUCTURED_OUTPUT, structured_stage)
    return pipeline


def _model_text(call: ModelCall) -> str:
    return call.text


def build_tool_pipeline(
    *,
    audit: AuditSink,
    telemetry: Telemetry,
    clock: Clock,
    ids: IdGenerator,
    registry: RegistrySource | None = None,
    registry_agent_check: AgentCheck = AgentCheck.SELF,
    policy: PolicyDecisionPoint | None = None,
    environment: str = "local",
    guardrails: GuardrailCheck | None = None,
    frame_results: bool = False,
    response_text: Callable[[ResponseT], str] = result_text,
) -> Pipeline[ToolStage, ToolCall, ResponseT]:
    """Return the tool pipeline with every stage that is implemented so far.

    The tool pipeline has no separate identity stage. The policy stage checks
    that the caller is identified before it asks the policy; without a policy
    decision point, the identity check alone takes that place.

    Args:
        audit: The audit port.
        telemetry: The telemetry port.
        clock: The clock port.
        ids: The identifier port.
        registry: The registries. Without them the registry stage is left out.
        registry_agent_check: Which agent the registry stage holds to the
            agent registry: this application, or the agent behind the caller.
        policy: The policy decision point.
        environment: Where the process is running, as the policy is told.
        guardrails: The guardrail port. Without it both guardrail stages are left out.
        frame_results: Whether each result is framed as untrusted data. An
            agent turns this on. An MCP server leaves it off, because the
            agent that receives the result frames it.
        response_text: Returns the text of a result, for the result guardrails.
    """
    audit_stage: AuditInterceptor[ToolCall, ResponseT] = AuditInterceptor(
        audit, telemetry, clock, ids
    )
    pipeline = Pipeline[ToolStage, ToolCall, ResponseT]().with_stage(ToolStage.AUDIT, audit_stage)
    if policy is not None:
        policy_stage: ToolPolicyInterceptor[ResponseT] = ToolPolicyInterceptor(
            policy, clock, environment
        )
        pipeline = pipeline.with_stage(ToolStage.POLICY, policy_stage)
    else:
        identity_stage: IdentityInterceptor[ToolCall, ResponseT] = IdentityInterceptor(clock)
        pipeline = pipeline.with_stage(ToolStage.POLICY, identity_stage)
    if registry is not None:
        registry_stage: RegistryInterceptor[ResponseT] = RegistryInterceptor(
            registry, agent_check=registry_agent_check
        )
        pipeline = pipeline.with_stage(ToolStage.REGISTRY, registry_stage)
    if guardrails is not None:
        arguments_stage: InputGuardrailInterceptor[ToolCall, ResponseT] = InputGuardrailInterceptor(
            guardrails, GuardrailPoint.TOOL_INPUT, tool_arguments_text
        )
        result_stage: OutputGuardrailInterceptor[ToolCall, ResponseT] = OutputGuardrailInterceptor(
            guardrails, GuardrailPoint.TOOL_RESULT, response_text
        )
        pipeline = pipeline.with_stage(ToolStage.INPUT_GUARDRAILS, arguments_stage).with_stage(
            ToolStage.RESULT_GUARDRAILS, result_stage
        )
    if frame_results:
        framing_stage: FramingInterceptor[ResponseT] = FramingInterceptor()
        pipeline = pipeline.with_stage(ToolStage.FRAMING, framing_stage)
    return pipeline
