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

_SOURCE_ZH = {
    "auto": "自动",
    "huggingface": "Hugging Face",
    "modelscope": "ModelScope",
    "ollama": "Ollama",
}

_TARGET_ZH = {
    "vllm": "vLLM",
    "ollama": "Ollama",
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


def _fmt_bytes(n: int | float | None) -> str:
    if n is None:
        return "—"
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "—"
    if value < 0:
        value = 0.0
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while value >= 1024 and i < len(units) - 1:
        value /= 1024
        i += 1
    return f"{value:.1f} {units[i]}" if i else f"{int(value)} {units[i]}"


def _fmt_speed(bps: float | int | None) -> str:
    if bps is None:
        return "测算中"
    try:
        speed = float(bps)
    except (TypeError, ValueError):
        return "测算中"
    if speed <= 0:
        return "测算中"
    return f"{_fmt_bytes(speed)}/s"


def _fmt_eta(
    progress_bytes: int | None,
    total_bytes: int | None,
    speed_bps: float | int | None,
    *,
    event: str,
) -> str:
    if event == "completed":
        return "已完成"
    if event in ("failed", "cancelled"):
        return "—"
    if not total_bytes:
        return "总量未知，待测速后估算"
    try:
        total = int(total_bytes)
        done = int(progress_bytes or 0)
        speed = float(speed_bps or 0)
    except (TypeError, ValueError):
        return "待测速后估算"
    remain = max(total - done, 0)
    if remain <= 0:
        return "即将完成"
    if speed <= 0:
        return "待测速后估算"
    seconds = int((remain / speed) + 0.999)
    if seconds < 60:
        return f"约 {seconds} 秒"
    if seconds < 3600:
        return f"约 {(seconds + 59) // 60} 分钟"
    hours = seconds / 3600
    return f"约 {hours:.1f} 小时"


def _progress_line(task: dict) -> str:
    progress = task.get("progress_bytes") or 0
    total = task.get("total_bytes")
    try:
        done = int(progress)
    except (TypeError, ValueError):
        done = 0
    if total:
        try:
            total_n = int(total)
        except (TypeError, ValueError):
            return _fmt_bytes(done)
        if total_n > 0:
            pct = min(100.0, 100.0 * done / total_n)
            return f"{pct:.1f}% · {_fmt_bytes(done)} / {_fmt_bytes(total_n)}"
    return _fmt_bytes(done)


def _default_note(event: str, task: dict) -> str:
    if event == "running":
        speed = task.get("speed_bps")
        if speed and float(speed) > 0:
            return "下载进行中，速率与 ETA 见上方。"
        return "下载已启动，正在建立连接/测速…"
    if event == "completed":
        return "模型已就绪，可在模型库中查看。"
    if event == "failed":
        return "请到任务页查看详情后重试。"
    if event == "cancelled":
        return "任务已取消；可稍后重试。"
    return ""


def format_task_message(task: dict, event: str) -> str:
    title = _STATUS_ZH.get(event, event)
    name = task.get("name") or "未知模型"
    source = _SOURCE_ZH.get(task.get("source") or "", task.get("source") or "—")
    target = _TARGET_ZH.get(task.get("target") or "", task.get("target") or "—")
    task_id = str(task.get("id") or "")
    revision = (task.get("revision") or "").strip()

    lines = [
        f"【dlmodel】{title}",
        f"模型：{name}",
        f"来源：{source} → {target}",
    ]
    if revision:
        lines.append(f"版本：{revision}")
    if task_id:
        lines.append(f"任务：#{task_id[:8]}")
    lines.append(f"进度：{_progress_line(task)}")
    lines.append(f"速率：{_fmt_speed(task.get('speed_bps'))}")
    lines.append(
        f"预计剩余：{_fmt_eta(task.get('progress_bytes'), task.get('total_bytes'), task.get('speed_bps'), event=event)}"
    )

    msg = (task.get("message") or "").strip()
    # Drop low-value boilerplate that duplicates structured fields.
    skip_notes = {
        "正在准备下载…",
        "下载已完成",
        "已取消",
        "已加入队列，等待调度…",
    }
    if msg and msg not in skip_notes and event in (
        "failed",
        "cancelled",
        "completed",
        "running",
    ):
        lines.append(f"说明：{msg[:200]}")
    else:
        note = _default_note(event, task)
        if note:
            lines.append(f"说明：{note}")
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
