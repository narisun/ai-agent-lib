"""The ``EAP_`` spellings keep working for one minor release, and say so.

Every test here is a dedicated legacy-alias test: it spells variables with the
old ``EAP_`` prefix on purpose. The rest of the suite speaks ``AC2_``.
"""

from __future__ import annotations

import logging
import warnings
from collections.abc import Iterator

import pytest

from agentcraft2_core.config import (
    DEFAULT_BINDINGS,
    LEGACY_PREFIX,
    PREFIX,
    Binding,
    ConfigResolver,
    Key,
    MappingConfigSource,
    Resolution,
    log_resolution_warnings,
    render_env_example,
    render_reference,
)
from agentcraft2_core.config import deprecations as deprecations_module
from agentcraft2_core.config.bindings import (
    _OWNED_NAME,
    LEGACY_SECRET_VARIABLE_PREFIX,
    SECRET_VARIABLE_PREFIX,
    secret_name,
)
from agentcraft2_core.contracts import ConfigurationError


def explain(values: dict[str, str]) -> Resolution:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return ConfigResolver(MappingConfigSource(values)).explain()


# ------------------------------------------------------------- the binding table


def test_the_canonical_prefix_is_ac2_and_the_legacy_prefix_is_eap() -> None:
    assert PREFIX == "AC2_"
    assert LEGACY_PREFIX == "EAP_"
    assert SECRET_VARIABLE_PREFIX == "AC2_SECRET_"
    assert LEGACY_SECRET_VARIABLE_PREFIX == "EAP_SECRET_"


def test_every_owned_binding_accepts_its_legacy_spelling() -> None:
    owned = [binding for binding in DEFAULT_BINDINGS if binding.name.startswith(PREFIX)]
    assert owned
    for binding in owned:
        legacy = LEGACY_PREFIX + binding.name.removeprefix(PREFIX)
        if binding.key == Key.HOME:
            # It never had the earlier prefix, so there is nothing to carry over.
            assert legacy not in binding.deprecated_names
            continue
        assert legacy in binding.deprecated_names, binding.name


def test_third_party_bindings_gain_no_legacy_spelling() -> None:
    for binding in DEFAULT_BINDINGS:
        if not binding.name.startswith(PREFIX):
            assert binding.deprecated_names == (), binding.name


def test_owned_name_pattern_matches_ac2_names() -> None:
    assert _OWNED_NAME.match("AC2_MODEL_PROVIDER")
    assert not _OWNED_NAME.match("EAP_MODEL_PROVIDER")


def test_secret_variables_are_recognised_in_either_spelling() -> None:
    assert secret_name("AC2_SECRET_RATES_TOKEN") == "rates_token"
    assert secret_name("EAP_SECRET_RATES_TOKEN") == "rates_token"
    assert secret_name("AC2_SECRET_") is None


def test_the_reference_states_the_rename_once_and_has_no_per_row_notes() -> None:
    reference = render_reference()
    assert "previously spelled `EAP_" in reference
    assert "replaces `EAP_" not in reference


def test_a_custom_table_is_rendered_untouched() -> None:
    table = [Binding("AC2_NEW", "k", "The setting.", deprecated_names=("AC2_OLD",))]
    reference = render_reference(table)
    assert "replaces `AC2_OLD`" in reference
    assert "EAP_" not in reference
    assert "EAP_" not in render_env_example(table)


def test_the_env_example_states_the_rename_for_the_library_table_only() -> None:
    assert "previously spelled EAP_*" in render_env_example()
    assert "previously spelled" not in render_env_example([Binding("AC2_NEW", "k", "A setting.")])


# ------------------------------------------------------------ bound variables


def test_a_legacy_name_applies_and_the_warning_names_its_replacement() -> None:
    resolution = explain({"EAP_MODEL_PROVIDER": "fake"})
    assert resolution.config.model.provider == "fake"
    assert resolution.warnings == (
        "EAP_MODEL_PROVIDER is deprecated; use AC2_MODEL_PROVIDER instead",
    )
    assert resolution.origins["model.provider"] == "variable EAP_MODEL_PROVIDER"


