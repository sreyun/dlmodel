from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth import require_admin
from app.config import get_settings
from app.db import list_tasks_by_status
from app.library import delete_model, scan_hf_library
from app.paths import hf_model_dir

router = APIRouter()


@router.get("/api/models")
async def list_models(_: None = Depends(require_admin)) -> list[dict]:
    return scan_hf_library(get_settings().model_root)


@router.delete("/api/models/{model_id:path}")
async def remove_model(
    model_id: str, request: Request, _: None = Depends(require_admin)
) -> dict:
    settings = get_settings()
    parts = model_id.strip("/").split("/")
    if len(parts) == 3 and parts[0] == "hf":
        name = f"{parts[1]}/{parts[2]}"
        try:
            expected = str(hf_model_dir(settings.model_root, name))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # Paused counts too: its partial files are what a resume continues from.
        for task in await list_tasks_by_status("queued", "running", "paused"):
            if task.get("dest_path") == expected or (
                task.get("name") == name and task.get("target") == "vllm"
            ):
                raise HTTPException(
                    status_code=409,
                    detail="该模型有未完成的下载任务，请先取消任务再删除",
                )
        # A cancelled ModelScope download can keep writing from its SDK thread
        # for a while; deleting under it would leave a half-written directory.
        if request.app.state.queue.is_dest_busy(expected):
            raise HTTPException(
                status_code=409,
                detail="后台仍有下载线程在写入该目录，请稍后再删除",
            )
    try:
        delete_model(settings.model_root, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="模型未找到") from exc
    return {"ok": True}
