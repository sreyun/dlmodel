from typing import Awaitable, Callable

ProgressCallback = Callable[[int, int | None, float | None], Awaitable[None]]
LogCallback = Callable[[str], Awaitable[None]]
