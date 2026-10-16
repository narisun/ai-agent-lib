"""Ephemeral draft snapshots; a slow or disconnected browser cannot stop dispatch."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class Draft:
    """Public text from the latest provider attempt, not an accepted answer."""

    call: int
    text: str


ProgressCallback = Callable[[Draft], Awaitable[None]]


class ProgressFeed:
    """Keep one bounded snapshot per listener, coalescing rapid token updates."""

    def __init__(self) -> None:
        self.current = Draft(0, "")
        self.listeners: set[asyncio.Queue[Draft]] = set()

    def publish(self, call: int, text: str, *, reset: bool) -> None:
        """Retain at most 65,536 draft characters, separate from transport byte limits."""
        if call < self.current.call:
            return
        previous = "" if reset or call != self.current.call else self.current.text
        self.current = Draft(call, (previous + text)[-65_536:])
        for queue in self.listeners:
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(self.current)

    @contextmanager
    def listen(self) -> Iterator[asyncio.Queue[Draft]]:
        """A browser owns only its subscription, never the request's lifetime."""
        queue: asyncio.Queue[Draft] = asyncio.Queue(maxsize=1)
        if self.current.call:
            queue.put_nowait(self.current)
        self.listeners.add(queue)
        try:
            yield queue
        finally:
            self.listeners.discard(queue)


_FEED: ContextVar[ProgressFeed | None] = ContextVar("ui_progress", default=None)


@contextmanager
def bind_progress(feed: ProgressFeed) -> Iterator[None]:
    """Bind the HTTP adapter's updates to the application-owned invocation task."""
    token = _FEED.set(feed)
    try:
        yield
    finally:
        _FEED.reset(token)


def publish_draft(call: int, text: str, *, reset: bool) -> None:
    """Offer draft progress without coupling the transport to a browser framework."""
    feed = _FEED.get()
    if feed is not None:
        feed.publish(call, text, reset=reset)


async def wait_with_progress(
    task: asyncio.Task[object], feed: ProgressFeed, callback: ProgressCallback
) -> None:
    """Render at most twenty snapshots a second; completion never waits on a token queue."""
    with feed.listen() as queue:
        while not task.done():
            try:
                draft = await asyncio.wait_for(queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            async with asyncio.timeout(2):
                await callback(draft)
            await asyncio.sleep(0.05)
