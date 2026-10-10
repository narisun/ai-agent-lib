"""The local engine refuses exactly the rules documents OPA refuses (see test_opa_live)."""

from __future__ import annotations

from typing import Any

import pytest
from policy_corpus import documents, load_corpus

from ai_agent_lib_core.adapters.policy_documents import parse_rules_document
from ai_agent_lib_core.contracts import ConfigurationError

CORPUS = load_corpus()


@pytest.mark.parametrize(("label", "document"), documents(CORPUS, "invalid"))
def test_an_invalid_rule_anywhere_refuses_the_whole_document(
    label: str, document: dict[str, Any]
) -> None:
    with pytest.raises(ConfigurationError):
        parse_rules_document(document)


@pytest.mark.parametrize(("label", "document"), documents(CORPUS, "valid"))
def test_a_valid_rule_is_accepted(label: str, document: dict[str, Any]) -> None:
    assert len(parse_rules_document(document)) == 2
