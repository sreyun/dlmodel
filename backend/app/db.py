from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite

_DB_PATH: str | None = None
_TASK_COLUMN_SET = frozenset(
    {
        "name",
        "source",
        "target",
        "revision",
        "status",
        "progress_bytes",
        "total_bytes",
        "speed_bps",
        "message",
        "dest_path",
        "updated_at",
    }
)

_TASK_COLUMNS = (
    "id",
    "name",
    "source",
    "target",
    "revision",
    "status",
    "progress_bytes",
    "total_bytes",
    "speed_bps",
    "message",
    "dest_path",
    "created_at",
    "updated_at",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: aiosqlite.Row) -> dict:
    data = dict(row)
    if data.get("speed_bps") is not None:
        data["speed_bps"] = float(data["speed_bps"])
    return data


def _validate_fields(fields: dict) -> dict:
    unknown = set(fields) - _TASK_COLUMN_SET
    if unknown:
        raise ValueError(f"非法任务字段：{', '.join(sorted(unknown))}")
    return fields


async def init_db(db_path: str) -> None:
    global _DB_PATH
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _DB_PATH = str(path)

    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                revision TEXT,
                status TEXT NOT NULL,
                progress_bytes INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER,
                speed_bps REAL,
                message TEXT NOT NULL DEFAULT '',
                dest_path TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at DESC)"
        )
        await db.commit()


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["value"] if row else None


async def set_setting(key: str, value: str) -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        await db.commit()


async def delete_setting(key: str) -> None:
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("DELETE FROM settings WHERE key = ?", (key,))
        await db.commit()


async def insert_task(task: dict) -> str:
    task_id = uuid4().hex
    now = _now_iso()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            """
            INSERT INTO tasks (
                id, name, source, target, revision, status,
                progress_bytes, total_bytes, speed_bps, message,
                dest_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                task["name"],
                task["source"],
                task["target"],
                task.get("revision"),
                task.get("status", "queued"),
                task.get("progress_bytes", 0),
                task.get("total_bytes"),
                task.get("speed_bps"),
                task.get("message", ""),
                task.get("dest_path", ""),
                now,
                now,
            ),
        )
        await db.commit()
    return task_id


async def update_task(task_id: str, **fields) -> None:
    if not fields:
        return

    fields = _validate_fields(dict(fields))
    fields["updated_at"] = _now_iso()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    values = list(fields.values()) + [task_id]

    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            values,
        )
        await db.commit()


async def update_active_task(task_id: str, **fields) -> bool:
    """Update only if the task is still queued/running. Returns whether a row changed."""
    if not fields:
        return False

    fields = _validate_fields(dict(fields))
    fields["updated_at"] = _now_iso()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    values = list(fields.values()) + [task_id]

    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ? AND status IN ('queued', 'running')",
            values,
        )
        await db.commit()
        return cursor.rowcount > 0


async def claim_task_for_retry(task_id: str, **fields) -> bool:
    """CAS: only requeue from failed/cancelled."""
    fields = _validate_fields(dict(fields))
    fields["updated_at"] = _now_iso()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    values = list(fields.values()) + [task_id]
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ? AND status IN ('failed', 'cancelled')",
            values,
        )
        await db.commit()
        return cursor.rowcount > 0


async def find_active_task(name: str, target: str) -> dict | None:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT {', '.join(_TASK_COLUMNS)} FROM tasks
            WHERE name = ? AND target = ? AND status IN ('queued', 'running')
            ORDER BY created_at DESC LIMIT 1
            """,
            (name, target),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None


async def list_tasks(*, limit: int = 200) -> list[dict]:
    limit = max(1, min(int(limit), 1000))
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def list_tasks_by_status(*statuses: str) -> list[dict]:
    if not statuses:
        return []
    placeholders = ", ".join("?" for _ in statuses)
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""
            SELECT {', '.join(_TASK_COLUMNS)} FROM tasks
            WHERE status IN ({placeholders})
            ORDER BY created_at ASC
            """,
            statuses,
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_dict(row) for row in rows]


async def get_task(task_id: str) -> dict | None:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks WHERE id = ?",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_dict(row) if row else None


async def delete_task(task_id: str, *, statuses: tuple[str, ...] | None = None) -> bool:
    """Delete a task row. If statuses given, only delete when status matches."""
    async with aiosqlite.connect(_DB_PATH) as db:
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            cursor = await db.execute(
                f"DELETE FROM tasks WHERE id = ? AND status IN ({placeholders})",
                (task_id, *statuses),
            )
        else:
            cursor = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        await db.commit()
        return cursor.rowcount > 0


async def delete_tasks_by_status(*statuses: str) -> int:
    if not statuses:
        return 0
    placeholders = ", ".join("?" for _ in statuses)
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute(
            f"DELETE FROM tasks WHERE status IN ({placeholders})",
            statuses,
        )
        await db.commit()
        return cursor.rowcount
