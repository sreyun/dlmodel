from fastapi import APIRouter, Depends

from app.auth import require_admin

router = APIRouter()


@router.post("/api/auth/verify")
async def verify(_: None = Depends(require_admin)) -> dict:
    return {"ok": True}
