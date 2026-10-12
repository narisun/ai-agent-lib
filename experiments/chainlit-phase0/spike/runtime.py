"""Explicit composition shared by the mounted callbacks and parent routes."""

from dataclasses import dataclass

from spike.application import Runs
from spike.settings import Settings
from spike.store import Store


@dataclass
class Runtime:
    """Resources owned by one sample process."""

    settings: Settings
    store: Store
    runs: Runs


_runtime: Runtime | None = None


def configure(settings: Settings) -> Runtime:
    """Build resources once before Chainlit loads its callback module."""
    global _runtime
    store = Store(settings.state / "sample.sqlite")
    store.recover()
    _runtime = Runtime(settings, store, Runs(store, settings))
    return _runtime


def runtime() -> Runtime:
    """Return configured resources; never initialize them during a request."""
    if _runtime is None:
        raise RuntimeError("Configure the sample before loading callbacks")
    return _runtime
