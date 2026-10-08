"""Testing a service that reads a REST API, without the API.

``rest_stub_providers`` gives a container ``rest`` data sources that are real
in every way but one: requests are answered from a table in the test instead
of being sent. The endpoint definitions, the parameter checks, the row cap
and the masks all run as they do in production.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from ai_agent_lib_core.adapters import RestDataSource, RestOptions
from ai_agent_lib_core.di import DATA_PORT, BuildContext, ServiceProviders

__all__ = ["RestResponder", "rest_stub_providers", "rest_stub_transport"]

RestResponder = Callable[[httpx.Request], httpx.Response]
"""Answers one request. Use it where the answer depends on the query string or the body."""

_NOT_FOUND = 404
_TEST_TOKEN = "test-token"  # noqa: S105 - a stand-in, sent to nothing


def rest_stub_transport(responses: Mapping[str, Any]) -> httpx.MockTransport:
    """Return a transport that answers from ``responses`` and never uses the network.

    Args:
        responses: What the API answers, by ``"METHOD /path"``. A value is the
            JSON body of a 200 response, or a ``(status, body)`` pair, or a
            function from the request to a response. The query string is not
            part of the key. A request with no entry gets a 404.
    """

    def answer(request: httpx.Request) -> httpx.Response:
        found = responses.get(f"{request.method} {request.url.path}")
        if found is None:
            return httpx.Response(_NOT_FOUND, json={"error": "no stubbed response"})
        if callable(found):
            response: httpx.Response = found(request)
            return response
        status, body = found if isinstance(found, tuple) else (200, found)
        return httpx.Response(
            int(status), content=json.dumps(body), headers={"content-type": "application/json"}
        )

    return httpx.MockTransport(answer)


def rest_stub_providers(
    responses: Mapping[str, Any], providers: ServiceProviders | None = None
) -> ServiceProviders:
    """Return providers whose ``rest`` data sources answer from ``responses``.

    A source that is configured to send a token gets a stand-in, so a test
    needs no secret.

    Args:
        responses: What the API answers; see ``rest_stub_transport``.
        providers: The providers to change. By default the local adapters.
    """
    transport = rest_stub_transport(responses)

    async def stubbed(context: BuildContext) -> RestDataSource:
        options = context.selection.parse_options(RestOptions)
        source = RestDataSource(
            context.instance,
            options,
            context.clock,
            token=SecretStr(_TEST_TOKEN) if options.auth_secret is not None else None,
            proxy="http://unused.invalid" if options.use_proxy else None,
            transport=transport,
        )
        await source.start()
        return source

    chosen = providers if providers is not None else ServiceProviders.default()
    return chosen.register(DATA_PORT, "rest", stubbed, options=RestOptions, replace=True)
