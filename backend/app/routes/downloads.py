import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.auth import require_admin
from app.db import get_task, list_tasks
from app.models_schema import DownloadCreate, TaskOut

_TERMINAL = frozenset({"completed", "failed", "cancelled"})
_STATUS_ZH = {
    "queued": "排队中",
    "running": "下载中",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}

router = APIRouter()


def _as_task(row: dict) -> TaskOut:
    return TaskOut.model_validate(row)


def _event_payload(row: dict) -> dict:
    return {
        "progress_bytes": row["progress_bytes"],
        "total_bytes": row["total_bytes"],
        "speed_bps": row["speed_bps"],
        "status": row["status"],
        "message": row["message"],
    }


async def _get_existing(task_id: str) -> dict:
    row = await get_task(task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="下载任务不存在")
    return row


@router.post("/api/downloads")
async def create_download(
    payload: DownloadCreate,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    try:
        task_id = await request.app.state.queue.enqueue(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"id": task_id}


@router.get("/api/downloads")
async def list_downloads(_: None = Depends(require_admin)) -> list[TaskOut]:
    return [_as_task(row) for row in await list_tasks(limit=200)]


@router.get("/api/downloads/{task_id}")
async def get_download(task_id: str, _: None = Depends(require_admin)) -> TaskOut:
    return _as_task(await _get_existing(task_id))


@router.post("/api/downloads/{task_id}/cancel")
async def cancel_download(
    task_id: str, request: Request, _: None = Depends(require_admin)
) -> TaskOut:
    row = await _get_existing(task_id)
    if row["status"] in _TERMINAL:
        raise HTTPException(
            status_code=400,
            detail=f"无法取消处于「{_STATUS_ZH.get(row['status'], row['status'])}」状态的任务",
        )
    await request.app.state.queue.cancel(task_id)
    return _as_task(await _get_existing(task_id))


@router.post("/api/downloads/{task_id}/retry")
async def retry_download(
    task_id: str, request: Request, _: None = Depends(require_admin)
) -> TaskOut:
    row = await _get_existing(task_id)
    if row["status"] not in ("failed", "cancelled"):
        raise HTTPException(
            status_code=400,
            detail=f"无法重试处于「{_STATUS_ZH.get(row['status'], row['status'])}」状态的任务",
        )
    try:
        await request.app.state.queue.retry(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _as_task(await _get_existing(task_id))


@router.get("/api/downloads/{task_id}/events")
async def download_events(
    task_id: str,
    request: Request,
    _: None = Depends(require_admin),
) -> StreamingResponse:
    """SSE stream. Auth via Authorization Bearer only (no query tokens)."""
    await _get_existing(task_id)
    queue = request.app.state.queue

    async def gen():
        sub = queue.subscribe(task_id)
        try:
            current = await get_task(task_id)
            if current is None:
                return
            evt = _event_payload(current)
            yield f"data: {json.dumps(evt)}\n\n"
            if evt["status"] in _TERMINAL:
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    evt = await asyncio.wait_for(sub.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(evt)}\n\n"
                if evt["status"] in _TERMINAL:
                    return
        finally:
            queue.unsubscribe(task_id, sub)

    return StreamingResponse(gen(), media_type="text/event-stream")
