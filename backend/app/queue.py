import asyncio
import logging
import time
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
    update_task_if_in,
)
from app.downloaders import (
    download_hf,
    download_modelscope,
    download_ollama,
    hf_repo_exists,
    ms_repo_exists,
)
from app.downloaders.base import (
    DownloadCancelled,
    DownloadPaused,
    DownloadRemoved,
)
from app.models_schema import DownloadCreate
from app.paths import (
    disk_error_to_message,
    ensure_under_model_root,
    free_space_bytes,
    hf_model_dir,
    ollama_root,
    parse_model_name,
)
from app.routes.settings import effective_settings
from app.source_resolve import resolve_source

logger = logging.getLogger(__name__)

_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_SOURCE_ZH = {
    "huggingface": "Hugging Face",
    "modelscope": "ModelScope",
    "ollama": "Ollama",
}
_STATUS_ZH = {
    "queued": "排队中",
    "running": "下载中",
    "paused": "已暂停",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}
_SHUTDOWN_MSG = "服务关闭，下次启动后继续…"
_RECOVER_MSG = "服务重启后重新排队…"
_PAUSED_MSG = "已暂停"
_RESUME_MSG = "已恢复，等待调度…"


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
        self._cancel_flags: dict[str, asyncio.Event] = {}
        self._pause_flags: dict[str, asyncio.Event] = {}
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._task_gids: dict[str, list[str]] = {}
        self._started = False
        self._aria2: Aria2Client | None = None
        self._stopping = False
        self._busy_dests: set[str] = set()
        # dest_key -> task_id that currently owns the in-flight download, and the
        # subset of keys whose detached ModelScope thread may still be writing.
        self._dest_holders: dict[str, str] = {}
        self._detached_dests: set[str] = set()
        self._notify_tasks: set[asyncio.Task] = set()
        self._started_notified: set[str] = set()

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopping = False
        settings = get_settings()
        self._aria2 = Aria2Client(
            settings.aria2_rpc_url, secret=settings.aria2_rpc_secret
        )
        for _ in range(self.concurrency):
            self._spawn_worker()
        recovered = await self._recover_pending_tasks()
        logger.info(
            "download queue started concurrency=%s recovered=%s",
            self.concurrency,
            recovered,
        )

    async def _recover_pending_tasks(self) -> int:
        rows = await list_tasks_by_status("queued", "running")
        for row in rows:
            task_id = row["id"]
            self._cancel_flags.setdefault(task_id, asyncio.Event())
            self._task_gids.setdefault(task_id, [])
            # Park interrupted runs; clear speed so UI does not show a stale ETA.
            # Keep progress_bytes — downloaders resume where possible.
            await update_task(
                task_id,
                status="queued",
                message=_RECOVER_MSG,
                speed_bps=None,
            )
            await self._pending.put(task_id)
            await self._publish(task_id)
            logger.info(
                "recovered task id=%s name=%s prior_status=%s",
                task_id[:8],
                row.get("name"),
                row["status"],
            )
        return len(rows)

    def _spawn_worker(self) -> None:
        # Slot index == current worker count so a down-then-up resize reuses ids
        # instead of letting them grow past concurrency (which made new workers
        # immediately hit the ``worker_id >= concurrency`` exit and die).
        worker_id = len(self._workers)
        self._workers.append(asyncio.create_task(self._worker(worker_id)))

    async def resize(self, concurrency: int) -> None:
        concurrency = max(1, int(concurrency))
        previous = self.concurrency
        self.concurrency = concurrency
        self._workers = [w for w in self._workers if not w.done()]
        while self._started and len(self._workers) < concurrency:
            self._spawn_worker()
        if previous != concurrency:
            logger.info("download concurrency resized %s -> %s", previous, concurrency)

    def _cancel_message(self) -> str:
        return "已取消"

    async def stop(self) -> None:
        """Graceful process shutdown: park active tasks in SQLite for next boot.

        Does not mark downloads cancelled — compose restart / SIGTERM must keep
        task history durable until the user deletes it.
        """
        if not self._started and not self._workers:
            return
        self._started = False
        self._stopping = True
        try:
            parked = 0
            for row in await list_tasks_by_status("queued", "running"):
                task_id = row["id"]
                await self._remove_aria2_gids(task_id)
                changed = await update_active_task(
                    task_id,
                    status="queued",
                    message=_SHUTDOWN_MSG,
                    speed_bps=None,
                )
                if changed:
                    await self._publish(task_id)
                    parked += 1
                    logger.info(
                        "parked task on shutdown id=%s name=%s",
                        task_id[:8],
                        row.get("name"),
                    )
            for worker in self._workers:
                worker.cancel()
            if self._workers:
                await asyncio.gather(*self._workers, return_exceptions=True)
            self._workers = []
            self._cancel_flags.clear()
            self._pause_flags.clear()
            self._task_gids.clear()
            self._busy_dests.clear()
            self._dest_holders.clear()
            self._detached_dests.clear()
            self._started_notified.clear()
            if self._notify_tasks:
                await asyncio.gather(*list(self._notify_tasks), return_exceptions=True)
                self._notify_tasks.clear()
            if self._aria2 is not None:
                await self._aria2.close()
                self._aria2 = None
            logger.info("download queue stopped parked=%s", parked)
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
            await self._ensure_dest_free(dest_path)
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
        logger.info(
            "enqueued task id=%s name=%s source=%s target=%s",
            task_id[:8],
            payload.name,
            payload.source,
            payload.target,
        )
        return task_id

    async def cancel(self, task_id: str) -> None:
        row = await get_task(task_id)
        flag = self._cancel_flags.setdefault(task_id, asyncio.Event())
        flag.set()
        # A pending pause flag must not let a resuming worker ignore the cancel.
        pause_flag = self._pause_flags.get(task_id)
        if pause_flag is not None:
            pause_flag.set()
        # Include paused: a paused row owns no in-flight worker, so the CAS writes
        # the terminal state directly; a running row is still covered by the flag.
        changed = await update_task_if_in(
            task_id,
            ("queued", "running", "paused"),
            status="cancelled",
            message=self._cancel_message(),
        )
        await self._remove_aria2_gids(task_id)
        if changed:
            await self._publish(task_id)
            logger.info("cancelled task id=%s", task_id[:8])
        if row is not None and row["status"] == "paused":
            # No worker will run this task's finish path, so release tracking here.
            self._release_task(task_id)

    def _release_task(self, task_id: str) -> None:
        """Drop all in-memory bookkeeping for a task that owns no live worker.

        Used when a task reaches a terminal state or a durable paused state so the
        long-lived queue does not leak per-task dicts/sets. Also reclaims the
        target-dir claim so a downloader that failed before spawning its thread
        cannot leave a stale ``_busy_dests`` entry that would reject every later
        resume/retry/create forever. Keys whose detached ModelScope thread may
        still be writing are deliberately kept until the thread reports done.
        Never touches disk.
        """
        self._cancel_flags.pop(task_id, None)
        self._pause_flags.pop(task_id, None)
        self._task_gids.pop(task_id, None)
        self._started_notified.discard(task_id)
        for key, holder in list(self._dest_holders.items()):
            if holder == task_id and key not in self._detached_dests:
                self._release_dest(key, task_id)

    def _dest_key(self, dest_path: str) -> str:
        """Normalise a DB dest_path to the same form _run_download claims."""
        try:
            return str(ensure_under_model_root(get_settings().model_root, dest_path))
        except ValueError:
            return str(dest_path)

    def _release_dest(self, dest_key: str, task_id: str | None) -> None:
        self._busy_dests.discard(dest_key)
        self._detached_dests.discard(dest_key)
        holder = self._dest_holders.get(dest_key)
        if holder is not None and (task_id is None or holder == task_id):
            self._dest_holders.pop(dest_key, None)

    async def _ensure_dest_free(self, dest_path: str, task_id: str | None = None) -> None:
        """Gate a (re)queued download on the target dir being actually free.

        Only a *live* writer is a real conflict: a detached ModelScope thread
        still writing, or another task whose row is still queued/running. A dir
        held by this same task is the brief window right after pause/cancel —
        its worker releases the claim on its next progress tick (≤ a couple of
        seconds) — so we wait instead of rejecting. Everything else is an orphan
        and gets reclaimed here: a claim and its holder are always registered
        together synchronously, so a key with no holder (or one whose owner has
        already reached a state that owns no worker) can only be residue, and
        the error can never turn into a permanent "try again later".
        """
        if not dest_path:
            return
        key = self._dest_key(dest_path)
        for _ in range(50):  # up to ~5s covers one aria2/modelscope poll tick
            if key not in self._busy_dests:
                return
            if key in self._detached_dests:
                break  # ModelScope thread still writing: real double-write risk
            holder = self._dest_holders.get(key)
            if holder is None:
                self._release_dest(key, None)  # orphaned claim: never sticky
                return
            if holder == task_id:
                await asyncio.sleep(0.1)  # our own worker is winding down
                continue
            owner = await get_task(holder)
            if owner is None or owner["status"] not in ("queued", "running"):
                self._release_dest(key, None)  # dead owner: stale residue
                return
            break  # another live task owns the dir: fail fast
        if key not in self._busy_dests:
            return
        raise ValueError("目标目录仍有未结束的下载，请稍后再试")

    async def pause(self, task_id: str) -> None:
        row = await get_task(task_id)
        if row is None:
            raise ValueError("下载任务不存在")
        status = row["status"]
        if status in _TERMINAL:
            raise ValueError(
                f"无法暂停处于「{_STATUS_ZH.get(status, status)}」状态的任务"
            )
        if status == "paused":
            return  # idempotent
        # Signal first, then CAS: a running worker keeps transferring until its next
        # progress tick, where the flag makes it stop the stream and land on paused.
        self._pause_flags.setdefault(task_id, asyncio.Event()).set()
        changed = await update_task_if_in(
            task_id,
            ("queued", "running"),
            status="paused",
            speed_bps=None,
            message=_PAUSED_MSG,
        )
        if not changed:
            # Worker finished (completed/failed/cancelled) between read and CAS; its
            # own finish path already published the authoritative state.
            return
        await self._remove_aria2_gids(task_id)
        await self._publish(task_id)
        logger.info("paused task id=%s", task_id[:8])

    async def resume(self, task_id: str) -> None:
        row = await get_task(task_id)
        if row is None:
            raise ValueError("下载任务不存在")
        if row["status"] != "paused":
            raise ValueError(
                f"无法恢复处于「{_STATUS_ZH.get(row['status'], row['status'])}」状态的任务"
            )
        # Only a detached ModelScope thread (or another live task) blocks a
        # resume; the just-paused worker's own claim is awaited out here instead
        # of surfacing as a misleading 409.
        await self._ensure_dest_free(row.get("dest_path") or "", task_id)
        # CAS paused -> queued; progress_bytes is preserved so downloaders resume
        # in place (aria2 continue / HTTP Range / SDK temp dirs).
        changed = await update_task_if_in(
            task_id,
            ("paused",),
            status="queued",
            speed_bps=None,
            message=_RESUME_MSG,
        )
        if not changed:
            raise ValueError("任务状态已变化，请刷新后重试")
        # Clear stale pause/cancel flags and gid bookkeeping before requeueing.
        self._release_task(task_id)
        self._cancel_flags[task_id] = asyncio.Event()
        self._task_gids[task_id] = []
        await self._pending.put(task_id)
        await self._publish(task_id)
        logger.info("resumed task id=%s", task_id[:8])

    async def retry(self, task_id: str) -> None:
        row = await get_task(task_id)
        if row is None:
            return
        message = "已重新加入队列…"
        if row["target"] == "ollama":
            settings = get_settings()
            message = f"Ollama 拉取；模型保存在 {ollama_root(settings.model_root)}"
        await self._ensure_dest_free(row.get("dest_path") or "", task_id)
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
        self._pause_flags.pop(task_id, None)
        self._task_gids[task_id] = []
        self._started_notified.discard(task_id)
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
                if self._stopping:
                    try:
                        await update_active_task(
                            task_id,
                            status="queued",
                            message=_SHUTDOWN_MSG,
                            speed_bps=None,
                        )
                    except Exception:
                        logger.exception("park on cancel failed id=%s", task_id[:8])
                    return
                raise
            except DownloadCancelled:
                try:
                    await self._finish_if_active(
                        task_id, status="cancelled", message=self._cancel_message()
                    )
                except Exception:
                    logger.exception("cancel finish failed id=%s", task_id[:8])
            except DownloadPaused:
                try:
                    await self._finish_if_active(
                        task_id,
                        status="paused",
                        message=_PAUSED_MSG,
                        speed_bps=None,
                    )
                except Exception:
                    logger.exception("pause finish failed id=%s", task_id[:8])
            except Exception as exc:
                try:
                    if self._cancel_flags.get(task_id) and self._cancel_flags[task_id].is_set():
                        await self._finish_if_active(
                            task_id,
                            status="cancelled",
                            message=self._cancel_message(),
                        )
                    else:
                        logger.warning(
                            "download failed id=%s err=%s", task_id[:8], exc
                        )
                        await self._finish_if_active(
                            task_id, status="failed", message=f"下载失败：{exc}"
                        )
                except Exception:
                    logger.exception("failure finish failed id=%s", task_id[:8])
            finally:
                self._pending.task_done()

    async def _process(self, task_id: str) -> None:
        row = await get_task(task_id)
        if row is None:
            return

        cancelled = self._cancel_flags.setdefault(task_id, asyncio.Event())
        paused = self._pause_flags.setdefault(task_id, asyncio.Event())
        if cancelled.is_set() or row["status"] in _TERMINAL:
            await self._finish_if_active(
                task_id, status="cancelled", message=self._cancel_message()
            )
            return
        if row["status"] == "paused" or paused.is_set():
            # The task was paused while still queued (its id is already in _pending);
            # do not start it — re-park as paused with progress untouched.
            await self._finish_if_active(
                task_id, status="paused", message=_PAUSED_MSG, speed_bps=None
            )
            return

        claimed = await update_active_task(
            task_id, status="running", message="正在准备下载…"
        )
        if not claimed:
            return
        row = await get_task(task_id)
        await self._publish(task_id)
        logger.info(
            "task status id=%s status=running name=%s",
            task_id[:8],
            (row or {}).get("name"),
        )
        if cancelled.is_set():
            await self._finish_if_active(
                task_id, status="cancelled", message=self._cancel_message()
            )
            return

        # Progress bookkeeping is monotonic on purpose. Every transport learns the
        # denominator at a different moment (aria2 only knows total_length after its
        # HEAD, ModelScope after a repo listing, HF per file), so a single poll can
        # legitimately come back with less information than the last one. Feeding the
        # stored values back as the floor is what stops a 150 GB/200 GB bar from
        # blinking back to "unknown" or restarting at zero after a resume.
        progress_state = {
            "ts": 0.0,
            "bytes": -1,
            "done": max(0, int(row.get("progress_bytes") or 0)),
            "total": max(0, int(row.get("total_bytes") or 0)),
        }

        async def on_progress(
            progress_bytes: int,
            total_bytes: int | None,
            speed_bps: float | None,
        ) -> None:
            if cancelled.is_set():
                await self._remove_aria2_gids(task_id)
                raise DownloadCancelled()
            if paused.is_set():
                await self._remove_aria2_gids(task_id)
                raise DownloadPaused()
            now = time.monotonic()
            done = max(int(progress_bytes or 0), progress_state["done"])
            incoming = int(total_bytes or 0)
            if incoming:
                # A fresh denominator is trusted (sources refine their estimate as
                # files land) but never below the bytes already moved, which would
                # publish a >100 % ratio.
                total = max(incoming, done)
            else:
                # "No total in this poll" must never erase a total we already
                # learned — that erase was the single biggest source of the bar
                # losing its percentage mid-download.
                total = progress_state["total"]
            total_changed = total != progress_state["total"]
            progress_state["done"] = done
            progress_state["total"] = total
            progress_bytes, total_bytes = done, (total or None)
            finished = total_bytes is not None and done >= int(total_bytes) > 0
            # Persist/publish at most every ~0.5s or per 1 MB moved, but always the
            # first update and the final/complete ones so a finished row is exact.
            # HTTP fallback fires per network chunk; without this it would commit to
            # SQLite (plus a publish read) thousands of times per large file. A
            # changed total is published immediately so the bar gains (or corrects)
            # its percentage the moment the source reveals it, not up to 0.5 s later.
            due = (
                progress_state["ts"] == 0.0
                or (now - progress_state["ts"]) >= 0.5
                or (done - progress_state["bytes"]) >= (1024 * 1024)
                or speed_bps in (0, 0.0)
                or finished
                or total_changed
            )
            if due:
                progress_state["ts"] = now
                progress_state["bytes"] = done
                await update_active_task(
                    task_id,
                    progress_bytes=progress_bytes,
                    total_bytes=total_bytes,
                    speed_bps=speed_bps,
                )
                await self._publish(task_id)
            # Defer "started" notify until we have real transfer stats (rate/ETA).
            if task_id not in self._started_notified and (
                (speed_bps is not None and float(speed_bps) > 0)
                or progress_bytes > 0
                or (total_bytes is not None and int(total_bytes) > 0)
            ):
                self._started_notified.add(task_id)
                await self._schedule_notify(task_id, "running")

        async def on_log(message: str) -> None:
            if cancelled.is_set():
                return
            if paused.is_set():
                await self._remove_aria2_gids(task_id)
                raise DownloadPaused()
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
        except DownloadPaused:
            await self._finish_if_active(
                task_id, status="paused", message=_PAUSED_MSG, speed_bps=None
            )
            return
        except DownloadRemoved:
            await self._finish_if_active(
                task_id, status="cancelled", message=self._cancel_message()
            )
            return
        except asyncio.CancelledError:
            if self._stopping:
                await update_active_task(
                    task_id,
                    status="queued",
                    message=_SHUTDOWN_MSG,
                    speed_bps=None,
                )
                return
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
            logger.warning("download failed id=%s err=%s", task_id[:8], exc)
            await self._finish_if_active(
                task_id, status="failed", message=f"下载失败：{exc}"
            )
            return

        if cancelled.is_set():
            await self._finish_if_active(
                task_id, status="cancelled", message=self._cancel_message()
            )
            return
        # Close the row on the numbers we actually moved: a source whose listing
        # under-counted would otherwise stay forever at "98 %" after completing.
        final_done = progress_state["done"]
        final_total = max(progress_state["total"], final_done)
        await self._finish_if_active(
            task_id,
            status="completed",
            speed_bps=0,
            message="下载已完成",
            progress_bytes=final_done,
            total_bytes=final_total or None,
        )

    async def _run_download(self, task, on_progress, on_log, cancelled) -> None:
        # Pause is observed via the shared per-task flag (created by _process) so
        # the runner signature stays 4-positional (existing test doubles rely on it).
        paused = self._pause_flags.get(task["id"])
        settings = await effective_settings()
        model_root = get_settings().model_root
        ms_cache: dict[str, bool] = {}
        hf_cache: dict[str, bool] = {}

        async def ms_exists(name: str) -> bool:
            if name not in ms_cache:
                try:
                    ms_cache[name] = await ms_repo_exists(
                        name, settings["modelscope_api_token"]
                    )
                except Exception:
                    # Transient probe error: assume present so the download loop
                    # still attempts it and can fall back to another source.
                    ms_cache[name] = True
            return ms_cache[name]

        async def hf_exists(name: str) -> bool:
            if name not in hf_cache:
                try:
                    hf_cache[name] = await hf_repo_exists(
                        name, settings["hf_endpoint"], settings["hf_token"]
                    )
                except Exception:
                    hf_cache[name] = True
            return hf_cache[name]

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
            if paused is not None and paused.is_set():
                raise DownloadPaused()
            label = _SOURCE_ZH.get(source, source)
            try:
                if source == "huggingface":
                    if not await hf_exists(task["name"]):
                        errors.append(f"{label}：未找到模型")
                        continue
                    found_any = True
                    dest = ensure_under_model_root(model_root, task["dest_path"])
                    self._check_dest_writable(dest)
                    dest_key = str(dest)
                    if dest_key in self._busy_dests:
                        raise RuntimeError("目标目录仍有未结束的下载，请稍后再试")
                    self._busy_dests.add(dest_key)
                    self._dest_holders[dest_key] = task["id"]
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
                        self._release_dest(dest_key, task["id"])
                    return
                if source == "modelscope":
                    if not await ms_exists(task["name"]):
                        errors.append(f"{label}：未找到模型")
                        continue
                    found_any = True
                    dest = ensure_under_model_root(model_root, task["dest_path"])
                    self._check_dest_writable(dest)
                    dest_key = str(dest)
                    if dest_key in self._busy_dests:
                        raise RuntimeError("目标目录仍有未结束的下载，请稍后再试")
                    self._busy_dests.add(dest_key)
                    self._dest_holders[dest_key] = task["id"]

                    def _ms_release(
                        still_writing: bool,
                        key=dest_key,
                        tid=task["id"],
                    ) -> None:
                        # True: the detached snapshot_download thread is still
                        # writing — keep the claim (and mark it as a real risk
                        # for resume/retry). False: no thread (or it finished):
                        # release immediately.
                        if still_writing:
                            self._detached_dests.add(key)
                        else:
                            self._release_dest(key, tid)

                    try:
                        await download_modelscope(
                            task["name"],
                            dest,
                            token=settings["modelscope_api_token"],
                            revision=task.get("revision"),
                            on_progress=on_progress,
                            on_log=on_log,
                            on_detached=_ms_release,
                        )
                    except BaseException:
                        # download_modelscope guarantees on_detached fired (or
                        # pending from the detach task) on every propagating
                        # exit; _release_task remains the final backstop.
                        raise
                    else:
                        self._release_dest(dest_key, task["id"])
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
            except DownloadPaused:
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
        if paused is not None and paused.is_set():
            raise DownloadPaused()
        if not found_any:
            tried = "、".join(_SOURCE_ZH.get(s, s) for s in sources)
            raise RuntimeError(f"在尝试的源中未找到模型（已尝试：{tried}）")
        raise RuntimeError("；".join(errors) if errors else "下载失败")

    def _check_dest_writable(self, dest: Path) -> None:
        """Fail fast (with a path hint) on a full or non-writable target dir."""
        try:
            dest.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            message = disk_error_to_message(exc, dest)
            raise RuntimeError(message or f"无法创建目标目录：{dest}（{exc}）") from exc
        if free_space_bytes(dest) == 0:
            raise RuntimeError(f"磁盘空间不足：{dest} 所在分区已满（请清理 MODEL_ROOT）")

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

    async def _schedule_notify(self, task_id: str, event: str) -> None:
        """Fire-and-forget so webhook latency never blocks download workers."""
        task = asyncio.create_task(self._maybe_notify(task_id, event))
        self._notify_tasks.add(task)

        def _done(done: asyncio.Task) -> None:
            self._notify_tasks.discard(done)
            try:
                done.result()
            except Exception:
                logger.exception("notify task crashed id=%s event=%s", task_id[:8], event)

        task.add_done_callback(_done)

    async def _maybe_notify(self, task_id: str, event: str) -> None:
        try:
            row = await get_task(task_id)
            if row is None:
                return
            from app.notify import notify_download_event
            from app.routes.settings import effective_settings

            settings = await effective_settings()
            await notify_download_event(settings, row, event)
        except Exception:
            logger.exception("notify failed id=%s event=%s", task_id[:8], event)

    async def _finish_if_active(self, task_id: str, *, status: str, **fields) -> None:
        # Only a row this worker actually owns ("running") may be transitioned here.
        # cancel / pause / retry write their own state, so a no-op means somebody
        # else already moved the row — and a worker that is merely winding down must
        # never re-cancel a row that a retry has just requeued (which used to
        # silently swallow the retry: the task looked stuck on "已取消").
        changed = await update_task_if_in(task_id, ("running",), status=status, **fields)
        if status in _TERMINAL or status == "paused":
            # Worker is done with this task; drop in-memory bookkeeping so these
            # dicts do not grow with every task the long-lived queue serves.
            # Paused owns no live worker until resume, so its flags are released
            # here and re-created on resume. Disk artifacts are never touched.
            self._release_task(task_id)
        if not changed:
            return
        await self._publish(task_id)
        if status in ("completed", "failed", "cancelled", "running", "paused"):
            logger.info(
                "task status id=%s status=%s message=%s",
                task_id[:8],
                status,
                (fields.get("message") or "")[:120],
            )
        if status in ("completed", "failed", "cancelled"):
            # If started was never sent (tiny/instant finishes), skip; terminal covers it.
            self._started_notified.discard(task_id)
            await self._schedule_notify(task_id, status)

    async def _publish(self, task_id: str) -> None:
        if not self._subscribers.get(task_id):
            # No SSE consumers (UI polls); skip the extra DB read per progress tick.
            return
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
