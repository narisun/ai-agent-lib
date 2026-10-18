"""The committed configuration documents match the binding table."""

from __future__ import annotations

import html
import json
import re
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import cast

import build_guide
import pytest
from configuration_examples import AWS_DATA, LOCAL_DATA, configuration_examples

from ai_agent_lib_core.config import DEFAULT_BINDINGS, render_env_example, render_reference
from ai_agent_lib_core.config.bindings import options_key, provider_key
from ai_agent_lib_core.config.phoenix import PHOENIX_VARIABLES
from ai_agent_lib_core.contracts import NoOptions, Section
from ai_agent_lib_core.di import ServiceProviders
from ai_agent_lib_core.integrations.http import AgentService

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("path", "expected", "command"),
    [
        ("docs/variables.md", render_reference(), "reference > docs/variables.md"),
        (".env.example", render_env_example(), "env-example > .env.example"),
    ],
)
def test_generated_document_is_current(path: str, expected: str, command: str) -> None:
    actual = (REPO_ROOT / path).read_text(encoding="utf-8")
    assert actual == expected, (
        f"{path} is stale; regenerate it with: python -m ai_agent_lib_core.config {command}"
    )


def test_the_developer_guide_is_current() -> None:
    actual = (REPO_ROOT / "docs" / "developer-guide.html").read_text(encoding="utf-8")
    assert actual == build_guide.guide_html(), (
        "docs/developer-guide.html is stale; regenerate it with: python docs/guide/build_guide.py"
    )


def test_the_guides_dependency_injection_example_runs() -> None:
    guide = build_guide.guide_html()
    (example,) = re.findall(
        r'<code class="language-python" id="di-example">(.*?)</code>', guide, re.S
    )
    namespace: dict[str, object] = {}
    code = compile(html.unescape(example), "<guide dependency injection example>", "exec")
    exec(code, namespace)  # noqa: S102 - execute the checked-in documentation example
    run_test = cast(Callable[[], None], namespace["test_expiry_uses_the_supplied_clock"])
    run_test()


def test_the_guides_service_example_builds_an_agent_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guide = build_guide.guide_html()
    (example,) = re.findall(
        r'<code class="language-python" id="service-example">(.*?)</code>', guide, re.S
    )
    graph = types.SimpleNamespace(
        APPLICATION="helper", answer=None, build_graph=None, registered_mcp_tools=None
    )
    settings = types.SimpleNamespace(ENV_FILE=Path("helper.env"))
    monkeypatch.setitem(sys.modules, "helper", types.ModuleType("helper"))
    monkeypatch.setitem(sys.modules, "helper.graph", cast(types.ModuleType, graph))
    monkeypatch.setitem(sys.modules, "helper.settings", cast(types.ModuleType, settings))
    namespace: dict[str, object] = {}
    code = compile(html.unescape(example), "<guide service example>", "exec")
    exec(code, namespace)  # noqa: S102 - execute the checked-in documentation example
    service = cast(AgentService, namespace["SERVICE"])
    assert (service.application, service.env_file) == ("helper", Path("helper.env"))
    assert callable(namespace["main"])


def test_walkthrough_docs_and_samples_match_the_shipped_templates() -> None:
    for path, expected in build_guide.walkthrough_files().items():
        assert path.read_text(encoding="utf-8") == expected, (
            f"{path.name} is stale; regenerate it with: python docs/guide/build_guide.py"
        )


def test_the_guide_has_a_note_for_every_adapter_without_options() -> None:
    providers = ServiceProviders.default()
    without = {
        (port, name)
        for port in build_guide.PORTS
        for name in providers.names(port)
        if providers.lookup(port, name).options in (None, NoOptions)
    }
    assert without == set(build_guide.NO_OPTIONS_NOTES)


def test_every_variable_is_explained_in_the_binding_table() -> None:
    assert [binding.name for binding in DEFAULT_BINDINGS if not binding.details] == []


def test_configuration_table_covers_every_binding_and_accepted_name_once() -> None:
    assert set(configuration_examples()) == {binding.key for binding in DEFAULT_BINDINGS}
    table = build_guide.configuration_variables()
    assert table.count("<table") == 1
    assert re.findall(r"<th>(.*?)</th>", table) == [
        "Variable name",
        "Purpose",
        "Local",
        "AWS",
        "Notes",
    ]
    rows = [
        re.findall(r"<td>(.*?)</td>", row, re.S)
        for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)
    ]
    for cells in rows[1:]:
        assert len(cells) == 5
        assert all(cells)
    for binding in DEFAULT_BINDINGS:
        matching = [cells for cells in rows[1:] if f">{binding.name}</span>" in cells[0]]
        assert len(matching) == 1, binding.name
        for name in binding.all_names:
            assert name in html.unescape(" ".join(matching[0]))
    for name in PHOENIX_VARIABLES:
        assert sum(f">{name}</span>" in cells[0] for cells in rows[1:]) == 1


@pytest.mark.parametrize("environment", ["local", "aws"])
@pytest.mark.parametrize("section", list(Section))
def test_configuration_table_options_match_selected_adapter(
    environment: str, section: Section
) -> None:
    examples = configuration_examples()
    provider = getattr(examples[provider_key(section)], environment).strip("`")
    raw = getattr(examples[options_key(section)], environment).strip("`")
    options = ServiceProviders.default().lookup(section.value, provider).options
    assert options is not None
    options.model_validate(json.loads(raw))


@pytest.mark.parametrize("sources", [LOCAL_DATA, AWS_DATA], ids=["local", "aws"])
def test_configuration_table_data_sources_match_adapter_options(sources: dict[str, object]) -> None:
    for entry in sources.values():
        assert isinstance(entry, dict)
        raw = dict(entry)
        kind = raw.pop("kind")
        options = ServiceProviders.default().lookup("data", kind).options
        assert options is not None
        options.model_validate(raw)
