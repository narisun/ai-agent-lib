"""Read explicitly configured local diagnostic files without changing agent state."""

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

from agentcraft2_core.contracts import RequestContext, is_schema
from agentcraft2_core.contracts.inspection import InspectionSection, TurnInspection

CheckpointPreview = Callable[[RequestContext, str], Awaitable[InspectionSection]]
AuthorizeInspection = Callable[[RequestContext], Awaitable[None]]
_SCAN_BYTES = 64 * 1024 * 1024
_LINE_BYTES = 4 * 1024 * 1024


def preview(records: list[object], *, truncated: bool = False) -> InspectionSection:
    """Bound serialized content without splitting a UTF-8 character."""
    data = json.dumps(records, ensure_ascii=False, indent=2).encode("utf-8")
    return InspectionSection(
        availability="available" if records else "unavailable",
        text=data[:65_536].decode("utf-8", errors="ignore") if records else "",
        truncated=truncated or len(data) > 65_536,
        record_count=len(records),
    )


def _file_records(path: Path) -> Iterator[dict[str, object] | None]:
    """Yield decoded records, with None marking an incomplete local scan."""
    try:
        with path.open("rb") as source:
            size = source.seek(0, 2)
            source.seek(max(0, size - _SCAN_BYTES))
            if size > _SCAN_BYTES:
                source.readline(_LINE_BYTES + 1)
                yield None
            while source.tell() < size:
                line = source.readline(min(_LINE_BYTES + 1, size - source.tell()))
                if len(line) > _LINE_BYTES or not line.endswith(b"\n"):
                    yield None
                    while not line.endswith(b"\n") and source.tell() < size:
                        line = source.readline(min(_LINE_BYTES + 1, size - source.tell()))
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    yield None
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        yield None


def read_records(
    paths: tuple[Path, ...], context: RequestContext, request_id: str, *, event: str
) -> InspectionSection:
    """Read bounded tails, joining on full scope, thread and request, never ID alone."""
    if not paths:
        return InspectionSection(availability="disabled")
    records: list[object] = []
    seen: set[str] = set()
    truncated = False
    content_bytes = 0
    identity = {
        "tenant": context.principal.tenant,
        "subject": context.principal.subject,
        "application": context.application,
        "thread_id": context.thread_id,
        "request_id": request_id,
    }
    schema = "agentcraft2.audit/v1" if event == "tool.call" else "agentcraft2.model-trace/v1"
    accepted = {event, "model.error"} if event == "model.response" else {event}
    for path in paths:
        for record in _file_records(path):
            if record is None:
                truncated = True
                continue
            if any(record.get(key) != value for key, value in identity.items()):
                continue
            if not is_schema(record.get("schema"), schema) or record.get("event") not in accepted:
                continue
            key = json.dumps(record, sort_keys=True)
            if key in seen:
                continue
            if len(records) == 200:
                return preview(records, truncated=True)
            seen.add(key)
            records.append(record)
            content_bytes += len(key.encode("utf-8"))
            if content_bytes > 65_536:
                return preview(records, truncated=True)
    return preview(records, truncated=truncated)


class LocalTurnInspector:
    """Inject authorization, file locations and a scoped decoded checkpoint reader.

    This local adapter reads at most 64 MiB per configured file, 200 records per
    section, and returns at most 64 KiB per preview. Configure a small fixed list
    of private files on the server; browser input never selects a path.
    """

    def __init__(
        self,
        *,
        authorize: AuthorizeInspection,
        model_paths: tuple[Path, ...] = (),
        audit_paths: tuple[Path, ...] = (),
        checkpoints: CheckpointPreview | None = None,
    ) -> None:
        if len(model_paths) > 8 or len(audit_paths) > 8:
            raise ValueError("configure at most eight diagnostic files per section")
        self.authorize = authorize
        self.model_paths, self.audit_paths = model_paths, audit_paths
        self.checkpoints = checkpoints

    async def read(self, context: RequestContext, request_id: str) -> TurnInspection:
        """Authorize before any IO; a broken section never conceals other diagnostics."""
        await self.authorize(context)
        inputs, outputs, mcp = await asyncio.gather(
            asyncio.to_thread(
                read_records, self.model_paths, context, request_id, event="model.request"
            ),
            asyncio.to_thread(
                read_records, self.model_paths, context, request_id, event="model.response"
            ),
            asyncio.to_thread(
                read_records, self.audit_paths, context, request_id, event="tool.call"
            ),
        )
        checkpoints = InspectionSection(availability="unsupported")
        if self.checkpoints is not None:
            try:
                checkpoints = await self.checkpoints(context, request_id)
            except Exception:  # noqa: BLE001 - diagnostic failure has no user-content error text
                checkpoints = InspectionSection(availability="unavailable")
        return TurnInspection(
            scope=context.scope,
            thread_id=context.thread_id,
            request_id=request_id,
            model_inputs=inputs,
            model_outputs=outputs,
            mcp=mcp,
            checkpoints=checkpoints,
        )
