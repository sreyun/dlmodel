import asyncio
from pathlib import Path

import pytest
import respx
from httpx import Response

import app.queue as queue_mod
from app.db import get_task, init_db, set_setting
from app.downloaders.base import DownloadPaused
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
async def test_stop_parks_queued_and_running(tmp_path, monkeypatch):
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
    assert running["status"] == "queued"
    assert queued["status"] == "queued"
    assert "关闭" in running["message"] or "继续" in running["message"]
    assert "关闭" in queued["message"] or "继续" in queued["message"]


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
    # Worker may claim immediately after retry; assert counters were cleared.
    seen_reset = False
    for _ in range(40):
        queued = await get_task(tid)
        if (
            queued["progress_bytes"] == 0
            and queued["total_bytes"] is None
            and queued["status"] in ("queued", "running")
        ):
            seen_reset = True
            break
        if queued["status"] == "completed":
            # Finished before we sampled; still OK if second attempt ran.
            seen_reset = fail_once["n"] >= 2
            break
        await asyncio.sleep(0.02)
    assert seen_reset
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
async def test_stop_parks_active_tasks_for_restart(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    gate = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(10, 100, 1.0)
        await gate.wait()

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    await _wait_status(tid, "running")
    await q.stop()
    from app.db import get_task

    parked = await get_task(tid)
    assert parked is not None
    assert parked["status"] == "queued"
    assert "关闭" in parked["message"] or "继续" in parked["message"]

    resumed = asyncio.Event()

    async def fake_run2(task, on_progress, on_log, cancelled):
        resumed.set()

    q2 = DownloadQueue(concurrency=1)
    q2._run_download = fake_run2  # type: ignore
    await q2.start()
    await asyncio.wait_for(resumed.wait(), timeout=2)
    row = await _wait_status(tid, "completed", "failed")
    await q2.stop()
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


@pytest.mark.asyncio
@respx.mock
async def test_completed_task_schedules_notify_webhook(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.setenv(
        "NOTIFY_DINGTALK_WEBHOOK",
        "https://oapi.dingtalk.com/robot/send?access_token=abc",
    )
    monkeypatch.setenv("NOTIFY_ON_COMPLETED", "1")
    route = respx.post("https://oapi.dingtalk.com/robot/send").mock(
        return_value=Response(200, json={"errcode": 0})
    )

    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(1, 1, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    row = await _wait_status(tid, "completed", "failed")
    assert row["status"] == "completed"
    for _ in range(50):
        if route.called:
            break
        await asyncio.sleep(0.05)
    await q.stop()
    assert route.called


@pytest.mark.asyncio
@respx.mock
async def test_started_notify_waits_for_progress_stats(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.setenv(
        "NOTIFY_FEISHU_WEBHOOK",
        "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
    )
    monkeypatch.setenv("NOTIFY_ON_STARTED", "1")
    monkeypatch.setenv("NOTIFY_ON_COMPLETED", "0")
    route = respx.post("https://open.feishu.cn/open-apis/bot/v2/hook/abc").mock(
        return_value=Response(200, json={"code": 0})
    )
    gate = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        await asyncio.sleep(0.05)
        assert not route.called
        await on_progress(5 * 1024 * 1024, 100 * 1024 * 1024, 2 * 1024 * 1024)
        await gate.wait()

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    for _ in range(50):
        if route.called:
            break
        await asyncio.sleep(0.05)
    assert route.called
    body = route.calls[0].request.content.decode("utf-8", errors="ignore")
    assert "开始下载" in body or "\\u5f00\\u59cb\\u4e0b\\u8f7d" in body or "速率" in body
    gate.set()
    await _wait_status(tid, "completed", "failed", "cancelled")
    await q.stop()


@pytest.mark.asyncio
async def test_resize_down_then_up_restores_concurrency(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))

    def alive():
        return sum(1 for w in q._workers if not w.done())

    q = DownloadQueue(concurrency=2)
    await q.start()
    assert alive() == 2

    await q.resize(1)
    for _ in range(60):  # surplus worker exits on its next loop check (<=1s)
        if alive() == 1:
            break
        await asyncio.sleep(0.05)
    assert alive() == 1

    # Regression: with monotonic worker ids the freshly spawned workers would be
    # numbered past concurrency and exit immediately, so this never reached 3.
    await q.resize(3)
    for _ in range(60):
        if alive() == 3:
            break
        await asyncio.sleep(0.05)
    assert alive() == 3
    await q.stop()


@pytest.mark.asyncio
async def test_progress_throttle_reduces_db_writes(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    import app.queue as queue_mod

    writes = {"n": 0}
    real_update = queue_mod.update_active_task

    async def counting_update(task_id, **fields):
        if "progress_bytes" in fields:
            writes["n"] += 1
        return await real_update(task_id, **fields)

    monkeypatch.setattr(queue_mod, "update_active_task", counting_update)

    async def fake_run(task, on_progress, on_log, cancelled):
        # 40 tiny (<1 MB), fast increments with a live speed: only the first and
        # the completing update should be persisted, not all 41.
        for i in range(1, 41):
            await on_progress(i * 1000, 100_000, 1_000_000)
        await on_progress(100_000, 100_000, 1_000_000)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    row = await _wait_status(tid, "completed", "failed")
    await q.stop()
    assert row["status"] == "completed"
    assert writes["n"] <= 10  # unthrottled this would be ~41


@pytest.mark.asyncio
async def test_terminal_task_clears_inmemory_bookkeeping(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))

    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(100, 100, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    assert tid in q._cancel_flags
    await _wait_status(tid, "completed", "failed")
    for _ in range(40):
        if tid not in q._cancel_flags and tid not in q._task_gids:
            break
        await asyncio.sleep(0.05)
    await q.stop()
    assert tid not in q._cancel_flags
    assert tid not in q._task_gids


@pytest.mark.asyncio
async def test_pause_running_preserves_progress_and_paused(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    started = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(500, 1000, 10)
        started.set()
        while True:
            await asyncio.sleep(0.02)
            await on_progress(500, 1000, 10)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    q._task_gids[tid] = ["gid-xyz"]
    await q.pause(tid)
    row = await _wait_status(tid, "paused")
    assert row["status"] == "paused"
    assert row["progress_bytes"] == 500  # progress must survive a pause
    assert row["total_bytes"] == 1000
    assert "暂停" in row["message"]
    # Paused releases in-memory tracking (flags + aria2 gids) without a worker leak.
    for _ in range(40):
        if (
            tid not in q._cancel_flags
            and tid not in q._pause_flags
            and tid not in q._task_gids
        ):
            break
        await asyncio.sleep(0.05)
    assert tid not in q._cancel_flags
    assert tid not in q._pause_flags
    assert tid not in q._task_gids
    await q.stop()


@pytest.mark.asyncio
async def test_pause_then_cancel(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    started = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(300, 1000, 10)
        started.set()
        while True:
            await asyncio.sleep(0.02)
            await on_progress(300, 1000, 10)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    await q.pause(tid)
    assert (await _wait_status(tid, "paused"))["status"] == "paused"
    await q.cancel(tid)
    row = await _wait_status(tid, "cancelled")
    assert row["status"] == "cancelled"
    await q.stop()


@pytest.mark.asyncio
async def test_pause_queued_then_resume_completes(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    block_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        if task["name"] == "block/er":
            block_started.set()
            await release.wait()
            await on_progress(1, 1, 0)
            return
        await on_progress(100, 100, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    await q.enqueue(
        DownloadCreate(name="block/er", source="huggingface", target="vllm")
    )
    await asyncio.wait_for(block_started.wait(), timeout=2)
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    await _wait_status(tid, "queued")
    await q.pause(tid)
    assert (await get_task(tid))["status"] == "paused"
    await q.resume(tid)
    row = await _wait_status(tid, "queued", "running")
    assert row["status"] in ("queued", "running")
    release.set()
    final = await _wait_status(tid, "completed", "failed")
    assert final["status"] == "completed"
    await q.stop()


@pytest.mark.asyncio
async def test_resume_from_paused_requeues_and_runs(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    from app.db import insert_task

    ran = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        ran.set()
        await on_progress(100, 100, 0)

    dest = str(tmp_path / "models" / "hf" / "org" / "m")
    tid = await insert_task(
        {
            "name": "org/m",
            "source": "huggingface",
            "target": "vllm",
            "status": "paused",
            "dest_path": dest,
            "message": "已暂停",
        }
    )
    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    await asyncio.sleep(0.15)
    assert not ran.is_set()  # start must not auto-run a paused row
    await q.resume(tid)
    await asyncio.wait_for(ran.wait(), timeout=2)
    row = await _wait_status(tid, "completed", "failed")
    assert row["status"] == "completed"
    await q.stop()


@pytest.mark.asyncio
async def test_restart_and_resize_never_claim_paused(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    from app.db import insert_task, update_task

    ran = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        ran.set()
        await on_progress(100, 100, 0)

    dest = str(tmp_path / "models" / "hf" / "org" / "m")
    tid = await insert_task(
        {
            "name": "org/m",
            "source": "huggingface",
            "target": "vllm",
            "status": "paused",
            "dest_path": dest,
            "message": "已暂停",
        }
    )
    await update_task(tid, progress_bytes=50)
    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    await q.resize(3)
    await asyncio.sleep(0.2)
    assert not ran.is_set()
    row = await get_task(tid)
    assert row["status"] == "paused"
    assert row["progress_bytes"] == 50
    await q.stop()
    # Graceful stop parks queued/running only; paused stays paused.
    assert (await get_task(tid))["status"] == "paused"


@pytest.mark.asyncio
async def test_pause_and_resume_guard_invalid_states(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))

    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(100, 100, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    row = await _wait_status(tid, "completed", "failed")
    assert row["status"] == "completed"
    with pytest.raises(ValueError):
        await q.pause(tid)
    with pytest.raises(ValueError):
        await q.resume(tid)
    await q.stop()


# ---- 目标目录占用（_busy_dests）误报回归 ----


async def _start_ms_queue(tmp_path, monkeypatch, ms_impl):
    """Boot a queue with the real _run_download but a faked ModelScope IO layer."""
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))

    async def fake_exists(name, token):
        return True

    monkeypatch.setattr(queue_mod, "download_modelscope", ms_impl)
    monkeypatch.setattr(queue_mod, "ms_repo_exists", fake_exists)
    q = DownloadQueue(concurrency=1)
    await q.start()
    return q


@pytest.mark.asyncio
async def test_modelscope_pause_before_thread_releases_claim(tmp_path, monkeypatch):
    """场景1：线程启动前落地的暂停必须释放占用，resume 不得被「请稍后再试」拦下。"""
    calls = {"n": 0}

    async def fake_ms(name, dest, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            # 新契约：无活动线程时以 on_detached(False) 立即释放再传播控制异常。
            kw["on_detached"](False)
            raise DownloadPaused()

    q = await _start_ms_queue(tmp_path, monkeypatch, fake_ms)
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="modelscope", target="vllm")
    )
    row = await _wait_status(tid, "paused", "failed")
    assert row["status"] == "paused"
    key = q._dest_key(row["dest_path"])
    assert key not in q._busy_dests
    await q.resume(tid)  # 不再误报：立即成功
    row = await _wait_status(tid, "completed")
    assert row["status"] == "completed"
    assert calls["n"] == 2
    assert key not in q._busy_dests
    await q.stop()


@pytest.mark.asyncio
async def test_stale_busy_claim_reclaimed_by_release_task(tmp_path, monkeypatch):
    """下载器泄漏（未回调 on_detached）时，终态/暂停清理必须兜底回收陈旧占用。"""
    calls = {"n": 0}

    async def leaking_ms(name, dest, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise DownloadPaused()  # 模拟旧版泄漏：不调用 on_detached

    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))

    async def fake_exists(name, token):
        return True

    monkeypatch.setattr(queue_mod, "download_modelscope", leaking_ms)
    monkeypatch.setattr(queue_mod, "ms_repo_exists", fake_exists)
    q = DownloadQueue(concurrency=1)
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="modelscope", target="vllm")
    )
    row = await _wait_status(tid, "paused")
    assert row["status"] == "paused"
    # _release_task 兜底：paused 落库后不存在无主占用。
    assert q._busy_dests == set()
    await q.resume(tid)  # 误报不再持久化
    row = await _wait_status(tid, "completed")
    assert row["status"] == "completed"
    await q.stop()


@pytest.mark.asyncio
async def test_detached_modelscope_thread_blocks_resume_until_done(tmp_path, monkeypatch):
    """真实双写风险保留：线程未结束时 resume 拒绝；线程结束后自动放行。"""
    captured: dict = {}
    calls = {"n": 0}

    async def detaching_ms(name, dest, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            return
        captured["release"] = kw["on_detached"]
        captured["release"](True)  # 后台线程仍在写
        raise DownloadPaused()

    q = await _start_ms_queue(tmp_path, monkeypatch, detaching_ms)
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="modelscope", target="vllm")
    )
    row = await _wait_status(tid, "paused")
    key = q._dest_key(row["dest_path"])
    assert key in q._busy_dests  # 仍在写的线程必须保留占位
    with pytest.raises(ValueError):
        await q.resume(tid)
    assert (await get_task(tid))["status"] == "paused"
    captured["release"](False)  # 模拟后台线程真正结束
    await q.resume(tid)
    row = await _wait_status(tid, "completed")
    assert row["status"] == "completed"
    assert key not in q._busy_dests
    await q.stop()


@pytest.mark.asyncio
async def test_ms_cancel_retry_and_same_name_requeue_no_false_positive(
    tmp_path, monkeypatch
):
    """场景2/3：失败 → 重试、终态后同名新建，占用均已对称释放。"""
    calls = {"n": 0}

    async def flaky_ms(name, dest, **kw):
        calls["n"] += 1
        kw["on_detached"](False)
        if calls["n"] == 1:
            raise RuntimeError("boom")

    q = await _start_ms_queue(tmp_path, monkeypatch, flaky_ms)
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="modelscope", target="vllm")
    )
    row = await _wait_status(tid, "failed")
    assert row["status"] == "failed"
    await q.retry(tid)  # 不得报「目标目录仍有未结束的下载」
    row = await _wait_status(tid, "completed", "failed")
    assert row["status"] == "completed"
    # 终态后同名新建同样放行（find_active 已终态、busy 已回收）。
    second = await q.enqueue(
        DownloadCreate(name="org/m", source="modelscope", target="vllm")
    )
    assert second != tid
    await q.stop()


@pytest.mark.asyncio
async def test_resume_waits_out_brief_self_claim_after_pause(tmp_path, monkeypatch):
    """pause 落库与 worker 释放之间的短暂 self-hold：resume 等待而非误拒。"""
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    block_started = asyncio.Event()
    release = asyncio.Event()

    async def fake_run(task, on_progress, on_log, cancelled):
        if task["name"] == "block/er":
            block_started.set()
            await release.wait()
        await on_progress(100, 100, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    await q.enqueue(DownloadCreate(name="block/er", source="huggingface", target="vllm"))
    await asyncio.wait_for(block_started.wait(), timeout=2)
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    await _wait_status(tid, "queued")
    await q.pause(tid)
    row = await get_task(tid)
    # 模拟 worker 尚未跑到下一个 tick 的自持有窗口（holder 就是本任务）。
    key = q._dest_key(row["dest_path"])
    q._busy_dests.add(key)
    q._dest_holders[key] = tid

    async def release_soon():
        await asyncio.sleep(0.15)
        q._release_dest(key, tid)

    waiter = asyncio.create_task(release_soon())
    await q.resume(tid)  # 应短暂等待后放行，而不是抛 ValueError
    await waiter
    assert (await get_task(tid))["status"] == "queued"
    release.set()
    await asyncio.sleep(0.1)
    await q.stop()


@pytest.mark.asyncio
async def test_stop_clears_dest_claims(tmp_path, monkeypatch):
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))

    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(1, 1, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    q._busy_dests.add("/models/hf/x/y")
    q._dest_holders["/models/hf/x/y"] = "ghost"
    q._detached_dests.add("/models/hf/x/y")
    await q.stop()
    assert q._busy_dests == set()
    assert q._dest_holders == {}
    assert q._detached_dests == set()


@pytest.mark.asyncio
async def test_cancel_then_retry_releases_dest_once_thread_drains(tmp_path, monkeypatch):
    """场景2：取消 → 重试。收尾线程排空前保留真风险，排空后重试必须成功。"""
    from app.downloaders.base import DownloadCancelled

    calls = {"n": 0}
    captured: dict = {}

    async def cancelling_ms(name, dest, **kw):
        calls["n"] += 1
        if calls["n"] > 1:
            return
        release = kw["on_detached"]
        try:
            while True:
                await kw["on_progress"](1024 * calls["n"], None, None)
                await asyncio.sleep(0.05)
        except DownloadCancelled:
            release(True)  # 不可中断的线程仍在收尾
            # 由测试决定线程何时排空，不用定时器（全量回归下会抢不过真实耗时）
            captured["drained"] = lambda: release(False)
            raise

    q = await _start_ms_queue(tmp_path, monkeypatch, cancelling_ms)
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="modelscope", target="vllm")
    )
    await _wait_status(tid, "running")
    await q.cancel(tid)
    row = await _wait_status(tid, "cancelled")
    assert row["status"] == "cancelled"
    key = q._dest_key(row["dest_path"])
    assert key in q._busy_dests  # 线程仍在写：占位必须保留
    with pytest.raises(ValueError):
        await q.retry(tid)  # 收尾未结束时仍应拒绝（真风险，不是误报）
    captured["drained"]()  # 线程排空
    for _ in range(60):
        if key not in q._busy_dests:
            break
        await asyncio.sleep(0.05)
    assert key not in q._busy_dests  # 排空后自动释放，不会长期卡住
    await q.retry(tid)  # 不得再报「目标目录仍有未结束的下载」
    row = await _wait_status(tid, "completed", "failed")
    assert row["status"] == "completed", row["message"]
    await q.stop()


