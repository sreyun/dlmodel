from typing import Annotated
from fastapi import Header, HTTPException
from app.config import get_settings


async def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != get_settings().admin_token:
        raise HTTPException(status_code=401, detail="Invalid token")
