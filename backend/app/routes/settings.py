import os
from ipaddress import ip_address, ip_network
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth import require_admin
from app.config import get_settings, optional_secret
from app.db import delete_setting, get_setting, set_setting
from app.notify import send_robot_message, validate_webhook_url

router = APIRouter()

_MERGE_KEYS = (
    "hf_endpoint",
    "hf_token",
    "modelscope_api_token",
    "download_concurrency",
    "aria2_connections",
    "ollama_base_url",
    "vllm_base_url",
    "notify_dingtalk_webhook",
    "notify_feishu_webhook",
    "notify_wecom_webhook",
    "notify_on_completed",
    "notify_on_failed",
    "notify_on_started",
    "notify_on_cancelled",
)
_INT_KEYS = frozenset({"download_concurrency", "aria2_connections"})
_TOKEN_KEYS = frozenset({"hf_token", "modelscope_api_token"})
_WEBHOOK_KEYS = frozenset(
    {
        "notify_dingtalk_webhook",
        "notify_feishu_webhook",
        "notify_wecom_webhook",
    }
)
_BOOL_KEYS = frozenset(
    {
        "notify_on_completed",
        "notify_on_failed",
        "notify_on_started",
        "notify_on_cancelled",
    }
)
_URL_KEYS = frozenset({"hf_endpoint", "ollama_base_url", "vllm_base_url"})
_ENV_NAMES = {
    "hf_endpoint": "HF_ENDPOINT",
    "hf_token": "HF_TOKEN",
    "modelscope_api_token": "MODELSCOPE_API_TOKEN",
    "download_concurrency": "DOWNLOAD_CONCURRENCY",
    "aria2_connections": "ARIA2_CONNECTIONS",
    "ollama_base_url": "OLLAMA_BASE_URL",
    "vllm_base_url": "VLLM_BASE_URL",
    "notify_dingtalk_webhook": "NOTIFY_DINGTALK_WEBHOOK",
    "notify_feishu_webhook": "NOTIFY_FEISHU_WEBHOOK",
    "notify_wecom_webhook": "NOTIFY_WECOM_WEBHOOK",
    "notify_on_completed": "NOTIFY_ON_COMPLETED",
    "notify_on_failed": "NOTIFY_ON_FAILED",
    "notify_on_started": "NOTIFY_ON_STARTED",
    "notify_on_cancelled": "NOTIFY_ON_CANCELLED",
}
_BLOCKED_NETWORKS = (
    ip_network("169.254.169.254/32"),
    ip_network("169.254.0.0/16"),
)
_BOOL_DEFAULTS = {
    "notify_on_completed": True,
    "notify_on_failed": True,
    "notify_on_started": False,
    "notify_on_cancelled": False,
}
_WEBHOOK_CHANNEL = {
    "notify_dingtalk_webhook": "dingtalk",
    "notify_feishu_webhook": "feishu",
    "notify_wecom_webhook": "wecom",
}


class SettingsUpdate(BaseModel):
    hf_endpoint: str | None = Field(default=None, max_length=512)
    hf_token: str | None = Field(default=None, max_length=4096)
    modelscope_api_token: str | None = Field(default=None, max_length=4096)
    download_concurrency: int | None = Field(default=None, ge=1, le=32)
    aria2_connections: int | None = Field(default=None, ge=1, le=64)
    ollama_base_url: str | None = Field(default=None, max_length=512)
    vllm_base_url: str | None = Field(default=None, max_length=512)
    notify_dingtalk_webhook: str | None = Field(default=None, max_length=1024)
    notify_feishu_webhook: str | None = Field(default=None, max_length=1024)
    notify_wecom_webhook: str | None = Field(default=None, max_length=1024)
    notify_on_completed: bool | None = None
    notify_on_failed: bool | None = None
    notify_on_started: bool | None = None
    notify_on_cancelled: bool | None = None
    clear_hf_token: bool = False
    clear_modelscope_api_token: bool = False
    clear_notify_dingtalk_webhook: bool = False
    clear_notify_feishu_webhook: bool = False
    clear_notify_wecom_webhook: bool = False


