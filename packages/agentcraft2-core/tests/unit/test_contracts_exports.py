"""The contracts package exports exactly what its public modules export."""

from __future__ import annotations

import importlib
import pkgutil

from agentcraft2_core import contracts


def test_the_package_exports_the_union_of_its_public_modules() -> None:
    union: set[str] = set()
    for module in pkgutil.iter_modules(contracts.__path__):
        if module.name.startswith("_"):
            continue
        loaded = importlib.import_module(f"agentcraft2_core.contracts.{module.name}")
        names = getattr(loaded, "__all__", None)
        assert names is not None, f"contracts.{module.name} declares no __all__"
        union |= set(names)
    assert set(contracts.__all__) == union
    assert len(contracts.__all__) == len(set(contracts.__all__))
    for name in contracts.__all__:
        assert hasattr(contracts, name), name
