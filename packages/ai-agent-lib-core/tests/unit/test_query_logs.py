"""Query parameters remain useful locally without copying credentials or records."""

import json
from datetime import date

from pydantic import SecretStr

from ai_agent_lib_core.observability.query_logs import query_parameters


def test_nested_credentials_are_masked_and_query_filters_remain_readable() -> None:
    given = {
        "uen": "JONE-0022",
        "as_of": date(2026, 8, 3),
        "offset": 0,
        "auth": {"api_key": "secret", "password": {"nested": "secret"}},
        "filters": [{"email": "private@example.com", "year": 2026}],
        "search": "Bearer abcdefghijklmnop",
        "opaque": SecretStr("secret"),
        "url": "https://user:password@localhost/query",
    }
    safe = query_parameters(given)
    assert safe["uen"] == "JONE-0022"
    assert safe["as_of"] == "2026-08-03"
    assert safe["offset"] == 0
    assert safe["auth"] == {"api_key": "[redacted]", "password": "[redacted]"}
    assert safe["filters"] == [{"email": "[redacted]", "year": 2026}]
    encoded = json.dumps(safe)
    assert all(
        value not in encoded
        for value in ("secret", "private@example.com", "abcdefghijklmnop", "user:password")
    )
    assert given["search"] == "Bearer abcdefghijklmnop"


def test_parameters_are_bounded_even_for_cycles_and_large_collections() -> None:
    cycle: dict[str, object] = {}
    cycle["child"] = cycle
    safe = query_parameters({"cycle": cycle, "long": "x" * 5000, "many": list(range(1000))})
    assert "[truncated]" in json.dumps(safe)
    assert len(str(safe["long"])) <= 256
    assert len(json.dumps(safe)) < 2000