class NotifyTestRequest(BaseModel):
    channel: str = Field(pattern="^(dingtalk|feishu|wecom|all)$")
    notify_dingtalk_webhook: str | None = Field(default=None, max_length=1024)
    notify_feishu_webhook: str | None = Field(default=None, max_length=1024)
    notify_wecom_webhook: str | None = Field(default=None, max_length=1024)


def _usable_override(key: str, raw: str | None) -> bool:
    if raw is None:
        return False
    if key in _TOKEN_KEYS or key in _WEBHOOK_KEYS:
        return optional_secret(raw) is not None
    if key in _INT_KEYS:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return False
        return value >= 1
    if key in _BOOL_KEYS:
        return str(raw).strip().lower() in {
            "0",
            "1",
            "true",
            "false",
            "yes",
            "no",
            "on",
            "off",
        }
    if isinstance(raw, str) and not raw.strip() and key not in _TOKEN_KEYS:
        return False
    return True


def _coerce(key: str, raw: str):
    if key in _INT_KEYS:
        return int(str(raw).strip())
    if key in _BOOL_KEYS:
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    if key in _TOKEN_KEYS or key in _WEBHOOK_KEYS:
        return optional_secret(raw)
    return raw


def _validate_url(key: str, value: str) -> None:
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail=f"{key} 需要是合法的 http(s) 地址",
        )
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail=f"{key} 不允许在 URL 中嵌入凭据")
    host = parsed.hostname or ""
    if host in {"metadata.google.internal", "metadata"}:
        raise HTTPException(status_code=400, detail=f"{key} 地址不被允许")
    try:
        addr = ip_address(host)
    except ValueError:
        return
    for network in _BLOCKED_NETWORKS:
        if addr in network:
            raise HTTPException(status_code=400, detail=f"{key} 地址不被允许")


async def effective_settings() -> dict:
    base = get_settings()
    out: dict = {}
    for key in _MERGE_KEYS:
        if hasattr(base, key):
            out[key] = getattr(base, key)
        elif key in _BOOL_DEFAULTS:
            out[key] = _BOOL_DEFAULTS[key]
        else:
            out[key] = None
    for key in _MERGE_KEYS:
        raw = await get_setting(key)
        if not _usable_override(key, raw):
            continue
        out[key] = _coerce(key, raw)
    for key in _TOKEN_KEYS:
        out[key] = optional_secret(out.get(key))
    for key in _WEBHOOK_KEYS:
        out[key] = optional_secret(out.get(key))
    for key, default in _BOOL_DEFAULTS.items():
        if out.get(key) is None:
            out[key] = default
    return out


def public_settings(merged: dict) -> dict:
    return {
        "hf_endpoint": merged["hf_endpoint"],
        "download_concurrency": merged["download_concurrency"],
        "aria2_connections": merged["aria2_connections"],
        "ollama_base_url": merged["ollama_base_url"],
        "vllm_base_url": merged["vllm_base_url"],
        "hf_token_set": bool(merged.get("hf_token")),
        "modelscope_api_token_set": bool(merged.get("modelscope_api_token")),
        "notify_dingtalk_webhook_set": bool(merged.get("notify_dingtalk_webhook")),
        "notify_feishu_webhook_set": bool(merged.get("notify_feishu_webhook")),
        "notify_wecom_webhook_set": bool(merged.get("notify_wecom_webhook")),
        "notify_on_completed": bool(merged.get("notify_on_completed", True)),
        "notify_on_failed": bool(merged.get("notify_on_failed", True)),
        "notify_on_started": bool(merged.get("notify_on_started", False)),
        "notify_on_cancelled": bool(merged.get("notify_on_cancelled", False)),
    }


async def apply_sqlite_overrides() -> None:
    for key, env_name in _ENV_NAMES.items():
        if key in _TOKEN_KEYS or key in _WEBHOOK_KEYS:
            continue
        raw = await get_setting(key)
        if not _usable_override(key, raw):
            continue
        os.environ[env_name] = str(raw)


