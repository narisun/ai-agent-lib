"""Errors explain themselves: what was expected, what was found, where to look."""

from __future__ import annotations

import json
import logging
from io import StringIO

import pytest

from ai_agent_lib_core.contracts import ConfigurationError, PolicyDenied, ValidationFailed
from ai_agent_lib_core.observability import configure_logging, error_fields, explain, report_error


def _raise_from_my_code() -> None:
    try:
        int("twelve")
    except ValueError as exc:
        error = ConfigurationError(
            "the audit option 'fsync' is not true or false",
            expected="true or false",
            actual="the text 'twelve'",
            fix='write "fsync": true or "fsync": false in the audit options',
            detail="the caller wrote: twelve",
        )
        error.add_note("while building the audit adapter 'jsonl'")
        raise error from exc


def _caught() -> ConfigurationError:
    try:
        _raise_from_my_code()
    except ConfigurationError as error:
        return error
    raise AssertionError("not raised")


def test_explain_gives_the_message_the_facts_the_context_the_cause_and_the_place() -> None:
    text = explain(_caught())
    lines = text.splitlines()
    assert lines[0] == "ConfigurationError: the audit option 'fsync' is not true or false"
    assert "  expected: true or false" in lines
    assert "  got: the text 'twelve'" in lines
    assert any(line.startswith('  fix: write "fsync"') for line in lines)
    assert "  note: while building the audit adapter 'jsonl'" in lines
    assert any(
        line.startswith("  caused by: ValueError: invalid literal for int()") for line in lines
    )
    assert any(
        line.startswith("  raised at: ") and "in _raise_from_my_code" in line for line in lines
    )
    # The detail may hold caller content: shown on a developer's terminal only.
    assert "  detail: the caller wrote: twelve" in lines
    assert "twelve" not in explain(_caught(), details=False).split("got:")[0]
    assert "detail:" not in explain(_caught(), details=False)


def test_explain_hides_other_libraries_messages_without_details() -> None:
    text = explain(_caught(), details=False)
    assert "  caused by: ValueError (at " in text
    assert "invalid literal" not in text


def test_explain_shows_a_denial_with_its_reason() -> None:
    error = PolicyDenied(
        "no rule allows tool.call on 'greet'", reason_code="no_rule_matched", fix="add a rule"
    )
    assert explain(error).splitlines()[:3] == [
        "PolicyDenied: no rule allows tool.call on 'greet'",
        "  reason: no_rule_matched",
        "  fix: add a rule",
    ]


def test_explain_shows_a_plain_error_by_type_and_message() -> None:
    found: dict[str, int] = {}
    with pytest.raises(KeyError) as caught:
        found["missing"]
    assert explain(caught.value).splitlines()[0] == "KeyError: 'missing'"
    assert explain(caught.value, details=False).splitlines()[0] == "KeyError"


def test_error_fields_carry_the_facts_for_a_log_line() -> None:
    fields = error_fields(_caught())
    assert fields["error_type"] == "ConfigurationError"
    assert fields["error"] == "the audit option 'fsync' is not true or false"
    assert fields["error_expected"] == "true or false"
    assert fields["error_actual"] == "the text 'twelve'"
    assert fields["error_fix"].startswith('write "fsync"')
    assert fields["error_notes"] == ["while building the audit adapter 'jsonl'"]
    assert fields["error_causes"][0].startswith("ValueError (at ")
    assert "error_detail" not in fields
    assert fields["error_at"][-1].endswith("in _raise_from_my_code")


def test_error_fields_with_details_add_what_may_hold_content() -> None:
    fields = error_fields(_caught(), details=True)
    assert fields["error_detail"] == "the caller wrote: twelve"
    assert fields["error_causes"][0].startswith("ValueError: invalid literal for int()")


def test_a_log_line_carries_the_reason_of_a_denial() -> None:
    fields = error_fields(PolicyDenied("no", reason_code="no_rule_matched"))
    assert fields["error_reason"] == "no_rule_matched"


def test_the_log_formatter_writes_the_facts() -> None:
    stream = StringIO()
    handler = configure_logging("svc", stream=stream)
    try:
        try:
            raise ValidationFailed(
                "the reply does not match the schema",
                expected="category to be one of 'billing', 'fraud'",
                actual="text of 6 characters",
                detail="category was 'refund'",
            )
        except ValidationFailed:
            logging.getLogger("ai_agent_lib_core.test").exception("the run failed")
    finally:
        logging.getLogger().removeHandler(handler)
    line = json.loads(stream.getvalue().splitlines()[-1])
    assert line["error_expected"] == "category to be one of 'billing', 'fraud'"
    assert line["error_actual"] == "text of 6 characters"
    assert "refund" not in stream.getvalue()


def test_the_log_formatter_writes_the_detail_when_asked() -> None:
    stream = StringIO()
    handler = configure_logging("svc", stream=stream, details=True)
    try:
        try:
            raise ValidationFailed("the reply does not match", detail="category was 'refund'")
        except ValidationFailed:
            logging.getLogger("ai_agent_lib_core.test").exception("the run failed")
    finally:
        logging.getLogger().removeHandler(handler)
    line = json.loads(stream.getvalue().splitlines()[-1])
    assert line["error_detail"] == "category was 'refund'"


def test_report_error_explains_a_library_error_and_returns_the_exit_code() -> None:
    stream = StringIO()
    assert report_error("helper", _caught(), stream=stream) == 1
    text = stream.getvalue()
    assert text.startswith("helper: ConfigurationError: the audit option 'fsync'")
    assert "Traceback" not in text


def test_report_error_shows_the_traceback_of_a_bug_on_a_developers_machine() -> None:
    def buggy() -> None:
        raise ZeroDivisionError("division by zero")

    with pytest.raises(ZeroDivisionError) as caught:
        buggy()
    shown, kept = StringIO(), StringIO()
    report_error("helper", caught.value, stream=shown)
    report_error("helper", caught.value, details=False, stream=kept)
    assert shown.getvalue().startswith("Traceback (most recent call last):")
    assert "helper: ZeroDivisionError: division by zero" in shown.getvalue()
    assert kept.getvalue().startswith("helper: ZeroDivisionError\n")
    assert "division by zero" not in kept.getvalue()