@pytest.mark.asyncio
async def test_resize_preserves_detached_dest_claim(tmp_path, monkeypatch):
    """并发热调不得误回收占位，也不得让已释放的占位复活。"""
    captured: dict = {}

    async def detaching_ms(name, dest, **kw):
        captured["release"] = kw["on_detached"]
        captured["release"](True)
        raise DownloadPaused()

    q = await _start_ms_queue(tmp_path, monkeypatch, detaching_ms)
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="modelscope", target="vllm")
    )
    row = await _wait_status(tid, "paused")
    key = q._dest_key(row["dest_path"])

    await q.resize(3)
    assert key in q._busy_dests and key in q._detached_dests
    with pytest.raises(ValueError):
        await q.resume(tid)

    captured["release"](False)
    await q.resize(1)
    assert key not in q._busy_dests
    await q.resume(tid)
    assert (await get_task(tid))["status"] in ("queued", "running", "paused")
    await q.stop()


@pytest.mark.asyncio
async def test_restart_recovery_leaves_no_stale_dest_claim(tmp_path, monkeypatch):
    """占位仅存于内存：重启停放重排队的任务与同名新建都不被误拒。"""
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    from app.db import insert_task

    dest = str(tmp_path / "models" / "hf" / "org" / "m")
    tid = await insert_task(
        {
            "name": "org/m",
            "source": "huggingface",
            "target": "vllm",
            "status": "running",
            "dest_path": dest,
            "message": "stale",
        }
    )

    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(1, 1, 0)

    q = DownloadQueue(concurrency=2)  # 新实例 = 服务重启
    q._run_download = fake_run  # type: ignore
    await q.start()
    assert q._busy_dests == set()
    row = await _wait_status(tid, "completed")
    assert row["status"] == "completed"
    second = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    assert second != tid
    await q.stop()


