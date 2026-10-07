"""A short-lived cache in front of a policy decision point. Off unless configured."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import replace

from pydantic import Field

from ai_agent_lib_core.contracts import (
    Clock,
    Decision,
    OptionsModel,
    PolicyDecisionPoint,
    PolicyRequest,
    SupportsAsyncClose,
    SupportsValidation,
)

__all__ = ["CachingPolicyDecisionPoint", "PolicyCacheOptions"]

_NOT_CACHED = frozenset({"policy_unavailable", "policy_malformed", "obligation_unrecognised"})


class PolicyCacheOptions(OptionsModel):
    """Options every policy provider accepts for the decision cache.

    Attributes:
        cache_ttl_seconds: How long a decision may be reused. ``0`` turns the cache off.
        cache_max_entries: The most decisions kept at once.
    """

    cache_ttl_seconds: float = Field(default=0.0, ge=0, le=300)
    cache_max_entries: int = Field(default=1024, gt=0)


class CachingPolicyDecisionPoint:
    """Reuses a decision for an identical question for a short time.

    The key is a hash of the whole decision input, so any change to the
    caller, the action, the resource or the context asks the policy again.
    Answers that came from a failure are never kept.

    Args:
        inner: The decision point that really decides.
        clock: Measures the age of an entry.
        options: How long and how many decisions are kept.
    """

    def __init__(
        self, inner: PolicyDecisionPoint, clock: Clock, options: PolicyCacheOptions
    ) -> None:
        self._inner = inner
        self._clock = clock
        self._ttl = options.cache_ttl_seconds
        self._max_entries = options.cache_max_entries
        self._entries: OrderedDict[str, tuple[float, Decision]] = OrderedDict()

    @property
    def inner(self) -> PolicyDecisionPoint:
        """The decision point behind the cache."""
        return self._inner

    async def decide(self, request: PolicyRequest) -> Decision:
        """Return a kept decision if it is still fresh, otherwise ask again."""
        canonical = json.dumps(request.to_input(), sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        now = self._clock.monotonic()
        entry = self._entries.get(key)
        if entry is not None and now - entry[0] < self._ttl:
            self._entries.move_to_end(key)
            return replace(entry[1], cached=True)
        decision = await self._inner.decide(request)
        if decision.reason_code not in _NOT_CACHED:
            self._entries[key] = (now, decision)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return decision

    async def validate(self) -> None:
        """Validate the decision point behind the cache."""
        if isinstance(self._inner, SupportsValidation):
            await self._inner.validate()

    async def aclose(self) -> None:
        """Close the decision point behind the cache."""
        if isinstance(self._inner, SupportsAsyncClose):
            await self._inner.aclose()
