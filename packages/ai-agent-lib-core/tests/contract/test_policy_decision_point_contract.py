"""Every policy decision point that evaluates rules passes the same contract suite."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from ai_agent_lib_core.adapters import RulesPolicyDecisionPoint, RulesPolicyOptions
from ai_agent_lib_core.contracts import PolicyDecisionPoint
from ai_agent_lib_core.testing import SequentialIds
from ai_agent_lib_core.testing.contracts import PolicyDecisionPointContract


class TestRulesPolicyDecisionPoint(PolicyDecisionPointContract):
    async def make_policy(
        self, tmp_path: Path, document: Mapping[str, object]
    ) -> PolicyDecisionPoint:
        path = tmp_path / "rules.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return RulesPolicyDecisionPoint(RulesPolicyOptions(path=path), SequentialIds())
