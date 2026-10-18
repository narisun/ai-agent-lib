"""A private trace link returned by the selected, authenticated agent service."""

import re
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from ai_agent_lib_core.contracts.diagnostics import DiagnosticValue


class TraceLink(DiagnosticValue):
    """Safe, immutable link metadata; the UI never fetches the external URL."""

    provider: Literal["phoenix"]
    trace_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    url: Annotated[str, Field(min_length=1, max_length=2048)]

    @model_validator(mode="after")
    def check_url(self) -> Self:
        """Allow only private Phoenix trace routes, with no credentials or Markdown escapes."""
        parts = urlsplit(self.url)
        _ = parts.port  # Reject malformed or out-of-range ports before rendering a link.
        if (
            not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or re.search(r"[\s\x00-\x1f\x7f<>\\()]", self.url)
            or not re.fullmatch(r"/redirects/traces/" + self.trace_id, parts.path)
            or not (
                parts.scheme == "https"
                or (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"})
            )
        ):
            raise ValueError("invalid private trace link")
        return self


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
