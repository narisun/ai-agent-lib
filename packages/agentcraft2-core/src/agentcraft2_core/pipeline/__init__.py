"""The interceptor contract and the fixed-order request pipelines."""

from agentcraft2_core.pipeline.assembly import (
    Operations,
    build_data_pipeline,
    build_model_pipeline,
    build_tool_pipeline,
)
from agentcraft2_core.pipeline.audit import (
    AuditableCall,
    AuditInterceptor,
    record_refused_sign_in,
)
from agentcraft2_core.pipeline.budget import BudgetInterceptor, BudgetLedger
from agentcraft2_core.pipeline.calls import DataCall, Evidence, ModelCall, ToolCall
from agentcraft2_core.pipeline.context import bind_request_context, bound_request_context
from agentcraft2_core.pipeline.data import (
    DataPolicyInterceptor,
    GovernedDataSource,
    describe_query_result,
)
from agentcraft2_core.pipeline.deadline import DeadlineInterceptor
from agentcraft2_core.pipeline.guardrails import (
    FramingInterceptor,
    InputGuardrailInterceptor,
    OutputGuardrailInterceptor,
    frame_untrusted,
    result_text,
    tool_arguments_text,
)
from agentcraft2_core.pipeline.identity import IdentityInterceptor, require_identified
from agentcraft2_core.pipeline.interceptor import Handler, Interceptor, compose
from agentcraft2_core.pipeline.policy import (
    ModelPolicyInterceptor,
    ToolPolicyInterceptor,
    enforce_decision,
)
from agentcraft2_core.pipeline.registry import (
    AgentCheck,
    RegistryInterceptor,
    allowed_agent,
    name_actors,
)
from agentcraft2_core.pipeline.resilience import ResilienceInterceptor
from agentcraft2_core.pipeline.stages import DataStage, ModelStage, Pipeline, ToolStage
from agentcraft2_core.pipeline.structured import StructuredOutputInterceptor

__all__ = [
    "AgentCheck",
    "AuditInterceptor",
    "AuditableCall",
    "BudgetInterceptor",
    "BudgetLedger",
    "DataCall",
    "DataPolicyInterceptor",
    "DataStage",
    "DeadlineInterceptor",
    "Evidence",
    "FramingInterceptor",
    "GovernedDataSource",
    "Handler",
    "IdentityInterceptor",
    "InputGuardrailInterceptor",
    "Interceptor",
    "ModelCall",
    "ModelPolicyInterceptor",
    "ModelStage",
    "Operations",
    "OutputGuardrailInterceptor",
    "Pipeline",
    "RegistryInterceptor",
    "ResilienceInterceptor",
    "StructuredOutputInterceptor",
    "ToolCall",
    "ToolPolicyInterceptor",
    "ToolStage",
    "allowed_agent",
    "bind_request_context",
    "bound_request_context",
    "build_data_pipeline",
    "build_model_pipeline",
    "build_tool_pipeline",
    "compose",
    "describe_query_result",
    "enforce_decision",
    "frame_untrusted",
    "name_actors",
    "record_refused_sign_in",
    "require_identified",
    "result_text",
    "tool_arguments_text",
]
