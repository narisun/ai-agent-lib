"""Every data source passes the same contract suite."""

from __future__ import annotations

from pathlib import Path

import httpx

from ai_agent_lib_core.adapters import (
    DuckDbCsvDataSource,
    DuckDbCsvOptions,
    RestDataSource,
    RestOptions,
)
from ai_agent_lib_core.contracts import DataSource
from ai_agent_lib_core.testing import (
    FrozenClock,
    accounts_api,
    fake_accounts_source,
    write_accounts_endpoints,
    write_accounts_files,
)
from ai_agent_lib_core.testing.contracts import DataSourceContract


class TestDuckDbCsvDataSource(DataSourceContract):
    async def make_source(self, tmp_path: Path) -> DataSource:
        data_dir, queries_dir = write_accounts_files(tmp_path)
        source = DuckDbCsvDataSource(
            "accounts", DuckDbCsvOptions(data_dir=data_dir, queries_dir=queries_dir), FrozenClock()
        )
        await source.start()
        return source


class TestFakeDataSource(DataSourceContract):
    async def make_source(self, tmp_path: Path) -> DataSource:
        return fake_accounts_source()


class TestRestDataSource(DataSourceContract):
    async def make_source(self, tmp_path: Path) -> DataSource:
        source = RestDataSource(
            "accounts",
            RestOptions(
                base_url="https://api.example.test", queries_dir=write_accounts_endpoints(tmp_path)
            ),
            FrozenClock(),
            transport=httpx.MockTransport(accounts_api),
        )
        await source.start()
        return source
