"""What a service needs from its cloud, derived from the adapters it selects."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

import pytest

from ai_agent_lib_core.config import ConfigResolver, MappingConfigSource
from ai_agent_lib_core.contracts import (
    Access,
    AccessQuery,
    ConfigurationError,
    ModelRef,
    ModelSection,
    NoOptions,
    OptionsModel,
    ProviderSelection,
    Section,
    ServiceConfig,
)
from ai_agent_lib_core.di import DATA_PORT, MODEL_PORT, BuildContext, ServiceProviders, access_plan


class _BucketOptions(OptionsModel):
    bucket: str


def _bucket_access(query: AccessQuery) -> Sequence[Access]:
    options = query.selection.parse_options(_BucketOptions)
    return (Access(("store:Read",), (f"bucket/{options.bucket}",), f"read {query.instance}"),)


def _model_access(query: AccessQuery) -> Sequence[Access]:
    return (Access(("model:Call",), tuple(f"model/{m}" for m in query.model_ids), "call"),)


def _local(**changes: Any) -> ServiceConfig:
    """The configuration of a service with nothing set: the local profile."""
    return replace(ConfigResolver(MappingConfigSource({})).resolve(), **changes)


def _never_built(context: BuildContext) -> object:  # pragma: no cover - a plan builds nothing
    raise AssertionError(context.port)


def _providers() -> ServiceProviders:
    providers = ServiceProviders.default()
    providers.register(MODEL_PORT, "cloud", _never_built, options=NoOptions, access=_model_access)
    providers.register(
        DATA_PORT, "bucket", _never_built, options=_BucketOptions, access=_bucket_access
    )
    providers.register(Section.AUDIT, "mystery", _never_built)
    return providers


def test_every_core_adapter_says_it_needs_nothing_from_the_cloud() -> None:
    plan = access_plan(_local())

    # No model is configured, so no model provider is built or granted anything.
    assert len(plan.adapters) == len(Section)
    assert all(adapter.declared for adapter in plan.adapters)
    assert plan.access == ()
    # The local profile's own adapters may not be deployed, and the plan says which.
    assert "audit jsonl" in [adapter.label for adapter in plan.local_only]


def test_each_model_provider_is_told_every_model_it_serves() -> None:
    config = _local(
        model=ModelSection(
            provider="cloud",
            model_id="big",
            aliases={
                "fast": ModelRef(provider="cloud", model_id="small"),
                "same": ModelRef(provider="cloud", model_id="big"),
            },
        ),
    )

    plan = access_plan(config, _providers())

    model = next(adapter for adapter in plan.adapters if adapter.port == MODEL_PORT)
    assert model.access == (Access(("model:Call",), ("model/big", "model/small"), "call"),)


def test_a_data_source_answers_from_its_own_options_and_repeats_are_merged() -> None:
    sources = {
        name: ProviderSelection(provider="bucket", options={"bucket": "ledger"})
        for name in ("a", "b")
    }
    plan = access_plan(_local(data_sources=sources), _providers())

    labels = [adapter.label for adapter in plan.adapters if adapter.port == DATA_PORT]
    assert labels == ["data source a (bucket)", "data source b (bucket)"]
    assert [access.why for access in plan.access] == ["read a", "read b"]


def test_an_adapter_that_does_not_say_is_reported_not_guessed() -> None:
    sections = {**_local().sections, Section.AUDIT: ProviderSelection(provider="mystery")}

    plan = access_plan(_local(sections=sections), _providers())

    assert [adapter.label for adapter in plan.undeclared] == ["audit mystery"]


def test_bad_options_are_named_before_anything_is_derived() -> None:
    sources = {"a": ProviderSelection(provider="bucket", options={"bukket": "x"})}

    with pytest.raises(ConfigurationError) as caught:
        access_plan(_local(data_sources=sources), _providers())

    assert "bucket" in str(caught.value)


def test_an_access_needs_an_action_and_a_resource() -> None:
    with pytest.raises(ValueError, match="at least one action"):
        Access((), ("x",), "nothing")


def test_r19_a_service_without_a_model_gets_no_model_access() -> None:
    plan = access_plan(_local(model=ModelSection(provider="cloud")), _providers())
    assert MODEL_PORT not in {adapter.port for adapter in plan.adapters}
