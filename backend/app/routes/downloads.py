import json
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.auth import require_admin
from app.config import get_settings
from app.db import get_task, list_tasks
from app.models_schema import DownloadCreate, TaskOut

_TERMINAL = frozenset({"completed", "failed", "cancelled"})

router = APIRouter()


async def _require_events_auth(
    authorization: Annotated[str | None, Header()] = None,
    token: Annotated[str | None, Query()] = None,
) -> None:
    if authorization:
        await require_admin(authorization)
        return
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")
    if token != get_settings().admin_token:
        raise HTTPException(status_code=401, detail="Invalid token")


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
        raise HTTPException(status_code=404, detail="Download not found")
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
    return [_as_task(row) for row in await list_tasks()]


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
            detail=f"Cannot cancel task in status {row['status']}",
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
            detail=f"Cannot retry task in status {row['status']}",
        )
    await request.app.state.queue.retry(task_id)
    return _as_task(await _get_existing(task_id))


@router.get("/api/downloads/{task_id}/events")
async def download_events(
    task_id: str,
    request: Request,
    _: None = Depends(_require_events_auth),
) -> StreamingResponse:
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
                evt = await sub.get()
                yield f"data: {json.dumps(evt)}\n\n"
                if evt["status"] in _TERMINAL:
                    return
        finally:
            queue.unsubscribe(task_id, sub)

    return StreamingResponse(gen(), media_type="text/event-stream")
