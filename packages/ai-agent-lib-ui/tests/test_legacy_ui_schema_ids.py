"""Documents saved before the rename carry ``agentlib.*`` schema identifiers and still load."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_agent_lib_ui.application.catalog import read_manifest
from ai_agent_lib_ui.contracts.artifacts import Artifact
from ai_agent_lib_ui.contracts.queries import ContractError

FIXTURES = Path(__file__).parent / "fixtures"


def test_a_presentation_manifest_with_the_old_schema_loads_as_the_new_one() -> None:
    saved = json.loads((FIXTURES / "presentation.json").read_text(encoding="utf-8"))
    saved["schema"] = "agentlib.presentation/v1"
    assert read_manifest(saved) == read_manifest({**saved, "schema": "agentcraft2.presentation/v1"})
    assert read_manifest(saved).schema_version == "agentcraft2.presentation/v1"


def test_a_later_presentation_version_is_still_refused() -> None:
    saved = json.loads((FIXTURES / "presentation.json").read_text(encoding="utf-8"))
    saved["schema"] = "agentlib.presentation/v2"
    with pytest.raises(ContractError):
        read_manifest(saved)


def test_a_saved_artifact_with_the_old_schema_loads_as_the_new_one() -> None:
    invoked = json.loads((FIXTURES / "invoke.json").read_text(encoding="utf-8"))
    (saved,) = invoked["output"]["artifacts"]
    saved["schema"] = "agentlib.artifact/v1"
    assert Artifact.model_validate(saved).schema_version == "agentcraft2.artifact/v1"
