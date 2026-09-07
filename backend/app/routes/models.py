from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_admin
from app.config import get_settings
from app.library import delete_model, scan_hf_library

router = APIRouter()


@router.get("/api/models")
async def list_models(_: None = Depends(require_admin)) -> list[dict]:
    return scan_hf_library(get_settings().model_root)


@router.delete("/api/models/{model_id:path}")
async def remove_model(model_id: str, _: None = Depends(require_admin)) -> dict:
    try:
        delete_model(get_settings().model_root, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}
