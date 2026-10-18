"""Terminal outcomes survive lost responses without authorizing replay or cross-owner reads."""

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ai_agent_lib_core import Principal, RequestContext, ValidationFailed
from ai_agent_lib_core.integrations.http import ServiceLifecycle, SqliteOutcomeJournal, agent_app
from ai_agent_lib_core.testing import FakeIdentityVerifier, Fakes


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("fail", [False, True])
async def test_saved_outcome_matches_wire_and_is_read_without_execution(
    tmp_path: Path,
    streaming: bool,
    fail: bool,
) -> None:
    owner = Principal(subject="owner", tenant="bank", roles=frozenset({"rm"}))
    kit = Fakes(
        identity=FakeIdentityVerifier(
            {
                "owner": owner,
                "other": replace(owner, subject="other"),
                "changed-role": replace(owner, roles=frozenset()),
            }
        )
    )
    executions = 0

    async def ready() -> None:
        pass

    async def run(context: RequestContext, given: object) -> dict[str, str]:
        nonlocal executions
        executions += 1
        if fail:
            raise ValidationFailed("synthetic refusal")
        return {"answer": "Accepted synthetic answer"}

    path = tmp_path / "outcomes.sqlite"
    async with kit.container() as services:
        lifecycle = ServiceLifecycle(ready)
        await lifecycle.start()
        app = agent_app(
            services,
            run,
            application="demo",
            lifecycle=lifecycle,
            outcomes=SqliteOutcomeJournal(path),
            live_updates=True,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            headers = {
                "authorization": "Bearer owner",
                "x-request-id": "request",
                "accept": "text/event-stream" if streaming else "application/json",
            }
            response = await client.post(
                "/invoke", headers=headers, json={"input": {}, "thread_id": "thread"}
            )
            if streaming:
                wire = json.loads(response.text.split("data: ")[-1].strip())
            else:
                wire = response.json()
            saved = await client.get("/requests/request?thread_id=thread", headers=headers)
            assert saved.json() == wire
            assert saved.json()["elapsed_ms"] >= 0
            for token in ("other", "changed-role"):
                hidden = await client.get(
                    "/requests/request?thread_id=thread",
                    headers={"authorization": "Bearer " + token},
                )
                assert hidden.json() == {"state": "unknown"}
            for query in ("/requests/absent?thread_id=thread", "/requests/request?thread_id=other"):
                assert (await client.get(query, headers=headers)).json() == {"state": "unknown"}
            duplicate = await client.post(
                "/invoke", headers=headers, json={"input": {}, "thread_id": "thread"}
            )
            assert duplicate.status_code == 503
    assert executions == 1
    context = RequestContext(
        principal=owner, application="demo", thread_id="thread", request_id="read"
    )
    assert await SqliteOutcomeJournal(path).read(context, "request") == wire


async def test_incomplete_reservation_never_becomes_a_confirmed_failure(tmp_path: Path) -> None:
    context = RequestContext(
        principal=Principal(subject="owner", tenant="bank"),
        application="demo",
        thread_id="thread",
        request_id="request",
    )
    journal = SqliteOutcomeJournal(tmp_path / "outcomes.sqlite")
    assert await journal.begin(context)
    assert not await journal.begin(context)
    assert await journal.read(context, "request") is None


@pytest.mark.parametrize("route", ["/invoke", "/invoke/stream"])
@pytest.mark.parametrize("headers", [{}, {"authorization": "Bearer stranger"}])
async def test_a_refused_caller_never_reaches_the_journal(
    tmp_path: Path, route: str, headers: dict[str, str]
) -> None:
    kit = Fakes(
        identity=FakeIdentityVerifier(
            {"owner": Principal(subject="owner", tenant="bank", roles=frozenset({"rm"}))}
        )
    )
    executions = 0

    async def ready() -> None:
        pass

    async def run(context: RequestContext, given: object) -> dict[str, str]:
        nonlocal executions
        executions += 1
        return {"answer": "unreachable"}

    async def stream(context: RequestContext, given: object) -> AsyncIterator[dict[str, str]]:
        nonlocal executions
        executions += 1
        yield {"answer": "unreachable"}

    path = tmp_path / "outcomes.sqlite"
    async with kit.container() as services:
        lifecycle = ServiceLifecycle(ready)
        await lifecycle.start()
        app = agent_app(
            services,
            run,
            application="demo",
            lifecycle=lifecycle,
            stream=stream,
            outcomes=SqliteOutcomeJournal(path),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            reply = await client.post(
                route,
                headers={**headers, "x-request-id": "request"},
                json={"input": {}, "thread_id": "thread"},
            )
    assert reply.status_code == 401
    assert reply.json()["error"] == "unauthorized"
    assert executions == 0
    # The journal creates its file on first use, so no file means no begin, finish or read.
    assert not path.exists()
