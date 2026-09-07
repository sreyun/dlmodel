import os

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import require_admin
from app.config import get_settings, optional_secret
from app.db import get_setting, set_setting

router = APIRouter()

_MERGE_KEYS = (
    "hf_endpoint",
    "hf_token",
    "modelscope_api_token",
    "download_concurrency",
    "aria2_connections",
    "ollama_base_url",
    "vllm_base_url",
)
_INT_KEYS = frozenset({"download_concurrency", "aria2_connections"})
_TOKEN_KEYS = frozenset({"hf_token", "modelscope_api_token"})
_ENV_NAMES = {
    "hf_endpoint": "HF_ENDPOINT",
    "hf_token": "HF_TOKEN",
    "modelscope_api_token": "MODELSCOPE_API_TOKEN",
    "download_concurrency": "DOWNLOAD_CONCURRENCY",
    "aria2_connections": "ARIA2_CONNECTIONS",
    "ollama_base_url": "OLLAMA_BASE_URL",
    "vllm_base_url": "VLLM_BASE_URL",
}


class SettingsUpdate(BaseModel):
    hf_endpoint: str | None = None
    hf_token: str | None = None
    modelscope_api_token: str | None = None
    download_concurrency: int | None = None
    aria2_connections: int | None = None
    ollama_base_url: str | None = None
    vllm_base_url: str | None = None


def _usable_override(key: str, raw: str | None) -> bool:
    if raw is None:
        return False
    if key in _TOKEN_KEYS and optional_secret(raw) is None:
        return False
    if key in _INT_KEYS:
        try:
            int(str(raw).strip())
        except (TypeError, ValueError):
            return False
        return True
    if isinstance(raw, str) and not raw.strip() and key not in _TOKEN_KEYS:
        # Empty non-token overrides are treated as unset.
        return False
    return True


def _coerce(key: str, raw: str):
    if key in _INT_KEYS:
        return int(str(raw).strip())
    return raw


async def effective_settings() -> dict:
    base = get_settings()
    out = {key: getattr(base, key) for key in _MERGE_KEYS}
    for key in _MERGE_KEYS:
        raw = await get_setting(key)
        if not _usable_override(key, raw):
            continue
        out[key] = _coerce(key, raw)
    for key in _TOKEN_KEYS:
        out[key] = optional_secret(out.get(key))
    return out


async def apply_sqlite_overrides() -> None:
    for key, env_name in _ENV_NAMES.items():
        raw = await get_setting(key)
        if not _usable_override(key, raw):
            continue
        os.environ[env_name] = raw


@router.get("/api/settings")
async def get_settings_route(_: None = Depends(require_admin)) -> dict:
    return await effective_settings()


@router.put("/api/settings")
async def put_settings_route(
    body: SettingsUpdate, _: None = Depends(require_admin)
) -> dict:
    updates = body.model_dump(exclude_unset=True)
    for key, value in updates.items():
        if key not in _ENV_NAMES:
            continue
        stored = "" if value is None else str(value)
        if key in _TOKEN_KEYS and optional_secret(stored) is None:
            continue
        await set_setting(key, stored)
    return await effective_settings()
