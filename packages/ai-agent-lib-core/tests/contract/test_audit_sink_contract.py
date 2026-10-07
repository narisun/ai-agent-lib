"""Every audit sink passes the same contract suite."""

from __future__ import annotations

import json
from pathlib import Path

from ai_agent_lib_core.adapters import JsonlAuditOptions, JsonlAuditSink
from ai_agent_lib_core.contracts import AuditSink
from ai_agent_lib_core.testing import InMemoryAuditSink
from ai_agent_lib_core.testing.contracts import AuditSinkContract


class TestJsonlAuditSink(AuditSinkContract):
    def make_sink(self, tmp_path: Path) -> AuditSink:
        return JsonlAuditSink(JsonlAuditOptions(path=tmp_path / "logs" / "audit.jsonl"))

    async def read_records(self, sink: AuditSink) -> list[dict[str, object]]:
        assert isinstance(sink, JsonlAuditSink)
        lines = sink.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines]

    async def break_sink(self, sink: AuditSink) -> bool:
        assert isinstance(sink, JsonlAuditSink)
        # Replace the target with a directory, which cannot be opened for appending.
        await sink.aclose()
        if sink.path.exists():
            sink.path.unlink()
        sink.path.mkdir(parents=True)
        return True


class TestInMemoryAuditSink(AuditSinkContract):
    def make_sink(self, tmp_path: Path) -> AuditSink:
        return InMemoryAuditSink()

    async def read_records(self, sink: AuditSink) -> list[dict[str, object]]:
        assert isinstance(sink, InMemoryAuditSink)
        return [record.to_dict() for record in sink.records]

    async def break_sink(self, sink: AuditSink) -> bool:
        assert isinstance(sink, InMemoryAuditSink)
        sink.fail_with = OSError("simulated outage")
        return True
