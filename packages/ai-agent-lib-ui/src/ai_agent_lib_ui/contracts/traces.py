"""A private trace link returned by the selected, authenticated agent service."""

import re
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from ai_agent_lib_core.contracts.diagnostics import DiagnosticValue


class TraceLink(DiagnosticValue):
    """Safe, immutable link metadata; the UI never fetches the external URL."""

    provider: Literal["langfuse", "phoenix"]
    trace_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
    url: Annotated[str, Field(min_length=1, max_length=2048)]

    @model_validator(mode="after")
    def check_url(self) -> Self:
        """Allow only private project trace routes, with no credentials or Markdown escapes."""
        parts = urlsplit(self.url)
        _ = parts.port  # Reject malformed or out-of-range ports before rendering a link.
        route = (
            r"/project/[A-Za-z0-9_-]{1,128}/traces/"
            if self.provider == "langfuse"
            else r"/redirects/traces/"
        )
        if (
            not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or re.search(r"[\s\x00-\x1f\x7f<>\\()]", self.url)
            or not re.fullmatch(route + self.trace_id, parts.path)
            or not (
                parts.scheme == "https"
                or (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"})
            )
        ):
            raise ValueError("invalid private trace link")
        return self

    @property
    def label(self) -> str:
        """Use a fixed display label for the validated provider."""
        return {"langfuse": "Langfuse", "phoenix": "Phoenix"}[self.provider]
