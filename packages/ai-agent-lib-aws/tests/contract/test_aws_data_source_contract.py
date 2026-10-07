"""The Redshift data source passes the contract suite every data source passes."""

from __future__ import annotations

from pathlib import Path

from ai_agent_lib_aws.data_redshift import RedshiftDataOptions, RedshiftDataSource
from ai_agent_lib_aws.testing import FakeRedshiftData, offline_sessions
from ai_agent_lib_core.contracts import DataSource
from ai_agent_lib_core.testing import FrozenClock, write_accounts_files
from ai_agent_lib_core.testing.contracts import DataSourceContract


async def _no_wait(seconds: float) -> None:
    del seconds


class TestRedshiftDataSource(DataSourceContract):
    async def make_source(self, tmp_path: Path) -> DataSource:
        data_dir, queries_dir = write_accounts_files(tmp_path)
        source = RedshiftDataSource(
            "accounts",
            RedshiftDataOptions(queries_dir=queries_dir, database="dev", workgroup="eap"),
            offline_sessions(),
            FrozenClock(),
            client=FakeRedshiftData(data_dir),
            sleep=_no_wait,
        )
        await source.start()
        return source
