"""What a provider pack builds on.

A provider pack is another distribution, such as ``ai-agent-lib-aws``, that
adds adapters for the ports. A pack imports two things from core and nothing
else: the ``contracts`` package, for the ports and value types it implements,
and this module, for the registry it registers with and the machinery every
adapter of a kind shares. An import rule holds each first-party pack to that.

Keeping the shared machinery here is what makes two adapters agree. A SQL
data source for a warehouse loads the same query files, binds parameters and
pushes obligations into the statement exactly as the local one does, because
both use the same catalog and compiler.
"""

from ai_agent_lib_core.adapters.guardrails_patterns import GuardrailsOptions
from ai_agent_lib_core.adapters.http_support import checked_base_url, tls_verification
from ai_agent_lib_core.adapters.obligations import (
    check_filter_columns,
    effective_row_cap,
    filter_rows,
    mask_rows,
)
from ai_agent_lib_core.adapters.parameters import bind_parameters, coerce_parameter
from ai_agent_lib_core.adapters.registry_documents import (
    RegistryDocument,
    agents_document,
    content_revision,
    load_registries,
    parse_agents_document,
    parse_document_text,
    parse_tools_document,
    tools_document,
)
from ai_agent_lib_core.adapters.sql import (
    CompiledQuery,
    GovernedStatement,
    NamedQuery,
    QueryCatalog,
    build_statement,
    compile_query,
    finish_result,
)
from ai_agent_lib_core.di.providers import (
    DATA_PORT,
    MODEL_PORT,
    BuildContext,
    Factory,
    ProviderSpec,
    ServiceProviders,
)

__all__ = [
    "DATA_PORT",
    "MODEL_PORT",
    "BuildContext",
    "CompiledQuery",
    "Factory",
    "GovernedStatement",
    "GuardrailsOptions",
    "NamedQuery",
    "ProviderSpec",
    "QueryCatalog",
    "RegistryDocument",
    "ServiceProviders",
    "agents_document",
    "bind_parameters",
    "build_statement",
    "check_filter_columns",
    "checked_base_url",
    "coerce_parameter",
    "compile_query",
    "content_revision",
    "effective_row_cap",
    "filter_rows",
    "finish_result",
    "load_registries",
    "mask_rows",
    "parse_agents_document",
    "parse_document_text",
    "parse_tools_document",
    "tls_verification",
    "tools_document",
]
