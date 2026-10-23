"""A samples file written before the rename carries an ``agentlib.*`` schema and still loads."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcraft2_cli.errors import CliError
from agentcraft2_cli.policy import load_samples

SAMPLE = "  - {name: x, action: tool.call, application: a, resource: r, expect: allow}\n"


def test_a_samples_file_with_the_old_schema_loads_the_same(tmp_path: Path) -> None:
    old, new = tmp_path / "old.yaml", tmp_path / "new.yaml"
    old.write_text("schema: agentlib.policy-samples/v1\nsamples:\n" + SAMPLE, encoding="utf-8")
    new.write_text("schema: agentcraft2.policy-samples/v1\nsamples:\n" + SAMPLE, encoding="utf-8")
    assert load_samples(old) == load_samples(new)
    assert [sample.name for sample in load_samples(old)] == ["x"]


def test_a_later_version_of_the_old_schema_is_still_refused(tmp_path: Path) -> None:
    path = tmp_path / "samples.yaml"
    path.write_text("schema: agentlib.policy-samples/v2\nsamples: []\n", encoding="utf-8")
    with pytest.raises(CliError, match="not a samples document"):
        load_samples(path)
