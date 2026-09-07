import asyncio
from pathlib import Path

from app.aria2_client import Aria2Client
from app.config import get_settings
from app.db import (
    claim_task_for_retry,
    find_active_task,
    get_task,
    insert_task,
    list_tasks,
    list_tasks_by_status,
    update_active_task,
    update_task,
)
from app.downloaders import (
    download_hf,
    download_modelscope,
    download_ollama,
    hf_repo_exists,
    ms_repo_exists,
)
from app.downloaders.hf import DownloadRemoved
from app.models_schema import DownloadCreate
from app.paths import ensure_under_model_root, hf_model_dir, ollama_root, parse_model_name
from app.routes.settings import effective_settings
from app.source_resolve import resolve_source

_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_SOURCE_ZH = {
    "huggingface": "Hugging Face",
    "modelscope": "ModelScope",
    "ollama": "Ollama",
}


class DownloadCancelled(Exception):
    """Raised when the user cancels an in-flight download."""


class _GidTrackingAria2:
    """Records aria2 gids so cancel can force-remove in-flight downloads."""

    def __init__(self, client: Aria2Client, gids: list[str]):
        self._client = client
        self._gids = gids

    async def add_uri(self, *args, **kwargs):
        gid = await self._client.add_uri(*args, **kwargs)
        self._gids.append(gid)
        return gid

    def __getattr__(self, name):
        return getattr(self._client, name)


def _validate_pair(source: str, target: str) -> None:
    if target == "ollama" and source not in ("auto", "ollama"):
        raise ValueError("Ollama 目标仅支持「自动」或「Ollama」下载源")
    if target == "vllm" and source == "ollama":
        raise ValueError("vLLM 目标不支持 Ollama 下载源，请改用 Hugging Face / ModelScope")


