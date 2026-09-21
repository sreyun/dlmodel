import pytest

from app.db import (
    find_active_task,
    get_many_settings,
    get_setting,
    get_task,
    init_db,
    insert_task,
    set_setting,
    update_task,
    update_task_if_in,
)


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "app.db")
    await init_db(path)
    return path


@pytest.mark.asyncio
async def test_settings_roundtrip(db):
    await set_setting("hf_endpoint", "https://hf-mirror.com")
    assert await get_setting("hf_endpoint") == "https://hf-mirror.com"


@pytest.mark.asyncio
async def test_get_many_settings_batch(db):
    await set_setting("hf_endpoint", "https://hf-mirror.com")
    await set_setting("download_concurrency", "4")
    got = await get_many_settings(
        ["hf_endpoint", "download_concurrency", "hf_token", "missing_key"]
    )
    # Existing keys return values; absent keys are omitted (caller maps to None).
    assert got == {"hf_endpoint": "https://hf-mirror.com", "download_concurrency": "4"}
    assert "hf_token" not in got
    assert "missing_key" not in got


@pytest.mark.asyncio
async def test_get_many_settings_empty(db):
    assert await get_many_settings([]) == {}


@pytest.mark.asyncio
async def test_task_insert_and_update(db):
    tid = await insert_task(
        {
            "name": "Qwen/Qwen2.5-0.5B",
            "source": "auto",
            "target": "vllm",
            "revision": None,
            "status": "queued",
            "dest_path": "/models/hf/Qwen/Qwen2.5-0.5B",
        }
    )
    await update_task(tid, status="running", progress_bytes=10, total_bytes=100)
    row = await get_task(tid)
    assert row["status"] == "running"
    assert row["progress_bytes"] == 10


async def _insert(db, status):
    return await insert_task(
        {
            "name": "org/m",
            "source": "huggingface",
            "target": "vllm",
            "revision": None,
            "status": status,
            "dest_path": "/models/hf/org/m",
        }
    )


@pytest.mark.asyncio
async def test_update_task_if_in_allows_matching_transition(db):
    tid = await _insert(db, "paused")
    changed = await update_task_if_in(
        tid, ("paused",), status="queued", speed_bps=None, message="resuming"
    )
    assert changed is True
    row = await get_task(tid)
    assert row["status"] == "queued"
    assert row["message"] == "resuming"


@pytest.mark.asyncio
async def test_update_task_if_in_rejects_mismatched_status(db):
    tid = await _insert(db, "completed")
    changed = await update_task_if_in(
        tid, ("queued", "running", "paused"), status="cancelled", message="x"
    )
    assert changed is False
    row = await get_task(tid)
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_update_task_if_in_empty_expected_is_noop(db):
    tid = await _insert(db, "queued")
    assert await update_task_if_in(tid, (), status="paused") is False
    assert (await get_task(tid))["status"] == "queued"


@pytest.mark.asyncio
async def test_find_active_task_includes_paused(db):
    tid = await _insert(db, "paused")
    active = await find_active_task("org/m", "vllm")
    assert active is not None
    assert active["id"] == tid
    assert active["status"] == "paused"
