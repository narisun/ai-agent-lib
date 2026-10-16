"""Recursive display masking preserves useful configuration and JSON structure."""

import json
from urllib.parse import parse_qs, urlsplit

import pytest
from tools.local_config_redaction import MASKED, display_json


@pytest.mark.parametrize(
    "name",
    [
        "password",
        "PASSWD",
        "pwd",
        "api_key",
        "apiKey",
        "API-KEY",
        "clientSecret",
        "access_key_id",
        "secretAccessKey",
        "private_key",
        "publicKey",
        "signing-key",
        "authorization",
        "authentication",
        "auth",
        "bearerToken",
        "refresh_token",
        "sessionToken",
        "jwt",
        "token",
        "tokens",
        "tokenHash",
        "credentials",
        "Cookie",
        "set-cookie",
        "dsn",
        "connectionString",
        "subject",
        "user_id",
        "username",
        "email",
        "phoneNumber",
    ],
)
def test_sensitive_names_are_masked_at_every_depth(name: str) -> None:
    source = {"providers": [{"options": {name: "private-test-value", "enabled": True}}]}
    shown = json.loads(display_json(json.dumps(source)))
    assert shown == {"providers": [{"options": {name: MASKED, "enabled": True}}]}
    assert source["providers"][0]["options"][name] == "private-test-value"


def test_operational_options_retain_types_including_token_budgets() -> None:
    source = {
        "path": "../../registry/agents.yaml",
        "prompt_caching": True,
        "cache_ttl": "5m",
        "max_tokens": 4096,
        "maxOutputTokens": 8192,
        "token_ttl_seconds": 300,
        "tokenizer": "cl100k_base",
        "budget": {"tokens": 64000, "model_calls": 20},
        "timeout_seconds": 120.5,
        "roles": ["analyst", "manager"],
        "optional": None,
        "empty": "",
        "retries": 0,
    }
    assert json.loads(display_json(json.dumps(source))) == source


def test_sensitive_fields_mask_entire_values_including_lists_and_objects() -> None:
    source = {
        "api_key": None,
        "password": 1234,
        "credentials": {"custom-field": "arbitrary-secret", "other": ["nested-secret"]},
        "tokens": ["first-private-token", "second-private-token"],
        "timeout_seconds": 30,
    }
    shown = json.loads(display_json(json.dumps(source)))
    assert shown == {
        **dict.fromkeys(("api_key", "password", "credentials", "tokens"), MASKED),
        "timeout_seconds": 30,
    }


def test_http_headers_keep_public_content_settings_and_mask_custom_values() -> None:
    source = {
        "defaultHeaders": {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "agent-lab",
            "Authorization": "Bearer private-token",
            "X-API-Key": "private-key",
            "X-Enterprise-Access": "opaque-secret",
            "Cookie": "session=private-cookie",
            "X-Nested": {"value": "private-nested-value"},
        }
    }
    shown = json.loads(display_json(json.dumps(source)))["defaultHeaders"]
    assert shown == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "agent-lab",
        **dict.fromkeys(
            ("Authorization", "X-API-Key", "X-Enterprise-Access", "Cookie", "X-Nested"), MASKED
        ),
    }


def test_url_keeps_endpoint_and_public_query_settings_while_masking_credentials() -> None:
    url = "https://user:private-password@example.com/v1?page=2&api%5Fkey=private-key&api_key=second-key&max_tokens=512&X-Amz-Signature=private-signature#private-fragment"
    rendered = display_json(json.dumps({"url": url, "retries": 2}))
    shown = json.loads(rendered)
    parts = urlsplit(shown["url"])
    assert parts.hostname == "example.com"
    assert parts.path == "/v1"
    assert parts.username == MASKED
    assert parts.password is None
    assert parse_qs(parts.query) == {
        "page": ["2"],
        "api_key": [MASKED, MASKED],
        "max_tokens": ["512"],
        "X-Amz-Signature": [MASKED],
    }
    assert parts.fragment == MASKED
    assert shown["retries"] == 2
    assert "private-" not in rendered
    assert "second-key" not in rendered


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/v1?page=2",
        "http://127.0.0.1:8100/mcp",
        "sqlite:///state/checkpoints.sqlite",
        "file:///C:/work/data",
    ],
)
def test_public_urls_and_local_file_urls_remain_readable(url: str) -> None:
    assert json.loads(display_json(json.dumps({"url": url}))) == {"url": url}


def test_oauth_and_session_query_values_are_masked() -> None:
    shown = json.loads(
        display_json(
            json.dumps(
                {
                    "url": "https://example.com/callback?code=private-code&state=private-state&session_id=private-session&format=json"
                }
            )
        )
    )
    assert parse_qs(urlsplit(shown["url"]).query) == {
        "code": [MASKED],
        "state": [MASKED],
        "session_id": [MASKED],
        "format": ["json"],
    }


@pytest.mark.parametrize(
    "url", ["https://[invalid", "https://user:secret@host:bad", "https://host/\nprivate"]
)
def test_malformed_urls_are_masked_without_echoing_the_value(url: str) -> None:
    assert json.loads(display_json(json.dumps({"url": url}))) == {"url": MASKED}


@pytest.mark.parametrize(
    "value",
    [
        "Bearer private-test-token",
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
        "-----BEGIN PRIVATE KEY-----\nprivate-key-material\n-----END PRIVATE KEY-----",
    ],
)
def test_recognizable_credentials_are_redacted_inside_public_text_fields(value: str) -> None:
    rendered = display_json(json.dumps({"description": value}))
    assert value not in rendered
    assert "private-test-token" not in rendered
    assert "private-key-material" not in rendered


def test_json_stays_valid_and_single_line_even_when_values_are_long() -> None:
    rendered = display_json(
        json.dumps({"path": "first\n\x1b[31msecond", "label": "x" * 500, "enabled": True})
    )
    shown = json.loads(rendered)
    assert shown["path"] == "first\n\x1b[31msecond"
    assert shown["enabled"] is True
    assert len(shown["label"]) == 180
    assert "\n" not in rendered
    assert "\x1b" not in rendered


@pytest.mark.parametrize("value", ['{"api_key": "do-not-print"', "not-json", "[" * 1500])
def test_unparseable_json_never_falls_back_to_raw_text(value: str) -> None:
    assert display_json(value) == MASKED


def test_deeply_nested_values_are_bounded_without_exposing_hidden_subtrees() -> None:
    value = '{"password":"private-value"}'
    for _ in range(30):
        value = '{"nested":' + value + "}"
    rendered = display_json(value)
    assert "private-value" not in rendered
    assert MASKED in rendered
    assert isinstance(json.loads(rendered), dict)
