"""A private trace link returned by the selected, authenticated agent service."""

import re
from typing import Annotated, Literal, Self
from urllib.parse import SplitResult, urlsplit

from pydantic import Field, model_validator

from agentcraft2_core.contracts.diagnostics import DiagnosticValue

# Phoenix is the library's trace backend; CloudWatch is where an agent on AgentCore
# Runtime is observed. Langfuse links are retired: a stored row that still carries
# one is reloaded without its link by ``tolerant_trace``.
TraceProvider = Literal["phoenix", "cloudwatch"]

# A CloudWatch link identifies an AgentCore session, not a 128-bit trace id.
SESSION_ID = r"[A-Za-z0-9._:-]{33,128}"
_TRACE_IDS: dict[str, str] = {
    "phoenix": r"[0-9a-f]{32}",
    "cloudwatch": SESSION_ID,
}
_LABELS: dict[str, str] = {
    "phoenix": "Agent Lab",
    "cloudwatch": "CloudWatch",
}
_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f<>\()]")
_CONSOLE_HOST = re.compile(r"^(([a-z]{2}(-[a-z]+)+-\d)\.)?console\.aws\.amazon\.com$")
_CONSOLE_REGION = re.compile(r"^region=([a-z]{2}(-[a-z]+)+-\d)$")
_CONSOLE_VIEW = re.compile(r"^gen-ai-observability[A-Za-z0-9._:/?=&%-]*$")


class TraceLink(DiagnosticValue):
    """Safe, immutable link metadata; the UI never fetches the external URL."""

    provider: TraceProvider
    trace_id: Annotated[str, Field(min_length=1, max_length=128)]
    url: Annotated[str, Field(min_length=1, max_length=2048)]

    @model_validator(mode="after")
    def check_url(self) -> Self:
        """Allow only private trace routes, with no credentials or Markdown escapes."""
        parts = urlsplit(self.url)
        _ = parts.port  # Reject malformed or out-of-range ports before rendering a link.
        if (
            not re.fullmatch(_TRACE_IDS[self.provider], self.trace_id)
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or _UNSAFE.search(self.url)
            or not (
                self._console_view(parts)
                if self.provider == "cloudwatch"
                else self._trace_route(parts)
            )
        ):
            raise ValueError("invalid private trace link")
        return self

    def _trace_route(self, parts: SplitResult) -> bool:
        """The private Phoenix redirect route for this trace id, over HTTPS or loopback HTTP."""
        return (
            not parts.query
            and not parts.fragment
            and re.fullmatch(r"/redirects/traces/" + self.trace_id, parts.path) is not None
            and (
                parts.scheme == "https"
                or (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"})
            )
        )

    @staticmethod
    def _console_view(parts: SplitResult) -> bool:
        """The CloudWatch GenAI Observability page of one region, over HTTPS only.

        The console publishes no per-session route, so the fragment opens the
        observability view and the session is found there by its id.
        """
        host = _CONSOLE_HOST.match(parts.hostname or "")
        region = _CONSOLE_REGION.match(parts.query)
        return (
            parts.scheme == "https"
            and host is not None
            and region is not None
            and (host.group(2) is None or host.group(2) == region.group(1))
            and parts.path == "/cloudwatch/home"
            and _CONSOLE_VIEW.match(parts.fragment) is not None
        )

    @property
    def label(self) -> str:
        """The one display name for this provider's trace viewer.

        Phoenix runs as part of the local Agent Lab stack, so its traces open
        "in Agent Lab"; CloudWatch is a separate product and keeps its own name.
        """
        return _LABELS[self.provider]


def tolerant_trace(payload: object) -> TraceLink | None:
    """A trace link that no longer validates degrades to None; the answer never fails for it.

    A trace is optional diagnostics. An agent that still emits a retired provider's
    link during a mixed-version rollout, and a row stored by an older UI, must keep
    its answer and metrics; only the link is dropped.
    """
    try:
        return TraceLink.model_validate(payload)
    except ValueError:  # pydantic wraps validator errors, so ValidationError is the only escape.
        return None


def console_link(region: str, session_id: str) -> TraceLink:
    """The CloudWatch GenAI Observability view of the region an AgentCore session ran in.

    AWS documents the page at ``console.aws.amazon.com/cloudwatch/home#gen-ai-observability``
    without a per-session route; the session is opened from that view by its id,
    which is the link's ``trace_id``.
    """
    return TraceLink(
        provider="cloudwatch",
        trace_id=session_id,
        url=f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
        "#gen-ai-observability",
    )
