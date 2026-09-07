import asyncio
from pathlib import Path

import pytest

from app.db import get_task, init_db, set_setting
from app.models_schema import DownloadCreate
from app.paths import hf_model_dir
from app.queue import DownloadQueue


async def _wait_status(task_id: str, *statuses: str, attempts: int = 50):
    row = None
    for _ in range(attempts):
        row = await get_task(task_id)
        if row and row["status"] in statuses:
            return row
        await asyncio.sleep(0.05)
    return row


@pytest.mark.asyncio
async def test_enqueue_runs_to_completion(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    q = DownloadQueue(concurrency=1)
    # inject fake runner
    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(50, 100, 1.0)
        dest = Path(task["dest_path"])
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "ok").write_text("1")
        await on_progress(100, 100, 0)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(DownloadCreate(name="org/m", source="huggingface", target="vllm"))
    # wait until completed
    for _ in range(50):
        row = await get_task(tid)
        if row["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)
    await q.stop()
    row = await get_task(tid)
    assert row["status"] == "completed"
    expected = hf_model_dir(str(tmp_path / "models"), "org/m")
    assert row["dest_path"] == str(expected)
    assert (expected / "ok").read_text() == "1"


@pytest.mark.asyncio
async def test_cancel_queued_task(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        started.set()
        await release.wait()
        await on_progress(100, 100, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    first = await q.enqueue(DownloadCreate(name="org/a", source="huggingface", target="vllm"))
    await started.wait()
    second = await q.enqueue(DownloadCreate(name="org/b", source="huggingface", target="vllm"))
    await q.cancel(second)
    release.set()
    await _wait_status(first, "completed", "failed")
    await q.stop()
    row = await get_task(second)
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_retry_requeues_failed(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    attempts = {"n": 0}

    async def fake_run(task, on_progress, on_log, cancelled):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("boom")
        dest = Path(task["dest_path"])
        dest.mkdir(parents=True, exist_ok=True)
        await on_progress(100, 100, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(DownloadCreate(name="org/m", source="huggingface", target="vllm"))
    row = await _wait_status(tid, "failed")
    assert row["status"] == "failed"
    await q.retry(tid)
    row = await _wait_status(tid, "completed", "failed")
    await q.stop()
    assert row["status"] == "completed"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_subscribe_receives_progress(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    gate = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        await gate.wait()
        await on_progress(50, 100, 1.0)
        dest = Path(task["dest_path"])
        dest.mkdir(parents=True, exist_ok=True)
        await on_progress(100, 100, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(DownloadCreate(name="org/m", source="huggingface", target="vllm"))
    sub = q.subscribe(tid)
    gate.set()
    events = []
    while True:
        events.append(await asyncio.wait_for(sub.get(), timeout=2))
        if events[-1]["status"] in ("completed", "failed"):
            break
    await q.stop()
    assert any(e["progress_bytes"] == 50 and e["total_bytes"] == 100 for e in events)
    assert events[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_unknown_model_fails(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))

    async def missing(*_args, **_kwargs):
        return False

    monkeypatch.setattr("app.queue.hf_repo_exists", missing)
    monkeypatch.setattr("app.queue.ms_repo_exists", missing)

    q = DownloadQueue(concurrency=1)
    await q.start()
    tid = await q.enqueue(DownloadCreate(name="org/missing", source="auto", target="vllm"))
    row = await _wait_status(tid, "failed", "completed")
    await q.stop()
    assert row["status"] == "failed"
    assert "未找到模型" in row["message"]


@pytest.mark.asyncio
async def test_run_download_uses_effective_settings(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.setenv("HF_ENDPOINT", "https://env-default.example")
    await set_setting("hf_endpoint", "https://sqlite-override.example")
    await set_setting("hf_token", "sqlite-token")
    captured = {}

    async def fake_hf_exists(name, endpoint, token):
        captured["endpoint"] = endpoint
        captured["token"] = token
        return False

    async def missing(*_args, **_kwargs):
        return False

    monkeypatch.setattr("app.queue.hf_repo_exists", fake_hf_exists)
    monkeypatch.setattr("app.queue.ms_repo_exists", missing)

    q = DownloadQueue(concurrency=1)
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    await _wait_status(tid, "failed", "completed")
    await q.stop()
    assert captured["endpoint"] == "https://sqlite-override.example"
    assert captured["token"] == "sqlite-token"


@pytest.mark.asyncio
async def test_cancel_running_updates_status_promptly(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    started = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        started.set()
        await cancelled.wait()
        # Stay in-flight so cancel() itself must write DB status.
        await asyncio.Event().wait()

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(DownloadCreate(name="org/m", source="huggingface", target="vllm"))
    await started.wait()
    assert (await get_task(tid))["status"] == "running"
    await q.cancel(tid)
    row = await get_task(tid)
    assert row["status"] == "cancelled"
    await _wait_status(tid, "cancelled")
    await q.stop()
    assert (await get_task(tid))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_stop_marks_queued_and_running(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    started = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        started.set()
        await cancelled.wait()

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    running_id = await q.enqueue(
        DownloadCreate(name="org/a", source="huggingface", target="vllm")
    )
    await started.wait()
    queued_id = await q.enqueue(
        DownloadCreate(name="org/b", source="huggingface", target="vllm")
    )
    await q.stop()
    running = await get_task(running_id)
    queued = await get_task(queued_id)
    assert running["status"] in ("cancelled", "failed")
    assert queued["status"] in ("cancelled", "failed")
    assert "队列已停止" in running["message"]
    assert "队列已停止" in queued["message"]


@pytest.mark.asyncio
async def test_subscribe_pruned_after_terminal(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))

    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(100, 100, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(DownloadCreate(name="org/m", source="huggingface", target="vllm"))
    sub = q.subscribe(tid)
    await _wait_status(tid, "completed", "failed")
    q.unsubscribe(tid, sub)
    await q.stop()
    assert tid not in q._subscribers


@pytest.mark.asyncio
async def test_unsubscribe_before_terminal(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    gate = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        await gate.wait()
        await on_progress(100, 100, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(DownloadCreate(name="org/m", source="huggingface", target="vllm"))
    sub = q.subscribe(tid)
    q.unsubscribe(tid, sub)
    assert sub not in q._subscribers.get(tid, [])
    gate.set()
    await _wait_status(tid, "completed", "failed")
    await q.stop()


@pytest.mark.asyncio
async def test_user_cancel_during_progress_keeps_worker_alive(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    started = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        started.set()
        await cancelled.wait()
        await on_progress(10, 100, 1.0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    first = await q.enqueue(DownloadCreate(name="org/a", source="huggingface", target="vllm"))
    await started.wait()
    await q.cancel(first)
    await _wait_status(first, "cancelled")
    await asyncio.sleep(0.1)
    assert any(not w.done() for w in q._workers)

    second_started = asyncio.Event()

    async def fake_run2(task, on_progress, on_log, cancelled):
        second_started.set()
        await on_progress(100, 100, 0)

    q._run_download = fake_run2  # type: ignore
    second = await q.enqueue(DownloadCreate(name="org/b", source="huggingface", target="vllm"))
    await asyncio.wait_for(second_started.wait(), timeout=2)
    row = await _wait_status(second, "completed", "failed")
    await q.stop()
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_retry_clears_total_bytes(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    fail_once = {"n": 0}

    async def fake_run(task, on_progress, on_log, cancelled):
        fail_once["n"] += 1
        await on_progress(50, 200, 1.0)
        if fail_once["n"] == 1:
            raise RuntimeError("boom")
        await on_progress(200, 200, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(DownloadCreate(name="org/m", source="huggingface", target="vllm"))
    await _wait_status(tid, "failed")
    failed = await get_task(tid)
    assert failed["total_bytes"] == 200
    await q.retry(tid)
    queued = await get_task(tid)
    assert queued["status"] == "queued"
    assert queued["progress_bytes"] == 0
    assert queued["total_bytes"] is None
    await _wait_status(tid, "completed", "failed")
    await q.stop()


@pytest.mark.asyncio
async def test_cancel_removed_is_not_failed(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    started = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        started.set()
        await cancelled.wait()
        from app.downloaders.hf import DownloadRemoved

        raise DownloadRemoved("aria2 下载已被移除")

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(DownloadCreate(name="org/m", source="huggingface", target="vllm"))
    await started.wait()
    await q.cancel(tid)
    row = await _wait_status(tid, "cancelled", "failed")
    await q.stop()
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_recover_pending_on_start(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    from app.db import insert_task, update_task

    tid = await insert_task(
        {
            "name": "org/m",
            "source": "huggingface",
            "target": "vllm",
            "status": "running",
            "dest_path": str(tmp_path / "models" / "hf" / "org" / "m"),
            "message": "stale",
        }
    )
    ran = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        ran.set()
        await on_progress(1, 1, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    await asyncio.wait_for(ran.wait(), timeout=2)
    row = await _wait_status(tid, "completed", "failed")
    await q.stop()
    assert row["status"] == "completed"


@pytest.mark.asyncio
async def test_enqueue_rejects_incompatible_source_target(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    q = DownloadQueue(concurrency=1)
    await q.start()
    with pytest.raises(ValueError, match="Ollama 目标"):
        await q.enqueue(
            DownloadCreate(name="org/m", source="huggingface", target="ollama")
        )
    with pytest.raises(ValueError, match="vLLM 目标"):
        await q.enqueue(DownloadCreate(name="llama3.2", source="ollama", target="vllm"))
    await q.stop()


@pytest.mark.asyncio
async def test_enqueue_rejects_duplicate_active(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    gate = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        await gate.wait()

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    await q.enqueue(DownloadCreate(name="org/m", source="huggingface", target="vllm"))
    await asyncio.sleep(0.05)
    with pytest.raises(ValueError, match="进行中"):
        await q.enqueue(DownloadCreate(name="org/m", source="huggingface", target="vllm"))
    gate.set()
    await q.stop()
