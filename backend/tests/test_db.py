import pytest

from app.db import (
    get_many_settings,
    get_setting,
    get_task,
    init_db,
    insert_task,
    set_setting,
    update_task,
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
