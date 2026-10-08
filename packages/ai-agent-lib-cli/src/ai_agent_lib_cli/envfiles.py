"""The ``.env`` files of generated services.

Settings are named by logical key and written by the configuration package,
which is the only place that knows a variable name.
"""

from __future__ import annotations

from collections.abc import Mapping

from ai_agent_lib_cli.shared import AGENTS_FILE, RULES_FILE, TOOLS_FILE
from ai_agent_lib_cli.workspace import AgentAnswers, McpAnswers
from ai_agent_lib_core.config import (
    EnvSetting,
    Key,
    options_key,
    provider_key,
    render_env_file,
    secret_key,
    variable_for,
)
from ai_agent_lib_core.contracts import Section

__all__ = ["DEV_ROLE", "agent_env", "mcp_env", "variable_names"]

DEV_ROLE = "analyst"
"""The role the local caller has until the developer changes it."""

# A service folder is two levels below the workspace root: agents/<name>, mcp-servers/<name>.
_UP = "../../"
_FAKE_MODEL_ID = "fake-model"
_HEADER = (
    "Local settings of {name}. agentlib wrote the same text to .env, which is yours to change\n"
    "and is not committed. The tests read this file, not .env.\n"
    "A relative path is relative to this file."
)


_DATA_COMMENTS = {
    "duckdb_csv": "The data the tools read: CSV files in data/, queried through queries/.",
    "rest": "The data the tools read: an HTTP API, called through the endpoints in queries/.",
}


def _shared() -> list[EnvSetting]:
    return [
        EnvSetting(
            options_key(Section.REGISTRY),
            {
                "agents_path": _UP + AGENTS_FILE.as_posix(),
                "tools_path": _UP + TOOLS_FILE.as_posix(),
            },
            comment="The registries and the rules every service of the workspace shares.",
        ),
        EnvSetting(options_key(Section.POLICY), {"path": _UP + RULES_FILE.as_posix()}),
    ]


def _identity(extra: Mapping[str, object]) -> EnvSetting:
    return EnvSetting(
        options_key(Section.IDENTITY),
        {**extra, "subject": "dev-user", "tenant": "dev-tenant", "roles": [DEV_ROLE]},
        comment=(
            "Who a caller without a token is on this machine. Change the roles to see\n"
            "what the rules do. This development identity cannot be used when deployed."
        ),
    )


def agent_env(agent: AgentAnswers) -> str:
    """Return the ``.env.example`` of a generated agent."""
    fake = agent.model_provider == "fake"
    settings = [
        EnvSetting(
            Key.MODEL_PROVIDER,
            agent.model_provider,
            comment=(
                "The model. 'fake' needs no account: it echoes the question, and tests script it."
                if fake
                else "The model. Set the model ID before the first run."
            ),
        ),
        EnvSetting(Key.MODEL_ID, _FAKE_MODEL_ID if fake else "<model-id>", enabled=fake),
    ]
    if agent.model_provider == "anthropic":
        settings.append(
            EnvSetting(
                secret_key("anthropic_api_key"),
                "<api-key>",
                comment="Your API key. Set it here in .env only, never in .env.example.",
                enabled=False,
            )
        )
    if agent.model_provider == "bedrock":
        settings += [
            EnvSetting(
                Key.AWS_PROFILE,
                "<sso-profile>",
                comment="Sign in first: aws sso login --profile <sso-profile>",
                enabled=False,
            ),
            EnvSetting(Key.AWS_REGION, "<region>", enabled=False),
        ]
    settings += [*_shared(), _identity({})]
    return render_env_file(settings, header=_HEADER.format(name=agent.name))


def mcp_env(server: McpAnswers) -> str:
    """Return the ``.env.example`` of a generated MCP server."""
    plan = server.plan
    where = {"data_dir": "data"} if plan.kind == "duckdb_csv" else {}
    settings = [
        EnvSetting(
            Key.DATA_SOURCES,
            {plan.source: {"kind": plan.kind, **where, "queries_dir": "queries", **plan.options}},
            comment=_DATA_COMMENTS[plan.kind],
        ),
        EnvSetting(
            provider_key(Section.CHECKPOINT),
            "none",
            comment="An MCP server compiles no graph, so it needs no store for graph state.",
        ),
        *_shared(),
        _identity({"audience": server.name}),
    ]
    return render_env_file(settings, header=_HEADER.format(name=server.name))


def variable_names() -> dict[str, str]:
    """Return the variable names a generated README mentions, by a short label."""
    return {
        "model_provider": variable_for(Key.MODEL_PROVIDER),
        "model_id": variable_for(Key.MODEL_ID),
        "identity_options": variable_for(options_key(Section.IDENTITY)),
        "deployment_env": variable_for(Key.DEPLOYMENT_ENV),
        "data_sources": variable_for(Key.DATA_SOURCES),
        "secret_prefix": variable_for(secret_key("name")).removesuffix("NAME"),
    }
