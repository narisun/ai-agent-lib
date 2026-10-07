"""Clocks, identifier generators, telemetry and config sources pass their suites."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ai_agent_lib_core.adapters import (
    NullTelemetry,
    OpenTelemetryTelemetry,
    SystemClock,
    UuidGenerator,
)
from ai_agent_lib_core.config import (
    DotenvConfigSource,
    LayeredConfigSource,
    MappingConfigSource,
)
from ai_agent_lib_core.contracts import Clock, ConfigSource, IdGenerator, Telemetry
from ai_agent_lib_core.testing import FrozenClock, RecordingTelemetry, SequentialIds
from ai_agent_lib_core.testing.contracts import (
    ClockContract,
    ConfigSourceContract,
    IdGeneratorContract,
    TelemetryContract,
)


class TestSystemClock(ClockContract):
    def make_clock(self) -> Clock:
        return SystemClock()


class TestFrozenClock(ClockContract):
    def make_clock(self) -> Clock:
        return FrozenClock()


class TestUuidGenerator(IdGeneratorContract):
    def make_generator(self) -> IdGenerator:
        return UuidGenerator()


class TestSequentialIds(IdGeneratorContract):
    def make_generator(self) -> IdGenerator:
        return SequentialIds()


class TestNullTelemetry(TelemetryContract):
    def make_telemetry(self) -> Telemetry:
        return NullTelemetry()


class TestOpenTelemetryTelemetry(TelemetryContract):
    def make_telemetry(self) -> Telemetry:
        return OpenTelemetryTelemetry()


class TestRecordingTelemetry(TelemetryContract):
    def make_telemetry(self) -> Telemetry:
        return RecordingTelemetry()


class TestMappingConfigSource(ConfigSourceContract):
    def make_source(self, values: Mapping[str, str], tmp_path: Path) -> ConfigSource:
        return MappingConfigSource(values)


class TestDotenvConfigSource(ConfigSourceContract):
    def make_source(self, values: Mapping[str, str], tmp_path: Path) -> ConfigSource:
        path = tmp_path / ".env"
        path.write_text(
            "".join(f"{name}='{value}'\n" for name, value in values.items()), encoding="utf-8"
        )
        return DotenvConfigSource(path, required=True)


class TestLayeredConfigSource(ConfigSourceContract):
    def make_source(self, values: Mapping[str, str], tmp_path: Path) -> ConfigSource:
        items = list(values.items())
        return LayeredConfigSource(
            MappingConfigSource(dict(items[:1])), MappingConfigSource(dict(items[1:]))
        )