def test_a_legacy_name_still_raises_the_deprecation_warning_for_api_callers() -> None:
    with pytest.warns(DeprecationWarning, match="use AC2_MODEL_PROVIDER instead"):
        ConfigResolver(MappingConfigSource({"EAP_MODEL_PROVIDER": "fake"})).explain()


def test_both_spellings_with_equal_values_coexist_silently() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        resolution = ConfigResolver(
            MappingConfigSource({"AC2_MODEL_PROVIDER": "fake", "EAP_MODEL_PROVIDER": "fake"})
        ).explain()
    assert resolution.config.model.provider == "fake"
    assert resolution.warnings == ()
    assert resolution.origins["model.provider"] == "variable AC2_MODEL_PROVIDER"


def test_both_spellings_with_different_values_stop_startup_naming_both() -> None:
    with pytest.raises(ConfigurationError) as caught:
        explain({"AC2_MODEL_PROVIDER": "fake", "EAP_MODEL_PROVIDER": "bedrock"})
    message = str(caught.value)
    assert "AC2_MODEL_PROVIDER" in message
    assert "EAP_MODEL_PROVIDER" in message
    assert "fake" not in message
    assert "bedrock" not in message


def test_an_unknown_canonical_name_stops_startup() -> None:
    with pytest.raises(ConfigurationError, match="unknown configuration variable") as caught:
        explain({"AC2_TYPO": "x"})
    assert "AC2_TYPO" in str(caught.value)


def test_an_unknown_legacy_name_also_stops_startup() -> None:
    with pytest.raises(ConfigurationError, match="unknown configuration variable") as caught:
        explain({"EAP_TYPO": "x"})
    assert "EAP_TYPO" in str(caught.value)
    assert "AC2_" in str(caught.value)


def test_an_unknown_legacy_name_suggests_the_canonical_vocabulary() -> None:
    with pytest.raises(ConfigurationError) as caught:
        explain({"EAP_MODEL_PROVIDR": "fake"})
    assert "did you mean AC2_MODEL_PROVIDER" in str(caught.value)


@pytest.mark.parametrize("vendor", ["API_KEY", "PROJECT_NAME", "COLLECTOR_ENDPOINT"])
@pytest.mark.parametrize("prefix", [PREFIX, LEGACY_PREFIX])
def test_a_prefixed_spelling_of_a_vendor_phoenix_name_is_unknown(prefix: str, vendor: str) -> None:
    # The vendor's PHOENIX_* names are read as written; AC2_PHOENIX_API_KEY would be accepted
    # and then ignored, which is how a setting silently vanishes.
    name = f"{prefix}PHOENIX_{vendor}"
    with pytest.raises(ConfigurationError, match="unknown configuration variable") as caught:
        explain({name: "x"})
    assert name in str(caught.value)
    assert f"did you mean PHOENIX_{vendor}" in str(caught.value)


def test_the_vendor_phoenix_names_themselves_are_not_policed() -> None:
    explain(
        {"PHOENIX_API_KEY": "k", "PHOENIX_PROJECT_NAME": "p", "PHOENIX_COLLECTOR_ENDPOINT": "e"}
    )


def test_the_librarys_own_phoenix_names_are_known_in_both_spellings() -> None:
    names = {
        f"{prefix}PHOENIX_{setting}"
        for prefix in (PREFIX, LEGACY_PREFIX)
        for setting in ("ENABLED", "CAPTURE_CONTENT", "ENV_FILE")
    }
    explain(dict.fromkeys(names, "false"))


def test_a_legacy_name_whose_canonical_spelling_exists_is_pointed_at_it() -> None:
    # A table whose variable has no legacy alias: the canonical spelling is still suggested.
    table = [Binding("AC2_ONLY", "only", "A setting with no alias.")]
    resolver = ConfigResolver(MappingConfigSource({"EAP_ONLY": "x"}), bindings=table)
    with pytest.raises(ConfigurationError) as caught:
        resolver.explain()
    assert "did you mean AC2_ONLY" in str(caught.value)


