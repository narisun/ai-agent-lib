"""Reading registry documents: the frozen ``agentlib.registry/v1`` format.

Every provider that gets its registries from YAML or JSON uses these
functions, so a document means the same thing wherever it is stored.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypeVar

import httpx
from pydantic import AfterValidator, Field, StringConstraints, ValidationError

from ai_agent_lib_core.adapters.strict_yaml import StrictYamlError, load_strict_yaml
from ai_agent_lib_core.contracts import (
    REGISTRY_SCHEMA,
    AgentEntry,
    AgentSnapshot,
    Classification,
    ConfigurationError,
    EntryStatus,
    OptionsModel,
    ServerEntry,
    ToolEntry,
    ToolSnapshot,
)

__all__ = [
    "ABSENT_REVISION",
    "RegistryDocument",
    "agents_document",
    "content_revision",
    "load_registries",
    "parse_agents_document",
    "parse_document_text",
    "parse_tools_document",
    "tools_document",
]

_Id = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,62}$")]
_ToolName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
_Text = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
_Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_ClientId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")]
_ClassificationName = Literal["public", "internal", "confidential", "restricted"]
_CREDENTIAL_IN_URL = re.compile(r"(?i)(token|secret|password|api[_-]?key)=")


def _plain_url(value: str) -> str:
    """Accept an http(s) URL that carries no credentials."""
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise ValueError("must be a URL") from None
    if url.scheme not in {"http", "https"} or not url.host:
        raise ValueError("must be an http or https URL")
    if url.userinfo or _CREDENTIAL_IN_URL.search(value):
        raise ValueError("must not hold credentials")
    return value


_Url = Annotated[str, AfterValidator(_plain_url)]


class _ToolModel(OptionsModel):
    name: _ToolName
    version: _Text
    schema_sha256: _Sha256 | None = None
    classification: _ClassificationName = "internal"
    read_only: bool = False
    description: str = Field(default="", max_length=500)


class _ServerModel(OptionsModel):
    id: _Id
    owner: _Text
    url: _Url
    audience: _Text | None = None
    tools: list[_ToolModel] = Field(default_factory=list)


class _ToolsDocument(OptionsModel):
    schema_: Literal["agentlib.registry/v1"] = Field(alias="schema")
    servers: list[_ServerModel] = Field(default_factory=list)


class _AgentModel(OptionsModel):
    id: _Id
    owner: _Text
    version: _Text
    description: str = Field(default="", max_length=500)
    url: _Url | None = None
    mcp_servers: list[_Id] = Field(default_factory=list)
    model_aliases: list[_Text] = Field(default_factory=list)
    classification_ceiling: _ClassificationName = "internal"
    status: EntryStatus = EntryStatus.ACTIVE
    client_id: _ClientId | None = None


class _AgentsDocument(OptionsModel):
    schema_: Literal["agentlib.registry/v1"] = Field(alias="schema")
    agents: list[_AgentModel] = Field(default_factory=list)


ModelT = TypeVar("ModelT", bound=OptionsModel)

if REGISTRY_SCHEMA != "agentlib.registry/v1":  # pragma: no cover - guards the literals above
    raise AssertionError("the document models and REGISTRY_SCHEMA disagree")


def _validate(model: type[ModelT], raw: object, what: str) -> ModelT:
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigurationError(f"{what}: {problems}") from None


def _unique(what: str, kind: str, ids: list[str]) -> None:
    repeated = sorted({item for item in ids if ids.count(item) > 1})
    if repeated:
        raise ConfigurationError(f"{what}: {kind} registered more than once: {repeated}")


def parse_document_text(text: str, *, suffix: str, what: str) -> object:
    """Parse registry text as JSON or YAML, chosen by the file extension.

    Raises:
        ConfigurationError: If the extension is not known or the text is invalid.
    """
    kind = suffix.lower()
    if kind == ".json":
        try:
            return json.loads(text)
        except ValueError:
            raise ConfigurationError(f"{what}: the file is not valid JSON") from None
    if kind in {".yaml", ".yml"}:
        try:
            return load_strict_yaml(text)
        except StrictYamlError as exc:
            raise ConfigurationError(f"{what}: {exc}") from None
    raise ConfigurationError(f"{what}: the file must end in .yaml, .yml or .json")


def content_revision(content: bytes) -> str:
    """Return a revision that identifies ``content`` exactly."""
    return "sha256:" + hashlib.sha256(content).hexdigest()[:16]


def parse_tools_document(raw: object, *, what: str = "tool registry") -> tuple[ServerEntry, ...]:
    """Validate a parsed MCP tool registry document and return its servers.

    Raises:
        ConfigurationError: If the document does not match the schema, has an
            unknown field or registers something twice.
    """
    document = _validate(_ToolsDocument, raw, what)
    _unique(what, "server", [server.id for server in document.servers])
    servers = []
    for server in document.servers:
        _unique(what, f"tool of server {server.id!r}", [tool.name for tool in server.tools])
        servers.append(
            ServerEntry(
                id=server.id,
                owner=server.owner,
                url=server.url,
                audience=server.audience,
                tools=tuple(_tool(tool) for tool in server.tools),
            )
        )
    return tuple(servers)


def _tool(tool: _ToolModel) -> ToolEntry:
    return ToolEntry(
        name=tool.name,
        version=tool.version,
        classification=Classification[tool.classification.upper()],
        read_only=tool.read_only,
        schema_sha256=tool.schema_sha256,
        description=tool.description,
    )


def parse_agents_document(raw: object, *, what: str = "agent registry") -> tuple[AgentEntry, ...]:
    """Validate a parsed agent registry document and return its agents.

    Raises:
        ConfigurationError: If the document does not match the schema, has an
            unknown field or registers something twice.
    """
    document = _validate(_AgentsDocument, raw, what)
    _unique(what, "agent", [agent.id for agent in document.agents])
    _unique(
        what,
        "agent client ID",
        [agent.client_id for agent in document.agents if agent.client_id is not None],
    )
    return tuple(_agent(agent) for agent in document.agents)


def _agent(agent: _AgentModel) -> AgentEntry:
    values: dict[str, Any] = agent.model_dump()
    values["classification_ceiling"] = Classification[agent.classification_ceiling.upper()]
    values["mcp_servers"] = tuple(agent.mcp_servers)
    values["model_aliases"] = tuple(agent.model_aliases)
    return AgentEntry(**values)


def tools_document(servers: Iterable[ServerEntry]) -> dict[str, object]:
    """Return the MCP tool registry document that holds ``servers``."""
    return {
        "schema": REGISTRY_SCHEMA,
        "servers": [
            {
                "id": server.id,
                "owner": server.owner,
                "url": server.url,
                **({"audience": server.audience} if server.audience is not None else {}),
                "tools": [
                    {
                        "name": tool.name,
                        "version": tool.version,
                        **(
                            {"schema_sha256": tool.schema_sha256}
                            if tool.schema_sha256 is not None
                            else {}
                        ),
                        "classification": tool.classification.name.lower(),
                        "read_only": tool.read_only,
                        **({"description": tool.description} if tool.description else {}),
                    }
                    for tool in server.tools
                ],
            }
            for server in servers
        ],
    }


def agents_document(agents: Iterable[AgentEntry]) -> dict[str, object]:
    """Return the agent registry document that holds ``agents``."""
    return {
        "schema": REGISTRY_SCHEMA,
        "agents": [
            {
                "id": agent.id,
                "owner": agent.owner,
                "version": agent.version,
                **({"url": agent.url} if agent.url is not None else {}),
                **({"client_id": agent.client_id} if agent.client_id is not None else {}),
                "description": agent.description,
                "mcp_servers": list(agent.mcp_servers),
                "model_aliases": list(agent.model_aliases),
                "classification_ceiling": agent.classification_ceiling.name.lower(),
                "status": agent.status.value,
            }
            for agent in agents
        ],
    }


ABSENT_REVISION = "absent"
"""The revision of a registry that has no document."""


@dataclass(frozen=True, slots=True)
class RegistryDocument:
    """One registry document, as it was read from where it is stored.

    Attributes:
        content: The bytes of the document.
        suffix: The extension of its name, which says whether it is YAML or JSON.
        what: Names the document in an error message.
        revision: What identifies this version where it is stored. By default
            a hash of the content.
    """

    content: bytes
    suffix: str
    what: str
    revision: str | None = None


def _parsed(document: RegistryDocument) -> tuple[object, str]:
    try:
        text = document.content.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigurationError(f"{document.what}: the file could not be read as text") from None
    parsed = parse_document_text(text, suffix=document.suffix, what=document.what)
    return parsed, document.revision or content_revision(document.content)


def load_registries(
    *, agents: RegistryDocument | None, tools: RegistryDocument | None
) -> tuple[AgentSnapshot, ToolSnapshot]:
    """Turn the two registry documents into immutable snapshots.

    A document that is ``None`` gives an empty registry. Every provider that
    stores its registries as documents builds them here, so the same document
    passes or fails the same checks wherever it is kept.

    Raises:
        ConfigurationError: If a document is invalid, or an agent names an MCP
            server that the tool registry does not hold.
    """
    tool_snapshot = ToolSnapshot(revision=ABSENT_REVISION)
    if tools is not None:
        raw, revision = _parsed(tools)
        tool_snapshot = ToolSnapshot(parse_tools_document(raw, what=tools.what), revision=revision)
    if agents is None:
        return AgentSnapshot(revision=ABSENT_REVISION), tool_snapshot
    raw, revision = _parsed(agents)
    try:
        agent_snapshot = AgentSnapshot(
            parse_agents_document(raw, what=agents.what), revision=revision
        )
    except ValueError as exc:
        # For example a client ID that is also another agent's registry ID.
        raise ConfigurationError(f"{agents.what}: {exc}") from None
    known = {server.id for server in tool_snapshot.entries()}
    for agent in agent_snapshot.entries():
        unknown = sorted(set(agent.mcp_servers) - known)
        if unknown:
            raise ConfigurationError(
                f"{agents.what}: agent {agent.id!r} names MCP server(s) that are not in the "
                f"tool registry: {unknown}"
            )
    return agent_snapshot, tool_snapshot
