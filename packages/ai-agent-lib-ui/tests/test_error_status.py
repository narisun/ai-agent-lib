"""Every contract error names the HTTP status that reports it."""

import pytest

from ai_agent_lib_ui.contracts.queries import ErrorCode
from ai_agent_lib_ui.integrations.server import status_for


def test_every_error_code_has_a_status() -> None:
    for code in ErrorCode:
        assert 400 <= status_for(code) < 600, code


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (ErrorCode.NOT_FOUND, 404),
        (ErrorCode.INVALID_INPUT, 400),
        (ErrorCode.DESTINATION_DENIED, 403),
        (ErrorCode.STALE_BINDING, 409),
        (ErrorCode.CONFLICT, 409),
        (ErrorCode.UNSUPPORTED_SCHEMA, 422),
        (ErrorCode.UNSUPPORTED_ADAPTER, 422),
        (ErrorCode.INVALID_RESPONSE, 502),
        (ErrorCode.UNAVAILABLE, 503),
    ],
)
def test_the_status_says_what_kind_of_failure_it_was(code: ErrorCode, status: int) -> None:
    # A shutdown is not a conflict, and a bad agent reply is not the browser's fault.
    assert status_for(code) == status
