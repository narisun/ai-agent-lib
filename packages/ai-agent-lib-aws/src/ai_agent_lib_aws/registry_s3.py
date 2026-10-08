"""Registries read from two objects in an S3 bucket.

The documents are the same YAML or JSON files the ``file`` provider reads on a
developer machine. Keeping them in a versioned bucket gives every change a
version ID, and that ID is the registry's revision in each audit record.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

from botocore import exceptions as aws
from pydantic import Field

from ai_agent_lib_aws.session import AwsSessionFactory
from ai_agent_lib_core.contracts import (
    AgentRegistry,
    AgentSnapshot,
    ConfigurationError,
    OptionsModel,
    ToolRegistry,
    ToolSnapshot,
    describe,
)
from ai_agent_lib_core.kit import RegistryDocument, load_registries

__all__ = ["S3RegistryOptions", "S3RegistrySource"]

_WHAT = "the s3_file registry"
_MAX_BYTES = 5 * 1024 * 1024
_MISSING = frozenset({"NoSuchKey", "NoSuchBucket", "NoSuchVersion", "404", "NotFound"})
_UNVERSIONED = "null"  # what S3 reports for an object in a bucket without versioning


class S3RegistryOptions(OptionsModel):
    """Options of the ``s3_file`` registry provider.

    Attributes:
        bucket: The bucket that holds both documents.
        agents_key: The key of the agent registry document.
        tools_key: The key of the MCP tool registry document.
        expected_bucket_owner: The AWS account that must own the bucket. When
            set, a bucket of the same name in another account is refused.
    """

    bucket: str = Field(pattern=r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
    agents_key: str = Field(default="registry/agents.yaml", min_length=1, max_length=1024)
    tools_key: str = Field(default="registry/mcp-tools.yaml", min_length=1, max_length=1024)
    expected_bucket_owner: str | None = Field(default=None, pattern=r"^[0-9]{12}$")


class S3RegistrySource:
    """Loads both registries once, at startup, and holds them as immutable snapshots.

    Unlike a local file, a missing object stops startup: on a server an empty
    registry must be written down, as a document with no entries. A change to
    a document takes effect when the service is next started.

    Args:
        options: Where the documents are.
        sessions: Builds the client and makes its calls.
        client: An S3 client to use instead of building one.
    """

    def __init__(
        self, options: S3RegistryOptions, sessions: AwsSessionFactory, *, client: Any = None
    ) -> None:
        self._options = options
        self._sessions = sessions
        self._client = client if client is not None else sessions.client("s3")
        self._loaded: tuple[AgentSnapshot, ToolSnapshot] | None = None

    def __repr__(self) -> str:
        return f"S3RegistrySource(bucket={self._options.bucket!r})"

    async def start(self) -> None:
        """Read and check both documents.

        Raises:
            ConfigurationError: If an object is missing, cannot be read or is
                invalid, or an agent names an MCP server the tool registry
                does not hold.
            TransientError: If S3 could not be reached.
        """
        if self._loaded is not None:
            raise RuntimeError("the registry is already started")
        tools = await self._read(self._options.tools_key, "tool registry")
        agents = await self._read(self._options.agents_key, "agent registry")
        self._loaded = load_registries(agents=agents, tools=tools)

    @property
    def agents(self) -> AgentRegistry:
        """The agent registry."""
        return self._started()[0]

    @property
    def tools(self) -> ToolRegistry:
        """The MCP tool registry."""
        return self._started()[1]

    async def validate(self) -> None:
        """Check that the registries were loaded.

        Raises:
            ConfigurationError: If they were not.
        """
        if self._loaded is None:
            raise ConfigurationError(f"{_WHAT} is not started")

    def _started(self) -> tuple[AgentSnapshot, ToolSnapshot]:
        if self._loaded is None:
            raise RuntimeError(f"{_WHAT} is not started")
        return self._loaded

    async def _read(self, key: str, kind: str) -> RegistryDocument:
        what = f"{kind} (s3://{self._options.bucket}/{key})"
        arguments: dict[str, Any] = {"Bucket": self._options.bucket, "Key": key}
        if self._options.expected_bucket_owner is not None:
            arguments["ExpectedBucketOwner"] = self._options.expected_bucket_owner
        try:
            content, version = await self._sessions.invoke(_WHAT, self._get, **arguments)
        except aws.ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "error")
            if code in _MISSING:
                raise ConfigurationError(
                    f"{what}: the object does not exist. An empty registry is a document "
                    "with no entries, not a missing one."
                ) from None
            raise ConfigurationError(f"{what}: the object could not be read ({code})") from None
        except aws.BotoCoreError as exc:
            raise ConfigurationError(
                f"{what}: the object could not be read ({describe(exc)})"
            ) from exc
        if len(content) > _MAX_BYTES:
            raise ConfigurationError(f"{what}: the object is larger than a registry may be")
        return RegistryDocument(
            content=content, suffix=PurePosixPath(key).suffix, what=what, revision=version
        )

    def _get(self, **arguments: Any) -> tuple[bytes, str | None]:
        # Runs in a worker thread: reading the body is part of the network call.
        reply = self._client.get_object(**arguments)
        content = reply["Body"].read(_MAX_BYTES + 1)
        version = reply.get("VersionId")
        if not isinstance(version, str) or not version or version == _UNVERSIONED:
            return bytes(content), None
        return bytes(content), f"s3-version:{version}"
