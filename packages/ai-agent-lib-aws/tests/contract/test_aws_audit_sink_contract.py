"""The Firehose sink keeps the audit sink contract."""

from __future__ import annotations

import json
from pathlib import Path

from ai_agent_lib_aws.audit_firehose import FirehoseAuditOptions, FirehoseAuditSink
from ai_agent_lib_aws.testing import FakeFirehose, client_error, offline_sessions
from ai_agent_lib_core.contracts import AuditSink
from ai_agent_lib_core.testing.contracts import AuditSinkContract


class TestFirehoseAuditSink(AuditSinkContract):
    def make_sink(self, tmp_path: Path) -> AuditSink:
        self.firehose = FakeFirehose(("audit",))
        return FirehoseAuditSink(
            FirehoseAuditOptions(stream="audit"), offline_sessions(), client=self.firehose
        )

    async def read_records(self, sink: AuditSink) -> list[dict[str, object]]:
        return [json.loads(data) for data in self.firehose.records["audit"]]

    async def break_sink(self, sink: AuditSink) -> bool:
        self.firehose.fail_with = client_error("ServiceUnavailableException", "PutRecord")
        return True
