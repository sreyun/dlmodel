from datetime import datetime
from uuid import uuid4

import aiosqlite

_DB_PATH: str | None = None

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
    return datetime.utcnow().isoformat()


def _row_to_dict(row: aiosqlite.Row) -> dict:
    data = dict(row)
    if data.get("speed_bps") is not None:
        data["speed_bps"] = float(data["speed_bps"])
    return data


async def init_db(db_path: str) -> None:
    global _DB_PATH
    _DB_PATH = db_path

    async with aiosqlite.connect(db_path) as db:
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

    fields = dict(fields)
    fields["updated_at"] = _now_iso()
    assignments = ", ".join(f"{column} = ?" for column in fields)
    values = list(fields.values()) + [task_id]

    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            values,
        )
        await db.commit()


async def list_tasks() -> list[dict]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT {', '.join(_TASK_COLUMNS)} FROM tasks ORDER BY created_at DESC"
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