# ------------------------------------------------------------- the secrets family


def test_a_legacy_secret_variable_supplies_the_secret_with_a_warning() -> None:
    resolution = explain({"EAP_SECRET_RATES_TOKEN": "t0ken"})
    assert resolution.config.secrets["rates_token"].get_secret_value() == "t0ken"
    assert resolution.origins["secret.rates_token"] == "variable EAP_SECRET_RATES_TOKEN"
    assert resolution.warnings == (
        "EAP_SECRET_RATES_TOKEN is deprecated; use AC2_SECRET_RATES_TOKEN instead",
    )
    assert "t0ken" not in " ".join(resolution.warnings)


def test_a_canonical_secret_variable_supplies_the_secret_without_a_warning() -> None:
    resolution = explain({"AC2_SECRET_RATES_TOKEN": "t0ken"})
    assert resolution.config.secrets["rates_token"].get_secret_value() == "t0ken"
    assert resolution.origins["secret.rates_token"] == "variable AC2_SECRET_RATES_TOKEN"
    assert resolution.warnings == ()


def test_both_secret_spellings_with_equal_values_coexist_silently() -> None:
    resolution = explain({"AC2_SECRET_RATES_TOKEN": "t0ken", "EAP_SECRET_RATES_TOKEN": "t0ken"})
    assert resolution.config.secrets["rates_token"].get_secret_value() == "t0ken"
    assert resolution.warnings == ()


def test_both_secret_spellings_with_different_values_stop_startup_naming_both() -> None:
    with pytest.raises(ConfigurationError) as caught:
        explain({"AC2_SECRET_RATES_TOKEN": "v-alpha", "EAP_SECRET_RATES_TOKEN": "v-beta"})
    message = str(caught.value)
    assert "AC2_SECRET_RATES_TOKEN" in message
    assert "EAP_SECRET_RATES_TOKEN" in message
    assert "v-alpha" not in message
    assert "v-beta" not in message


@pytest.mark.parametrize("name", ["EAP_SECRET_", "EAP_SECRET_rates", "EAP_SECRET__X"])
def test_a_malformed_legacy_secret_variable_is_unknown(name: str) -> None:
    with pytest.raises(ConfigurationError, match="unknown configuration variable"):
        explain({name: "value"})


# ---------------------------------------------------------------- the warning channel


@pytest.fixture(autouse=True)
def _fresh_channel() -> Iterator[None]:
    deprecations_module._LOGGED.clear()
    yield
    deprecations_module._LOGGED.clear()


def _config_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "agentcraft2_core.config"]


def test_resolution_warnings_reach_the_library_log(caplog: pytest.LogCaptureFixture) -> None:
    resolution = explain({"EAP_MODEL_PROVIDER": "fake"})
    with caplog.at_level(logging.WARNING, logger="agentcraft2_core.config"):
        log_resolution_warnings(resolution)
    records = _config_records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING]
    assert records[0].getMessage() == (
        "EAP_MODEL_PROVIDER is deprecated; use AC2_MODEL_PROVIDER instead"
    )


def test_each_warning_is_logged_once_per_process(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="agentcraft2_core.config"):
        log_resolution_warnings(explain({"EAP_MODEL_PROVIDER": "fake"}))
        log_resolution_warnings(explain({"EAP_MODEL_PROVIDER": "fake"}))
    assert len(_config_records(caplog)) == 1


def test_a_different_name_is_logged_separately(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="agentcraft2_core.config"):
        log_resolution_warnings(explain({"EAP_MODEL_PROVIDER": "fake"}))
        log_resolution_warnings(explain({"EAP_MODEL_PROVIDER": "fake", "EAP_PROFILE": "local"}))
    assert len(_config_records(caplog)) == 2


def test_a_resolution_without_warnings_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="agentcraft2_core.config"):
        log_resolution_warnings(explain({"AC2_MODEL_PROVIDER": "fake"}))
    assert _config_records(caplog) == []
