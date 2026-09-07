from typing import Annotated
from fastapi import Header, HTTPException
from app.config import get_settings


async def require_admin(authorization: Annotated[str | None, Header()] = None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer 令牌")
    token = authorization.removeprefix("Bearer ").strip()
    if token != get_settings().admin_token:
        raise HTTPException(status_code=401, detail="令牌无效")
