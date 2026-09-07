from fastapi import APIRouter, Depends

from app.auth import require_admin

router = APIRouter()


@router.get("/api/health")
async def health() -> dict:
    """Unauthenticated liveness probe for Docker/K8s healthchecks."""
    return {"ok": True, "service": "dlmodel"}


@router.post("/api/auth/verify")
async def verify(_: None = Depends(require_admin)) -> dict:
    return {"ok": True}
