"""Size/progress helpers shared by the downloaders.

Kept in one place because every transport has to answer the same two questions
the same way: "how many bytes are on disk" and "how do I show that to a human".
Divergent implementations are what made per-source progress bars disagree.
"""

from __future__ import annotations

from pathlib import Path


def format_bytes(n: int | float | None) -> str:
    """Human readable byte count (binary units), ``—`` for nothing usable."""
    if n is None:
        return "—"
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "—"
    if value != value:  # NaN
        return "—"
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    return f"{value:.1f} {units[i]}" if i else f"{int(value)} {units[i]}"


def dir_size_bytes(path: Path) -> int:
    """Sum of the file sizes under ``path``; unreadable entries are skipped.

    Used by transports that hand the actual transfer to a blocking thread (the
    ModelScope SDK) and can only observe the result through the filesystem, so
    it must never raise on a file that vanishes mid-scan.
    """
    path = Path(path)
    if not path.exists():
        return 0
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if not item.is_file():
                    continue
                total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total
