import asyncio
from pathlib import Path

from app.aria2_client import Aria2Client
from app.config import get_settings
from app.db import get_task, insert_task, update_task
from app.downloaders import (
    download_hf,
    download_modelscope,
    download_ollama,
    hf_repo_exists,
    ms_repo_exists,
)
from app.models_schema import DownloadCreate
from app.paths import hf_model_dir, ollama_root
from app.source_resolve import resolve_source


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


class DownloadQueue:
    def __init__(self, concurrency: int | None = None) -> None:
        settings = get_settings()
        self.concurrency = (
            concurrency if concurrency is not None else settings.download_concurrency
        )
        self._pending: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._task_gids: dict[str, list[str]] = {}
        self._started = False
        self._aria2: Aria2Client | None = None

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        settings = get_settings()
        self._aria2 = Aria2Client(
            settings.aria2_rpc_url, secret=settings.aria2_rpc_secret
        )
        self._workers = [
            asyncio.create_task(self._worker()) for _ in range(self.concurrency)
        ]

    async def stop(self) -> None:
        self._started = False
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []
        if self._aria2 is not None:
            await self._aria2.close()
            self._aria2 = None

    async def enqueue(self, payload: DownloadCreate) -> str:
        settings = get_settings()
        if payload.target == "vllm":
            dest_path = str(hf_model_dir(settings.model_root, payload.name))
            message = ""
        else:
            dest_path = ""
            message = f"Ollama pull; models stored under {ollama_root(settings.model_root)}"

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
        await self._remove_aria2_gids(task_id)

        row = await get_task(task_id)
        if row is None:
            return
        if row["status"] in ("queued", "running"):
            if row["status"] == "queued":
                await update_task(task_id, status="cancelled", message="Cancelled")
                await self._publish(task_id)

    async def retry(self, task_id: str) -> None:
        row = await get_task(task_id)
        if row is None or row["status"] not in ("failed", "cancelled"):
            return
        self._cancel_flags[task_id] = asyncio.Event()
        self._task_gids[task_id] = []
        await update_task(
            task_id,
            status="queued",
            progress_bytes=0,
            speed_bps=None,
            message="",
        )
        await self._pending.put(task_id)
        await self._publish(task_id)

    def subscribe(self, task_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(task_id, []).append(q)
        return q

    async def _worker(self) -> None:
        while True:
            task_id = await self._pending.get()
            try:
                await self._process(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                try:
                    await update_task(task_id, status="failed", message=str(exc))
                    await self._publish(task_id)
                except Exception:
                    pass
            finally:
                self._pending.task_done()

    async def _process(self, task_id: str) -> None:
        row = await get_task(task_id)
        if row is None:
            return

        cancelled = self._cancel_flags.setdefault(task_id, asyncio.Event())
        if cancelled.is_set() or row["status"] == "cancelled":
            if row["status"] != "cancelled":
                await update_task(task_id, status="cancelled", message="Cancelled")
                await self._publish(task_id)
            return

        await update_task(task_id, status="running")
        row = await get_task(task_id)
        await self._publish(task_id)

        async def on_progress(
            progress_bytes: int,
            total_bytes: int | None,
            speed_bps: float | None,
        ) -> None:
            if cancelled.is_set():
                await self._remove_aria2_gids(task_id)
                raise asyncio.CancelledError()
            await update_task(
                task_id,
                progress_bytes=progress_bytes,
                total_bytes=total_bytes,
                speed_bps=speed_bps,
            )
            await self._publish(task_id)

        async def on_log(message: str) -> None:
            await update_task(task_id, message=message)
            await self._publish(task_id)

        try:
            await self._run_download(row, on_progress, on_log, cancelled)
        except asyncio.CancelledError:
            if cancelled.is_set():
                await update_task(task_id, status="cancelled", message="Cancelled")
                await self._publish(task_id)
                return
            raise
        except Exception as exc:
            await update_task(task_id, status="failed", message=str(exc))
            await self._publish(task_id)
            return

        if cancelled.is_set():
            await update_task(task_id, status="cancelled", message="Cancelled")
        else:
            await update_task(task_id, status="completed", speed_bps=0)
        await self._publish(task_id)

    async def _run_download(self, task, on_progress, on_log, cancelled) -> None:
        settings = get_settings()

        async def ms_exists(name: str) -> bool:
            return await ms_repo_exists(name, settings.modelscope_api_token)

        async def hf_exists(name: str) -> bool:
            return await hf_repo_exists(
                name, settings.hf_endpoint, settings.hf_token
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
                return
            try:
                if source == "huggingface":
                    if not await hf_exists(task["name"]):
                        errors.append(f"{source}: not found")
                        continue
                    found_any = True
                    dest = Path(task["dest_path"])
                    aria2 = self._aria2_for_task(task["id"])
                    await download_hf(
                        task["name"],
                        dest,
                        endpoint=settings.hf_endpoint,
                        token=settings.hf_token,
                        revision=task.get("revision"),
                        aria2=aria2,
                        connections=settings.aria2_connections,
                        on_progress=on_progress,
                        on_log=on_log,
                    )
                    return
                if source == "modelscope":
                    if not await ms_exists(task["name"]):
                        errors.append(f"{source}: not found")
                        continue
                    found_any = True
                    dest = Path(task["dest_path"])
                    await download_modelscope(
                        task["name"],
                        dest,
                        token=settings.modelscope_api_token,
                        revision=task.get("revision"),
                        on_progress=on_progress,
                        on_log=on_log,
                    )
                    return
                if source == "ollama":
                    found_any = True
                    await download_ollama(
                        task["name"],
                        settings.ollama_base_url,
                        on_progress=on_progress,
                        on_log=on_log,
                    )
                    return
                errors.append(f"{source}: unknown adapter")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                found_any = True
                errors.append(f"{source}: {exc}")
                await on_log(f"{source} failed: {exc}")

        if not found_any:
            raise RuntimeError(
                f"Model not found on attempted sources: {', '.join(sources)}"
            )
        raise RuntimeError("; ".join(errors) if errors else "Download failed")

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
                await self._aria2._call(
                    "aria2.forceRemove",
                    self._aria2._auth_params(gid),
                )
            except Exception:
                pass

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
        for q in self._subscribers.get(task_id, []):
            await q.put(event)
