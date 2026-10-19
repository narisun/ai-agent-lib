"""The UI reads the agent's ``AC2_`` settings; the ``EAP_`` spellings still work for a release.

Every test here that spells a variable ``EAP_`` does so on purpose: it is a dedicated
legacy-alias test. The UI has no ``warnings`` channel to use (its tests turn warnings into
errors), so a deprecation is a WARNING on the library's configuration logger, once per process.
"""

import json
import logging
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ai_agent_lib_ui.config import settings as settings_module
from ai_agent_lib_ui.config.settings import LocalIdentity, agent_environment

CHANNEL = "ai_agent_lib_core.config"
REGISTRY = {"agents_path": "agents.yaml", "tools_path": "tools.yaml"}
IDENTITY = {"subject": "ada", "tenant": "bank", "roles": ["analyst"]}


@pytest.fixture(autouse=True)
def fresh_channel() -> Iterator[None]:
    settings_module._REPORTED.clear()
    yield
    settings_module._REPORTED.clear()


def write(tmp_path: Path, **lines: str) -> Path:
    env = tmp_path / ".env"
    env.write_text("".join(f"{name}={value}\n" for name, value in lines.items()), encoding="utf-8")
    return env


def deprecations(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == CHANNEL]


def test_canonical_names_are_read_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = write(
        tmp_path,
        AC2_PROFILE="local",
        AC2_REGISTRY_PROVIDER="file",
        AC2_IDENTITY_PROVIDER="static",
        AC2_REGISTRY_OPTIONS=json.dumps(REGISTRY),
        AC2_IDENTITY_OPTIONS=json.dumps(IDENTITY),
    )
    with caplog.at_level(logging.WARNING, logger=CHANNEL):
        agents, tools, identity = agent_environment(env)
    assert agents == (tmp_path / "agents.yaml").resolve()
    assert tools == (tmp_path / "tools.yaml").resolve()
    assert identity == LocalIdentity(subject="ada", tenant="bank", roles=("analyst",))
    assert deprecations(caplog) == []


