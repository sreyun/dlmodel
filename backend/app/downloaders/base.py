from typing import Awaitable, Callable

ProgressCallback = Callable[[int, int | None, float | None], Awaitable[None]]
LogCallback = Callable[[str], Awaitable[None]]


class DownloadControl(Exception):
    """Base for cooperative control-flow signals raised from progress/log hooks.

    These are *not* download errors: they must never be retried, classified as
    transient, or swallowed by a per-source fallback. Catching ``DownloadControl``
    lets every transport abort cleanly while leaving on-disk partial files intact.
    """


class DownloadCancelled(DownloadControl):
    """Raised when the user cancels an in-flight download."""


class DownloadPaused(DownloadControl):
    """Raised when the user pauses an in-flight download (resumable, non-terminal)."""


class DownloadRemoved(DownloadControl):
    """aria2 download was force-removed (typically user cancel)."""
