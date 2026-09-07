"""Group-chat robot notifications: DingTalk / Feishu / WeCom."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)

_STATUS_ZH = {
    "queued": "排队中",
    "running": "开始下载",
    "completed": "下载完成",
    "failed": "下载失败",
    "cancelled": "已取消",
}

_HOST_ALLOW = {
    "dingtalk": {"oapi.dingtalk.com"},
    "feishu": {"open.feishu.cn", "open.larksuite.com"},
    "wecom": {"qyapi.weixin.qq.com"},
}


def webhook_channel(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    for channel, hosts in _HOST_ALLOW.items():
        if host in hosts:
            return channel
    return None


def validate_webhook_url(channel: str, url: str) -> None:
    parsed = urlparse(url.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{channel} Webhook 需要 https 地址")
    host = (parsed.hostname or "").lower()
    allowed = _HOST_ALLOW.get(channel, set())
    if host not in allowed:
        raise ValueError(
            f"{channel} Webhook 主机不被允许（期望：{', '.join(sorted(allowed))}）"
        )


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    value = float(n)
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    return f"{value:.1f} {units[i]}" if i else f"{int(value)} {units[i]}"


def format_task_message(task: dict, event: str) -> str:
    title = _STATUS_ZH.get(event, event)
    name = task.get("name") or "未知模型"
    source = task.get("source") or "—"
    target = task.get("target") or "—"
    progress = _fmt_bytes(task.get("progress_bytes"))
    total = task.get("total_bytes")
    size_line = f"{progress}" + (f" / {_fmt_bytes(total)}" if total else "")
    msg = (task.get("message") or "").strip()
    lines = [
        f"【dlmodel】{title}",
        f"模型：{name}",
        f"来源：{source} → {target}",
        f"进度：{size_line}",
    ]
    if msg and event in ("failed", "cancelled", "completed", "running"):
        lines.append(f"说明：{msg[:200]}")
    return "\n".join(lines)


def _dingtalk_payload(text: str) -> dict[str, Any]:
    return {"msgtype": "text", "text": {"content": text}}


def _feishu_payload(text: str) -> dict[str, Any]:
    return {"msg_type": "text", "content": {"text": text}}


def _wecom_payload(text: str) -> dict[str, Any]:
    return {"msgtype": "text", "text": {"content": text}}


_PAYLOADS = {
    "dingtalk": _dingtalk_payload,
    "feishu": _feishu_payload,
    "wecom": _wecom_payload,
}


async def _post_webhook(url: str, payload: dict[str, Any]) -> None:
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        # Platform-specific soft errors often still return HTTP 200.
        try:
            data = resp.json()
        except Exception:
            return
        if isinstance(data, dict):
            errcode = data.get("errcode", data.get("code", data.get("StatusCode")))
            if errcode not in (None, 0, "0", "success"):
                raise RuntimeError(str(data.get("errmsg") or data.get("msg") or data))


async def send_robot_message(channel: str, webhook: str, text: str) -> None:
    validate_webhook_url(channel, webhook)
    builder = _PAYLOADS.get(channel)
    if not builder:
        raise ValueError(f"未知通知渠道：{channel}")
    await _post_webhook(webhook.strip(), builder(text))


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


async def notify_download_event(settings: dict, task: dict, event: str) -> None:
    """Fire-and-forget safe: logs errors, never raises to callers."""
    try:
        if event == "completed" and not _truthy(settings.get("notify_on_completed"), True):
            return
        if event == "failed" and not _truthy(settings.get("notify_on_failed"), True):
            return
        if event == "running" and not _truthy(settings.get("notify_on_started"), False):
            return
        if event == "cancelled" and not _truthy(settings.get("notify_on_cancelled"), False):
            return

        text = format_task_message(task, event)
        targets = [
            ("dingtalk", settings.get("notify_dingtalk_webhook")),
            ("feishu", settings.get("notify_feishu_webhook")),
            ("wecom", settings.get("notify_wecom_webhook")),
        ]
        for channel, url in targets:
            if not url or not str(url).strip():
                continue
            try:
                await send_robot_message(channel, str(url), text)
            except Exception as exc:
                logger.warning("notify %s failed: %s", channel, exc)
    except Exception as exc:
        logger.warning("notify_download_event failed: %s", exc)