def test_the_legacy_names_still_work_and_each_warning_names_its_replacement(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = write(
        tmp_path,
        EAP_PROFILE="local",
        EAP_REGISTRY_PROVIDER="file",
        EAP_IDENTITY_PROVIDER="static",
        EAP_REGISTRY_OPTIONS=json.dumps(REGISTRY),
        EAP_IDENTITY_OPTIONS=json.dumps(IDENTITY),
    )
    with caplog.at_level(logging.WARNING, logger=CHANNEL):
        agents, _, identity = agent_environment(env)
    assert agents == (tmp_path / "agents.yaml").resolve()
    assert identity.subject == "ada"
    assert sorted(deprecations(caplog)) == [
        "EAP_IDENTITY_OPTIONS is deprecated; use AC2_IDENTITY_OPTIONS instead",
        "EAP_IDENTITY_PROVIDER is deprecated; use AC2_IDENTITY_PROVIDER instead",
        "EAP_PROFILE is deprecated; use AC2_PROFILE instead",
        "EAP_REGISTRY_OPTIONS is deprecated; use AC2_REGISTRY_OPTIONS instead",
        "EAP_REGISTRY_PROVIDER is deprecated; use AC2_REGISTRY_PROVIDER instead",
    ]


def test_a_legacy_deprecation_is_logged_once_per_process(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = write(tmp_path, EAP_PROFILE="local")
    with caplog.at_level(logging.WARNING, logger=CHANNEL):
        agent_environment(env)
        agent_environment(env)
    assert deprecations(caplog) == ["EAP_PROFILE is deprecated; use AC2_PROFILE instead"]


@pytest.mark.parametrize("name", ["AC2_PROFILE", "EAP_PROFILE"])
def test_a_wrong_profile_is_refused_in_the_canonical_wording(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match=r"requires AC2_PROFILE=local"):
        agent_environment(write(tmp_path, **{name: "aws"}))


@pytest.mark.parametrize("stem", ["REGISTRY_PROVIDER", "IDENTITY_PROVIDER"])
@pytest.mark.parametrize("prefix", ["AC2_", "EAP_"])
def test_a_wrong_provider_is_refused_in_the_canonical_wording(
    tmp_path: Path, prefix: str, stem: str
) -> None:
    with pytest.raises(ValueError, match=rf"supports AC2_{stem}="):
        agent_environment(write(tmp_path, **{prefix + stem: "jwt"}))


@pytest.mark.parametrize(
    ("stem", "value"),
    [
        ("PROFILE", "local"),
        ("REGISTRY_PROVIDER", "file"),
        ("IDENTITY_PROVIDER", "static"),
        ("REGISTRY_OPTIONS", json.dumps(REGISTRY)),
        ("IDENTITY_OPTIONS", json.dumps(IDENTITY)),
    ],
    ids=lambda value: value if value.isidentifier() else "json",
)
def test_both_spellings_with_equal_values_coexist_silently(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, stem: str, value: str
) -> None:
    env = write(tmp_path, **{f"AC2_{stem}": value, f"EAP_{stem}": value})
    with caplog.at_level(logging.WARNING, logger=CHANNEL):
        agent_environment(env)
    assert deprecations(caplog) == []


@pytest.mark.parametrize(
    ("stem", "canonical", "legacy"),
    [
        ("PROFILE", "local", "aws"),
        ("REGISTRY_PROVIDER", "file", "mongo"),
        ("IDENTITY_PROVIDER", "static", "jwt"),
        ("REGISTRY_OPTIONS", json.dumps(REGISTRY), "{}"),
        ("IDENTITY_OPTIONS", json.dumps(IDENTITY), "{}"),
    ],
    ids=lambda value: value if value.isidentifier() else "json",
)
def test_both_spellings_with_different_values_are_refused_naming_both(
    tmp_path: Path, stem: str, canonical: str, legacy: str
) -> None:
    conflict = rf"EAP_{stem} is deprecated and conflicts with AC2_{stem}"
    with pytest.raises(ValueError, match=conflict):
        agent_environment(write(tmp_path, **{f"AC2_{stem}": canonical, f"EAP_{stem}": legacy}))
    # Neither spelling is guessed to be the one meant: the other way round fails the same way.
    with pytest.raises(ValueError, match=conflict):
        agent_environment(write(tmp_path, **{f"EAP_{stem}": canonical, f"AC2_{stem}": legacy}))


def test_defaults_apply_when_neither_spelling_is_set(tmp_path: Path) -> None:
    env = write(tmp_path, ANTHROPIC_API_KEY="not-a-real-key")
    agents, tools, identity = agent_environment(env)
    assert agents == (tmp_path / "registry/agents.yaml").resolve()
    assert tools == (tmp_path / "registry/mcp-tools.yaml").resolve()
    assert identity == LocalIdentity(subject="dev-user", tenant="dev-tenant", roles=())


def test_a_legacy_name_is_reported_once_even_when_threads_read_it_together(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class SlowSet(set[str]):
        """Widens the gap between looking a message up and adding it."""

        def __contains__(self, item: object) -> bool:
            found = super().__contains__(item)
            time.sleep(0.02)
            return found

    monkeypatch.setattr(settings_module, "_REPORTED", SlowSet())
    start = threading.Barrier(8)

    def read() -> str | None:
        start.wait()
        return settings_module._setting({"EAP_PROFILE": "local"}, "PROFILE")

    with (
        caplog.at_level(logging.WARNING, logger=CHANNEL),
        ThreadPoolExecutor(max_workers=8) as pool,
    ):
        values = list(pool.map(lambda _: read(), range(8)))
    assert values == ["local"] * 8
    assert deprecations(caplog) == ["EAP_PROFILE is deprecated; use AC2_PROFILE instead"]
