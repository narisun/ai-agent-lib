"""Readable source lists without changing values, links, code or stored evidence."""

import pytest

from agentcraft2_ui.integrations.answer_format import format_answer, present_answer

CANADA = (
    "financial_facts[uen=CANA-0010;concept=Revenues;period_end=2025-12-31;"
    "statement=income_statement;is_annual=True]"
)
SLEEP = CANADA.replace("CANA-0010", "SLEE-0041")


@pytest.mark.parametrize("line_break", ["<br>", "<br/>", "<br />", "<BR>", ""])
def test_comparison_keeps_values_and_collects_full_sources_once(line_break: str) -> None:
    answer = (
        "| Metric | Canada Goose | Sleep Country |\n"
        "| --- | --- | --- |\n"
        f"| Revenue | USD 919,811,320.76 {line_break} [{CANADA}] "
        f"| USD 732,412,636.81 {line_break} [{SLEEP}] |\n\n"
        f"Canada Goose has higher revenue [{CANADA}, {SLEEP}]."
    )
    result = format_answer(answer)
    body, sources = result.split("\n\n### References\n\n")
    assert body == (
        "| Metric | Canada Goose | Sleep Country |\n"
        "| --- | --- | --- |\n"
        "| Revenue | USD 919,811,320.76 | USD 732,412,636.81 |\n\n"
        "Canada Goose has higher revenue."
    )
    assert sources.count("**Financial facts**") == 2
    assert sources.index("CANA-0010") < sources.index("SLEE-0041")
    for value in ("Revenues", "2025-12-31", "income_statement", "True"):
        assert sources.count(value) == 2
    assert "UEN:" in sources
    assert "Period end:" in sources


def test_different_periods_and_datasets_are_not_deduplicated_together() -> None:
    result = format_answer(
        f"Revenue [{CANADA}, {CANADA.replace('2025-12-31', '2024-12-31')}]. "
        "Credit credit_metrics[uen=CANA-0010;rating=A]."
    )
    assert result.count("**Financial facts**") == 2
    assert "3. **Credit metrics**" in result
    assert "2024-12-31" in result
    assert "2025-12-31" in result


def test_plain_breaks_separate_values_without_splitting_table_rows() -> None:
    answer = "| Metric | Values |\n| --- | --- |\n| Revenue | FY2024<br/>FY2025 |"
    assert format_answer(answer) == answer.replace("<br/>", " · ")


@pytest.mark.parametrize(
    "answer",
    [
        "Ordinary answer with **emphasis**, USD 25.59, [1] and values[0].",
        f"`{CANADA}<br>`",
        f"``use `{CANADA}`<br>``",
        f"```text\n{CANADA}<br>\n```\n",
        f"~~~text\n{CANADA}<br>\n~~~\n",
        f"```text\n{CANADA}<br>\n",  # An unfinished code sample stays literal.
        f"    {CANADA}<br>\n",
        f"[Source {CANADA}](https://example.com/report)",
        f"Revenue [{CANADA}](https://example.com/report)",
        f"Source https://example.com/{CANADA}",
        f"![Source {CANADA}](https://example.com/chart.png)",
        f"[Source {CANADA}][report]\n\n[report]: https://example.com/report",
        f"<https://example.com/{CANADA}>",
        "<img src=x onerror=alert(1)> and <br onclick=alert(1)>",
        "",
    ],
)
def test_other_markdown_and_code_are_unchanged(answer: str) -> None:
    assert format_answer(answer) == answer


def test_sources_after_a_code_sample_are_still_collected() -> None:
    answer = f"```text\n{CANADA}<br>\n```\nRevenue [{SLEEP}]."
    result = format_answer(answer)
    assert result.startswith(f"```text\n{CANADA}<br>\n```\nRevenue.")
    assert result.split("### References")[1].count("**Financial facts**") == 1


def test_existing_reference_list_keeps_one_heading_without_empty_bullets() -> None:
    result = format_answer(f"Revenue grew.\n\n### References\n\n1. {CANADA}\n2. {SLEEP}\n")
    assert result.startswith("Revenue grew.\n\n### References\n\n1. **Financial facts**")
    assert result.count("### References") == 1
    assert "2. **Financial facts**" in result


def test_source_values_are_quoted_without_interpreting_markup() -> None:
    result = format_answer("Evidence records[id=<script>alert(1)</script>;value=one`two|three].")
    assert "Id: ` <script>alert(1)</script> `" in result
    assert "Value: `` one`two|three ``" in result


@pytest.mark.parametrize("separator", [", ", ",", "; ", " "])
def test_separators_between_citations_do_not_become_stray_punctuation(separator: str) -> None:
    refs = separator.join([f"[{CANADA}]", f"[{SLEEP}]", CANADA])
    view = present_answer(f"Revenue USD 919,811,320.76 {refs}. Other values: 1, 2, 3.")
    assert view.body == "Revenue USD 919,811,320.76. Other values: 1, 2, 3."
    assert view.references.count("**Financial facts**") == 2


def test_evidence_limit_annotations_remain_readable_caveats() -> None:
    annotation = "[evidence_limits: ratio_methodology=not_supplied]"
    view = present_answer(f"Ratio 1.08 {annotation}. Example: `{annotation}`.")
    assert view.body == (f"Ratio 1.08 (Ratio methodology: not supplied). Example: `{annotation}`.")
    assert not view.references
