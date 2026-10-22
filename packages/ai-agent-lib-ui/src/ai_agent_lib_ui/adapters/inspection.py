"""Read bounded diagnostics from a registry-approved agent, with no browser URLs."""

import asyncio
import logging
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from ai_agent_lib_core.contracts.inspection import InspectionSection, TurnInspection
from ai_agent_lib_ui.contracts.catalog import AgentBinding
from ai_agent_lib_ui.contracts.conversations import RunRecord
from ai_agent_lib_ui.contracts.queries import ContractError, ErrorCode, ViewContext

logger = logging.getLogger(__name__)


async def fetch_inspection(
    client: httpx.AsyncClient, view: ViewContext, binding: AgentBinding, run: RunRecord
) -> TurnInspection:
    """Validate scope, turn IDs, schema and size before displaying remote content."""
    if run.scope != view.scope or binding.manifest.agent_id != view.scope.application:
        raise ContractError(ErrorCode.NOT_FOUND)
    unavailable = TurnInspection(
        scope=run.scope, thread_id=run.thread_id, request_id=run.request_id
    )
    try:
        async with asyncio.timeout(10):
            async with client.stream(
                "GET",
                binding.base_url + "/requests/" + quote(run.request_id, safe="") + "/inspection",
                params={"thread_id": run.thread_id},
                timeout=10,
                follow_redirects=False,
            ) as response:
                if response.status_code == 404:
                    missing = InspectionSection(availability="unsupported")
                    return unavailable.model_copy(
                        update=dict.fromkeys(
                            ("model_inputs", "model_outputs", "mcp", "checkpoints"), missing
                        )
                    )
                if response.status_code != 200:
                    return unavailable
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 512 * 1024:
                        return unavailable
        result = TurnInspection.model_validate_json(body)
        if (result.scope, result.thread_id, result.request_id) != (
            run.scope,
            run.thread_id,
            run.request_id,
        ):
            return unavailable
        return result
    except (httpx.HTTPError, TimeoutError, ValidationError, ValueError) as error:
        # The panel says "unavailable"; the log says which agent and turn, and why.
        logger.warning(
            "inspection unavailable",
            extra={
                "error_type": type(error).__name__,
                "agent_id": binding.manifest.agent_id,
                "thread_id": run.thread_id,
                "request_id": run.request_id,
            },
        )
        return unavailable
