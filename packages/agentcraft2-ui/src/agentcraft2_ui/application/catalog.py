"""Validate a server-owned presentation binding against a registry snapshot."""

from collections.abc import Mapping
from urllib.parse import urlsplit

from pydantic import ValidationError

from agentcraft2_core.contracts import AgentEntry, EntryStatus, is_schema
from agentcraft2_ui.contracts.catalog import AgentBinding, AgentCoreRuntime, PresentationManifest
from agentcraft2_ui.contracts.queries import ContractError, ErrorCode


def same_agent_binding(saved: AgentBinding, current: AgentBinding) -> bool:
    """Registry revisions and optional display features do not change agent authority."""
    return (
        saved.model_copy(
            update={
                "manifest": saved.manifest.model_copy(
                    update={
                        "registry_revision": current.manifest.registry_revision,
                        "capabilities": current.manifest.capabilities,
                    }
                )
            }
        )
        == current
    )


def read_manifest(value: Mapping[str, object]) -> PresentationManifest:
    """Reject unsupported schema versions with a stable, content-free error."""
    if not is_schema(value.get("schema"), "agentcraft2.presentation/v1"):
        raise ContractError(ErrorCode.UNSUPPORTED_SCHEMA)
    try:
        return PresentationManifest.model_validate(value)
    except ValidationError as error:
        raise ContractError(ErrorCode.INVALID_INPUT) from error


def validate_destination(url: str, *, allowed_urls: frozenset[str], local: bool) -> str:
    """Require an exact server-configured URL and HTTPS, or local loopback HTTP.

    Future HTTP adapters must disable redirects and environment proxies and apply
    deployment egress controls. This validates configuration; it does not perform
    DNS resolution or promise protection from DNS rebinding by itself.
    """
    try:
        parts = urlsplit(url)
        valid = (
            url in allowed_urls
            and not any(char.isspace() or not char.isprintable() for char in url)
            and not any(char in url for char in ("\\", "%"))
            and parts.hostname is not None
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and parts.port != 0
            and all(part not in {".", ".."} for part in parts.path.split("/"))
            and "//" not in parts.path
            and (
                parts.scheme == "https"
                or (local and parts.scheme == "http" and parts.hostname in {"127.0.0.1", "::1"})
            )
        )
    except ValueError:
        valid = False
    if not valid:
        raise ContractError(ErrorCode.DESTINATION_DENIED)
    return url.rstrip("/")


def validate_runtime_destination(
    url: str, runtime: AgentCoreRuntime, *, allowed_urls: frozenset[str]
) -> str:
    """Accept only the URL derived from the pinned runtime ARN, and only when allowed.

    The derivation percent-encodes the ARN, which ``validate_destination`` refuses
    in typed URLs; here the URL is never typed, so equality with the derivation
    is the whole check, with the explicit allowlist on top of it.
    """
    if url not in allowed_urls or url != runtime.invocation_url:
        raise ContractError(ErrorCode.DESTINATION_DENIED)
    return url


def bind_agent(
    manifest: PresentationManifest,
    entry: AgentEntry,
    *,
    registry_revision: str,
    allowed_urls: frozenset[str],
    local: bool = False,
    runtime: AgentCoreRuntime | None = None,
) -> AgentBinding:
    """Match immutable registry metadata; authorization remains a separate step.

    With a ``runtime`` the registry entry needs no URL: the agent is reached
    through AgentCore Runtime at the URL derived from the pinned ARN.
    """
    if (
        entry.id != manifest.agent_id
        or entry.version != manifest.agent_version
        or registry_revision != manifest.registry_revision
    ):
        raise ContractError(ErrorCode.STALE_BINDING)
    if entry.status == EntryStatus.DISABLED or (entry.url is None and runtime is None):
        raise ContractError(ErrorCode.NOT_FOUND)
    if manifest.adapter != "question-answer/v1":
        raise ContractError(ErrorCode.UNSUPPORTED_ADAPTER)
    if runtime is not None:
        return AgentBinding(
            manifest=manifest,
            base_url=validate_runtime_destination(
                runtime.invocation_url, runtime, allowed_urls=allowed_urls
            ),
            runtime=runtime,
        )
    return AgentBinding(
        manifest=manifest,
        base_url=validate_destination(
            entry.url or "",
            allowed_urls=allowed_urls,
            local=local,
        ),
    )
