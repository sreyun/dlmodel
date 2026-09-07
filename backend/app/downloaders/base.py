from typing import Awaitable, Callable

ProgressCallback = Callable[[int, int | None, float | None], Awaitable[None]]