GB = 1024**3
MB = 1024 * 1024


@pytest.mark.asyncio
async def test_dying_worker_cannot_cancel_a_requeued_row(tmp_path, monkeypatch):
    """worker 收尾的终态写入只能作用于自己认领的 running 行。"""
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    from app.db import insert_task, update_task

    dest = str(tmp_path / "models" / "hf" / "org" / "m")
    tid = await insert_task(
        {
            "name": "org/m",
            "source": "huggingface",
            "target": "vllm",
            "status": "queued",
            "dest_path": dest,
        }
    )
    q = DownloadQueue(concurrency=1)
    # 重试已把行重排队为 queued：迟到的旧 worker 不得把它写回 cancelled
    await q._finish_if_active(tid, status="cancelled", message="旧 worker 收尾")
    assert (await get_task(tid))["status"] == "queued"
    # 而真正由本 worker 认领的 running 行必须能正常落终态
    await update_task(tid, status="running")
    await q._finish_if_active(tid, status="cancelled", message="旧 worker 收尾")
    assert (await get_task(tid))["status"] == "cancelled"


@pytest.mark.asyncio
async def test_known_total_survives_polls_without_total(tmp_path, monkeypatch):
    """进度轮询丢失分母时，绝不能用 NULL 擦掉已知的总大小。"""
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    observed: dict = {}

    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(150 * GB, 200 * GB, 500 * MB)
        # 下一次轮询没带 total（aria2 HEAD 之前 / 镜像 listing 失败）
        await on_progress(150 * GB + 4 * MB, None, 500 * MB)
        observed["row"] = await get_task(task["id"])
        await on_progress(200 * GB, 200 * GB, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    row = await _wait_status(tid, "completed", "failed")
    await q.stop()
    assert row["status"] == "completed"
    assert observed["row"]["total_bytes"] == 200 * GB
    assert observed["row"]["progress_bytes"] == 150 * GB + 4 * MB


@pytest.mark.asyncio
async def test_resume_keeps_stored_progress_and_total_as_floor(tmp_path, monkeypatch):
    """断点续传：传输层从零重报时，进度条不得回退或丢掉分母。"""
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))
    from app.db import insert_task, update_task

    observed: dict = {}

    async def fake_run(task, on_progress, on_log, cancelled):
        # 传输层从 0 起步（重试语义），但库里已存 150G/200G
        await on_progress(2 * MB, None, 100 * MB)
        observed["row"] = await get_task(task["id"])
        await on_progress(200 * GB, 200 * GB, 0)

    dest = str(tmp_path / "models" / "hf" / "org" / "m")
    tid = await insert_task(
        {
            "name": "org/m",
            "source": "huggingface",
            "target": "vllm",
            "status": "paused",
            "dest_path": dest,
            "message": "已暂停",
        }
    )
    await update_task(tid, progress_bytes=150 * GB, total_bytes=200 * GB)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    await q.resume(tid)
    row = await _wait_status(tid, "completed", "failed")
    await q.stop()
    assert row["status"] == "completed"
    assert observed["row"]["progress_bytes"] == 150 * GB  # 未回退到 2MB
    assert observed["row"]["total_bytes"] == 200 * GB


@pytest.mark.asyncio
async def test_completed_row_reconciles_undercounted_total(tmp_path, monkeypatch):
    """源站 listing 偏小时，完成行必须收口成 100%，而不是永久停在 98%。"""
    await init_db(str(tmp_path / "app.db"))
    monkeypatch.setenv("MODEL_ROOT", str(tmp_path / "models"))

    async def fake_run(task, on_progress, on_log, cancelled):
        await on_progress(90, 80, 10)  # 字节数超过了宣称的总量
        await on_progress(100, 80, 0)

    q = DownloadQueue(concurrency=1)
    q._run_download = fake_run  # type: ignore
    await q.start()
    tid = await q.enqueue(
        DownloadCreate(name="org/m", source="huggingface", target="vllm")
    )
    row = await _wait_status(tid, "completed", "failed")
    await q.stop()
    assert row["status"] == "completed"
    assert row["progress_bytes"] == 100
    assert row["total_bytes"] == 100  # 分母被拉宽，绝不出现 >100%