class DownloadQueue:
    def __init__(self, concurrency: int | None = None) -> None:
        settings = get_settings()
        self.concurrency = max(
            1,
            concurrency if concurrency is not None else settings.download_concurrency,
        )
        self._pending: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._next_worker_id = 0
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._task_gids: dict[str, list[str]] = {}
        self._started = False
        self._aria2: Aria2Client | None = None
        self._stopping = False
        self._busy_dests: set[str] = set()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        settings = get_settings()
        self._aria2 = Aria2Client(
            settings.aria2_rpc_url, secret=settings.aria2_rpc_secret
        )
        for _ in range(self.concurrency):
            self._spawn_worker()
        await self._recover_pending_tasks()

    async def _recover_pending_tasks(self) -> None:
        rows = await list_tasks_by_status("queued", "running")
        for row in rows:
            task_id = row["id"]
            self._cancel_flags.setdefault(task_id, asyncio.Event())
            self._task_gids.setdefault(task_id, [])
            if row["status"] == "running":
                await update_task(
                    task_id,
                    status="queued",
                    message="服务重启后重新排队…",
                    speed_bps=None,
                )
            await self._pending.put(task_id)
            await self._publish(task_id)

    def _spawn_worker(self) -> None:
        worker_id = self._next_worker_id
        self._next_worker_id += 1
        self._workers.append(asyncio.create_task(self._worker(worker_id)))

    async def resize(self, concurrency: int) -> None:
        concurrency = max(1, int(concurrency))
        self.concurrency = concurrency
        self._workers = [w for w in self._workers if not w.done()]
        while self._started and len(self._workers) < concurrency:
            self._spawn_worker()

    def _cancel_message(self) -> str:
        return "队列已停止" if self._stopping else "已取消"

    async def stop(self) -> None:
        self._started = False
        self._stopping = True
        try:
            for row in await list_tasks_by_status("queued", "running"):
                flag = self._cancel_flags.setdefault(row["id"], asyncio.Event())
                flag.set()
                await self._remove_aria2_gids(row["id"])
                await update_active_task(
                    row["id"], status="cancelled", message=self._cancel_message()
                )
                await self._publish(row["id"])
            for worker in self._workers:
                worker.cancel()
            if self._workers:
                await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers = []
            if self._aria2 is not None:
                await self._aria2.close()
                self._aria2 = None
        finally:
            self._stopping = False

    async def enqueue(self, payload: DownloadCreate) -> str:
        settings = get_settings()
        parse_model_name(payload.name)
        _validate_pair(payload.source, payload.target)

        active = await find_active_task(payload.name, payload.target)
        if active is not None:
            raise ValueError(
                f"已有进行中的任务（{active['id'][:8]}…），请等待完成或取消后再试"
            )
        if payload.target == "vllm":
            dest_path = str(hf_model_dir(settings.model_root, payload.name))
            if dest_path in self._busy_dests:
                raise ValueError("目标目录仍有未结束的下载，请稍后再试")
            message = "已加入队列，等待调度…"
        else:
            dest_path = ""
            message = f"Ollama 拉取；模型保存在 {ollama_root(settings.model_root)}"

        task_id = await insert_task(
            {
                "name": payload.name,
                "source": payload.source,
                "target": payload.target,
                "revision": payload.revision,
                "status": "queued",
                "dest_path": dest_path,
                "message": message,
            }
        )
        self._cancel_flags[task_id] = asyncio.Event()
        self._task_gids[task_id] = []
        await self._pending.put(task_id)
        await self._publish(task_id)
        return task_id

    async def cancel(self, task_id: str) -> None:
        flag = self._cancel_flags.setdefault(task_id, asyncio.Event())
        flag.set()
        # Mark cancelled before tearing down transports so races land as cancelled.
        changed = await update_active_task(
            task_id, status="cancelled", message=self._cancel_message()
        )
        await self._remove_aria2_gids(task_id)
        if changed:
            await self._publish(task_id)

    async def retry(self, task_id: str) -> None:
        row = await get_task(task_id)
        if row is None:
            return
        message = "已重新加入队列…"
        if row["target"] == "ollama":
            settings = get_settings()
            message = f"Ollama 拉取；模型保存在 {ollama_root(settings.model_root)}"
        if row["target"] == "vllm" and row.get("dest_path") in self._busy_dests:
            raise ValueError("目标目录仍有未结束的下载，请稍后再试")
        claimed = await claim_task_for_retry(
            task_id,
            status="queued",
            progress_bytes=0,
            total_bytes=None,
            speed_bps=None,
            message=message,
        )
        if not claimed:
            return
        self._cancel_flags[task_id] = asyncio.Event()
        self._task_gids[task_id] = []
        await self._pending.put(task_id)
        await self._publish(task_id)

    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.setdefault(task_id, []).append(q)
        return q

    def unsubscribe(self, task_id: str, queue: asyncio.Queue | None = None) -> None:
        subs = self._subscribers.get(task_id)
        if not subs:
            return
        if queue is None:
            self._subscribers.pop(task_id, None)
            return
        try:
            subs.remove(queue)
        except ValueError:
            return
        if not subs:
            self._subscribers.pop(task_id, None)

    async def _worker(self, worker_id: int) -> None:
        while self._started:
            if worker_id >= self.concurrency:
                return
            try:
                task_id = await asyncio.wait_for(self._pending.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            try:
                await self._process(task_id)
            except asyncio.CancelledError:
                raise
            except DownloadCancelled:
                try:
                    await self._finish_if_active(
                        task_id, status="cancelled", message=self._cancel_message()
                    )
                except Exception:
                    pass
            except Exception as exc:
                try:
                    if self._cancel_flags.get(task_id) and self._cancel_flags[task_id].is_set():
                        await self._finish_if_active(
                            task_id,
                            status="cancelled",
                            message=self._cancel_message(),
                        )
                    else:
                        await self._finish_if_active(
                            task_id, status="failed", message=f"下载失败：{exc}"
                        )
                except Exception:
                    pass
            finally:
                self._pending.task_done()

    async def _process(self, task_id: str) -> None:
        row = await get_task(task_id)
        if row is None:
            return

        cancelled = self._cancel_flags.setdefault(task_id, asyncio.Event())
        if cancelled.is_set() or row["status"] in _TERMINAL:
            await self._finish_if_active(
                task_id, status="cancelled", message=self._cancel_message()
            )
            return

        claimed = await update_active_task(
            task_id, status="running", message="正在准备下载…"
        )
        if not claimed:
            return
        row = await get_task(task_id)
        await self._publish(task_id)
        if cancelled.is_set():
            await self._finish_if_active(
                task_id, status="cancelled", message=self._cancel_message()
            )
            return

        async def on_progress(
            progress_bytes: int,
            total_bytes: int | None,
            speed_bps: float | None,
        ) -> None:
            if cancelled.is_set():
                await self._remove_aria2_gids(task_id)
                raise DownloadCancelled()
            await update_active_task(
                task_id,
                progress_bytes=progress_bytes,
                total_bytes=total_bytes,
                speed_bps=speed_bps,
            )
            await self._publish(task_id)

        async def on_log(message: str) -> None:
            if cancelled.is_set():
                return
            await update_active_task(task_id, message=message)
            await self._publish(task_id)

        try:
            if cancelled.is_set():
                await self._finish_if_active(
                    task_id, status="cancelled", message=self._cancel_message()
                )
                return
            await self._run_download(row, on_progress, on_log, cancelled)
        except DownloadCancelled:
            await self._finish_if_active(
                task_id, status="cancelled", message=self._cancel_message()
            )
            return
        except DownloadRemoved:
            await self._finish_if_active(
                task_id, status="cancelled", message=self._cancel_message()
            )
            return
        except asyncio.CancelledError:
            if cancelled.is_set():
                await self._finish_if_active(
                    task_id, status="cancelled", message=self._cancel_message()
                )
                return
            raise
        except Exception as exc:
            if cancelled.is_set():
                await self._finish_if_active(
                    task_id, status="cancelled", message=self._cancel_message()
                )
                return
            await self._finish_if_active(
                task_id, status="failed", message=f"下载失败：{exc}"
            )
            return

        if cancelled.is_set():
            await self._finish_if_active(
                task_id, status="cancelled", message=self._cancel_message()
            )
            return
        await self._finish_if_active(
            task_id, status="completed", speed_bps=0, message="下载已完成"
        )

    async def _run_download(self, task, on_progress, on_log, cancelled) -> None:
        settings = await effective_settings()
        model_root = get_settings().model_root

        async def ms_exists(name: str) -> bool:
            return await ms_repo_exists(name, settings["modelscope_api_token"])

        async def hf_exists(name: str) -> bool:
            return await hf_repo_exists(
                name, settings["hf_endpoint"], settings["hf_token"]
            )

        sources = await resolve_source(
            task["name"],
            task["source"],
            task["target"],
            ms_exists=ms_exists,
            hf_exists=hf_exists,
        )

        found_any = False
        errors: list[str] = []
        for source in sources:
            if cancelled.is_set():
                raise DownloadCancelled()
            label = _SOURCE_ZH.get(source, source)
            try:
                if source == "huggingface":
                    if not await hf_exists(task["name"]):
                        errors.append(f"{label}：未找到模型")
                        continue
                    found_any = True
                    dest = ensure_under_model_root(model_root, task["dest_path"])
                    dest_key = str(dest)
                    if dest_key in self._busy_dests:
                        raise RuntimeError("目标目录仍有未结束的下载，请稍后再试")
                    self._busy_dests.add(dest_key)
                    try:
                        aria2 = self._aria2_for_task(task["id"])
                        await download_hf(
                            task["name"],
                            dest,
                            endpoint=settings["hf_endpoint"],
                            token=settings["hf_token"],
                            revision=task.get("revision"),
                            aria2=aria2,
                            connections=settings["aria2_connections"],
                            on_progress=on_progress,
                            on_log=on_log,
                        )
                    finally:
                        self._busy_dests.discard(dest_key)
                    return
                if source == "modelscope":
                    if not await ms_exists(task["name"]):
                        errors.append(f"{label}：未找到模型")
                        continue
                    found_any = True
                    dest = ensure_under_model_root(model_root, task["dest_path"])
                    dest_key = str(dest)
                    if dest_key in self._busy_dests:
                        raise RuntimeError("目标目录仍有未结束的下载，请稍后再试")
                    self._busy_dests.add(dest_key)
                    try:
                        await download_modelscope(
                            task["name"],
                            dest,
                            token=settings["modelscope_api_token"],
                            revision=task.get("revision"),
                            on_progress=on_progress,
                            on_log=on_log,
                            on_detached=lambda key=dest_key: self._busy_dests.discard(key),
                        )
                    except BaseException:
                        # on_detached releases when the background thread finishes.
                        raise
                    else:
                        self._busy_dests.discard(dest_key)
                    return
                if source == "ollama":
                    found_any = True
                    await download_ollama(
                        task["name"],
                        settings["ollama_base_url"],
                        on_progress=on_progress,
                        on_log=on_log,
                    )
                    return
                errors.append(f"{label}：未知下载源")
            except DownloadCancelled:
                raise
            except DownloadRemoved:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                found_any = True
                errors.append(f"{label}：{exc}")
                await on_log(f"{label} 失败：{exc}")
                # auto mode continues to the next source

        if cancelled.is_set():
            raise DownloadCancelled()
        if not found_any:
            tried = "、".join(_SOURCE_ZH.get(s, s) for s in sources)
            raise RuntimeError(f"在尝试的源中未找到模型（已尝试：{tried}）")
        raise RuntimeError("；".join(errors) if errors else "下载失败")

    def _aria2_for_task(self, task_id: str) -> _GidTrackingAria2 | None:
        if self._aria2 is None:
            return None
        gids = self._task_gids.setdefault(task_id, [])
        return _GidTrackingAria2(self._aria2, gids)

    async def _remove_aria2_gids(self, task_id: str) -> None:
        if self._aria2 is None:
            return
        for gid in list(self._task_gids.get(task_id, [])):
            try:
                await self._aria2.force_remove(gid)
            except Exception:
                pass

    async def _finish_if_active(self, task_id: str, *, status: str, **fields) -> None:
        changed = await update_active_task(task_id, status=status, **fields)
        if changed:
            await self._publish(task_id)

    async def _publish(self, task_id: str) -> None:
        row = await get_task(task_id)
        if row is None:
            return
        event = {
            "progress_bytes": row["progress_bytes"],
            "total_bytes": row["total_bytes"],
            "speed_bps": row["speed_bps"],
            "status": row["status"],
            "message": row["message"],
        }
        alive: list[asyncio.Queue] = []
        for q in list(self._subscribers.get(task_id, [])):
            try:
                q.put_nowait(event)
                alive.append(q)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                    alive.append(q)
                except asyncio.QueueFull:
                    continue
        if alive:
            self._subscribers[task_id] = alive
        elif task_id in self._subscribers:
            self._subscribers.pop(task_id, None)
        if event["status"] in _TERMINAL:
            self._subscribers.pop(task_id, None)