@router.get("/api/settings")
async def get_settings_route(_: None = Depends(require_admin)) -> dict:
    return public_settings(await effective_settings())


@router.put("/api/settings")
async def put_settings_route(
    body: SettingsUpdate,
    request: Request,
    _: None = Depends(require_admin),
) -> dict:
    updates = body.model_dump(exclude_unset=True)
    clear_map = {
        "clear_hf_token": ("hf_token", "HF_TOKEN"),
        "clear_modelscope_api_token": ("modelscope_api_token", "MODELSCOPE_API_TOKEN"),
        "clear_notify_dingtalk_webhook": ("notify_dingtalk_webhook", "NOTIFY_DINGTALK_WEBHOOK"),
        "clear_notify_feishu_webhook": ("notify_feishu_webhook", "NOTIFY_FEISHU_WEBHOOK"),
        "clear_notify_wecom_webhook": ("notify_wecom_webhook", "NOTIFY_WECOM_WEBHOOK"),
    }
    for flag, (key, env_name) in clear_map.items():
        if bool(updates.pop(flag, False)):
            updates.pop(key, None)
            await delete_setting(key)
            os.environ.pop(env_name, None)

    for key, value in list(updates.items()):
        if key not in _ENV_NAMES and key not in _MERGE_KEYS:
            continue
        if key in _URL_KEYS and isinstance(value, str):
            _validate_url(key, value)
        if key in _WEBHOOK_KEYS and isinstance(value, str) and value.strip():
            try:
                validate_webhook_url(_WEBHOOK_CHANNEL[key], value.strip())
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        if key in _INT_KEYS and value is not None and int(value) < 1:
            raise HTTPException(status_code=400, detail=f"{key} 必须大于等于 1")
        if key in _BOOL_KEYS:
            stored = "1" if bool(value) else "0"
        else:
            stored = "" if value is None else str(value)
        if (key in _TOKEN_KEYS or key in _WEBHOOK_KEYS) and optional_secret(stored) is None:
            continue
        await set_setting(key, stored)

    await apply_sqlite_overrides()

    if "download_concurrency" in updates and updates["download_concurrency"] is not None:
        queue = getattr(request.app.state, "queue", None)
        if queue is not None and hasattr(queue, "resize"):
            await queue.resize(int(updates["download_concurrency"]))

    return public_settings(await effective_settings())


@router.post("/api/settings/notify-test")
async def notify_test_route(
    body: NotifyTestRequest, _: None = Depends(require_admin)
) -> dict:
    settings = await effective_settings()
    channels = (
        ["dingtalk", "feishu", "wecom"]
        if body.channel == "all"
        else [body.channel]
    )
    key_map = {
        "dingtalk": "notify_dingtalk_webhook",
        "feishu": "notify_feishu_webhook",
        "wecom": "notify_wecom_webhook",
    }
    overrides = {
        "dingtalk": body.notify_dingtalk_webhook,
        "feishu": body.notify_feishu_webhook,
        "wecom": body.notify_wecom_webhook,
    }
    results: dict[str, str] = {}
    sample = (
        "【dlmodel】通知测试\n"
        "模型：demo/test-model\n"
        "来源：auto → vllm\n"
        "进度：—\n"
        "说明：这是一条来自设置页的测试消息。"
    )
    sent = 0
    for channel in channels:
        override = overrides.get(channel)
        url = optional_secret(override) if override is not None else None
        if url is None:
            url = settings.get(key_map[channel])
        if not url:
            results[channel] = "未配置"
            continue
        try:
            if override and optional_secret(override):
                validate_webhook_url(channel, str(url))
            await send_robot_message(channel, str(url), sample)
            results[channel] = "ok"
            sent += 1
        except ValueError as exc:
            results[channel] = str(exc)
        except Exception as exc:
            results[channel] = str(exc)
    if sent == 0:
        raise HTTPException(status_code=400, detail=f"未成功发送：{results}")
    return {"ok": True, "results": results}
