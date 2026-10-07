"""The interceptor contract and the fixed-order request pipelines."""

from ai_agent_lib_core.pipeline.assembly import build_model_pipeline, build_tool_pipeline
from ai_agent_lib_core.pipeline.audit import (
    AuditableCall,
    AuditInterceptor,
    record_refused_sign_in,
)
from ai_agent_lib_core.pipeline.calls import DataCall, Evidence, ModelCall, ToolCall
from ai_agent_lib_core.pipeline.context import bind_request_context, bound_request_context
from ai_agent_lib_core.pipeline.data import (
    DataPolicyInterceptor,
    GovernedDataSource,
    describe_query_result,
)
from ai_agent_lib_core.pipeline.guardrails import (
    FramingInterceptor,
    InputGuardrailInterceptor,
    OutputGuardrailInterceptor,
    frame_untrusted,
    result_text,
    tool_arguments_text,
)
from ai_agent_lib_core.pipeline.identity import IdentityInterceptor, require_identified
from ai_agent_lib_core.pipeline.interceptor import Handler, Interceptor, compose
from ai_agent_lib_core.pipeline.policy import (
    ModelPolicyInterceptor,
    ToolPolicyInterceptor,
    enforce_decision,
)
from ai_agent_lib_core.pipeline.registry import (
    AgentCheck,
    RegistryInterceptor,
    allowed_agent,
    name_actors,
)
from ai_agent_lib_core.pipeline.stages import DataStage, ModelStage, Pipeline, ToolStage
from ai_agent_lib_core.pipeline.structured import StructuredOutputInterceptor

__all__ = [
    "AgentCheck",
    "AuditInterceptor",
    "AuditableCall",
    "DataCall",
    "DataPolicyInterceptor",
    "DataStage",
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
    "OutputGuardrailInterceptor",
    "Pipeline",
    "RegistryInterceptor",
    "StructuredOutputInterceptor",
    "ToolCall",
    "ToolPolicyInterceptor",
    "ToolStage",
    "allowed_agent",
    "bind_request_context",
    "bound_request_context",
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
