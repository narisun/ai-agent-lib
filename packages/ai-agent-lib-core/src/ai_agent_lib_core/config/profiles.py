"""Profiles: named sets of default adapters.

A profile only supplies defaults. Any section can still select a different
adapter, which is how local and server-side adapters are mixed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ai_agent_lib_core.contracts import Profile, Section

__all__ = ["PROFILE_DEFAULTS", "ProfileDefaults"]


@dataclass(frozen=True, slots=True)
class ProfileDefaults:
    """The adapters a profile selects when nothing else is configured."""

    model_provider: str
    sections: Mapping[Section, str]

    def __post_init__(self) -> None:
        missing = set(Section) - set(self.sections)
        if missing:
            raise ValueError(f"profile is missing sections: {sorted(s.value for s in missing)}")
        object.__setattr__(self, "sections", MappingProxyType(dict(self.sections)))


PROFILE_DEFAULTS: Mapping[Profile, ProfileDefaults] = MappingProxyType(
    {
        Profile.LOCAL: ProfileDefaults(
            model_provider="anthropic",
            sections={
                Section.SECRETS: "env",
                Section.AUDIT: "jsonl",
                Section.IDENTITY: "static",
                Section.CHECKPOINT: "sqlite",
                Section.REGISTRY: "file",
                Section.POLICY: "rules",
                Section.GUARDRAILS: "patterns",
            },
        ),
        Profile.AWS: ProfileDefaults(
            model_provider="bedrock",
            sections={
                Section.SECRETS: "secrets_manager",
                Section.AUDIT: "firehose",
                Section.IDENTITY: "jwt",
                Section.CHECKPOINT: "postgres",
                Section.REGISTRY: "s3_file",
                Section.POLICY: "opa",
                Section.GUARDRAILS: "bedrock",
            },
        ),
    }
)
