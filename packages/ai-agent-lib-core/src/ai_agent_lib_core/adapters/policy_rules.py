"""A policy decision point that evaluates a rules file in process.

It exists so that a first run needs no policy server. It is for local
development only: deployed services ask OPA, which evaluates the same rules
document through the Rego bundle.
"""

from __future__ import annotations

from pathlib import Path

from ai_agent_lib_core.adapters.policy_cache import PolicyCacheOptions
from ai_agent_lib_core.adapters.policy_documents import (
    Rule,
    evaluate_rules,
    parse_rules_document,
)
from ai_agent_lib_core.adapters.registry_documents import content_revision, parse_document_text
from ai_agent_lib_core.contracts import (
    ConfigurationError,
    Decision,
    IdGenerator,
    PolicyRequest,
)

__all__ = ["RulesPolicyDecisionPoint", "RulesPolicyOptions"]


class RulesPolicyOptions(PolicyCacheOptions):
    """Options of the ``rules`` policy provider.

    Attributes:
        path: The rules file, as YAML or JSON. The default is where the file
            sits when the ``policies`` folder is laid out as an OPA bundle, so
            the same file serves this provider and OPA.
    """

    path: Path = Path("policies/agentlib/rules/data.yaml")


class RulesPolicyDecisionPoint:
    """Allows what the first matching rule allows and denies everything else.

    Args:
        options: Where the rules file is.
        ids: Gives each decision its ID.

    Raises:
        ConfigurationError: If the file is missing or invalid. A missing file
            is an error, not an empty policy: silently denying, or allowing,
            everything would hide the mistake.
    """

    def __init__(self, options: RulesPolicyOptions, ids: IdGenerator) -> None:
        path = options.path
        what = f"policy rules ({path})"
        if not path.exists():
            raise ConfigurationError(
                f"{what}: the file does not exist; create it, set the path in the policy "
                "options or select another policy provider"
            )
        try:
            content = path.read_bytes()
            text = content.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            raise ConfigurationError(f"{what}: the file could not be read as text") from None
        self._rules: tuple[Rule, ...] = parse_rules_document(
            parse_document_text(text, suffix=path.suffix, what=what), what=what
        )
        self._revision = content_revision(content)
        self._ids = ids

    @property
    def revision(self) -> str:
        """Identifies the rules file that was loaded."""
        return self._revision

    async def decide(self, request: PolicyRequest) -> Decision:
        """Return the decision for ``request``."""
        decision_id = self._ids.new_id()
        rule = evaluate_rules(self._rules, request)
        if rule is None:
            return Decision.denied(decision_id, "no_matching_rule", bundle_revision=self._revision)
        return Decision.allowed(
            decision_id,
            reason_code=rule.id,
            obligations=rule.obligations,
            bundle_revision=self._revision,
        )
