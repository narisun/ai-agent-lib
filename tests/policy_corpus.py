"""The shared rules corpus: documents both policy engines must judge alike.

``policies/testdata/rules_corpus.json`` lists changes to a probe rule. Each is
placed beside a valid grant, first and last, in a form that matches the request
and one that does not, so an invalid rule cannot hide by being unused.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CORPUS_FILE = Path(__file__).parents[1] / "policies" / "testdata" / "rules_corpus.json"


def load_corpus() -> dict[str, Any]:
    corpus: dict[str, Any] = json.loads(CORPUS_FILE.read_text(encoding="utf-8"))
    return corpus


def documents(corpus: dict[str, Any], group: str) -> list[tuple[str, dict[str, Any]]]:
    """Every (label, document) the corpus derives for ``"invalid"`` or ``"valid"``."""
    built = []
    for name, change in corpus[group].items():
        for form in ("matching", "not_matching"):
            probe = {**corpus[form], **change}
            for place, rules in (
                ("first", [probe, corpus["grant"]]),
                ("last", [corpus["grant"], probe]),
            ):
                label = f"{name} ({form.replace('_', ' ')}, {place})"
                built.append((label, {"schema": "agentlib.rules/v1", "rules": rules}))
    return built
