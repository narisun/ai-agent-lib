"""A reference disclosure has separate content from the always-visible answer."""

import pytest

from agentcraft2_ui.integrations.answer_format import format_answer, present_answer


def test_disclosure_content_is_separate_and_preserves_full_sources() -> None:
    raw = "Revenue is USD 10 [financial_facts[uen=CANA-0010;concept=Revenues]]."
    view = present_answer(raw)
    assert view.body == "Revenue is USD 10."
    assert view.references == "1. **Financial facts** — UEN: ` CANA-0010 `; Concept: ` Revenues `"
    assert format_answer(raw) == view.body + "\n\n### References\n\n" + view.references
    assert present_answer(raw) == view


@pytest.mark.parametrize("answer", ["", "Plain answer.", "[Report](https://example.com/report)"])
def test_uncited_answers_do_not_create_an_empty_disclosure(answer: str) -> None:
    view = present_answer(answer)
    assert view.body == answer
    assert view.references == ""


def test_existing_source_heading_belongs_only_to_the_disclosure() -> None:
    view = present_answer("Revenue grew.\n\n### References\n\n1. financial_facts[uen=CANA-0010]")
    assert view.body == "Revenue grew."
    assert view.references == "1. **Financial facts** — UEN: ` CANA-0010 `"


def test_citation_only_answer_has_no_empty_heading_in_visible_body() -> None:
    view = present_answer("financial_facts[uen=CANA-0010]")
    assert view.body == ""
    assert view.references == "1. **Financial facts** — UEN: ` CANA-0010 `"
