import pytest
import respx
from httpx import Response

from app.notify import (
    format_task_message,
    notify_download_event,
    send_robot_message,
    validate_webhook_url,
)


def test_validate_webhook_hosts():
    validate_webhook_url(
        "dingtalk",
        "https://oapi.dingtalk.com/robot/send?access_token=abc",
    )
    validate_webhook_url(
        "feishu",
        "https://open.feishu.cn/open-apis/bot/v2/hook/abc",
    )
    validate_webhook_url(
        "wecom",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abc",
    )
    with pytest.raises(ValueError):
        validate_webhook_url("dingtalk", "http://oapi.dingtalk.com/robot/send")
    with pytest.raises(ValueError):
        validate_webhook_url("dingtalk", "https://evil.example/hook")


def test_format_task_message_includes_progress_rate_and_eta():
    text = format_task_message(
        {
            "id": "abcdef1234567890",
            "name": "org/model",
            "source": "huggingface",
            "target": "vllm",
            "revision": "main",
            "progress_bytes": 100 * 1024 * 1024,
            "total_bytes": 400 * 1024 * 1024,
            "speed_bps": 10 * 1024 * 1024,
            "message": "正在下载 weights.bin",
        },
        "running",
    )
    assert "开始下载" in text
    assert "org/model" in text
    assert "Hugging Face → vLLM" in text
    assert "版本：main" in text
    assert "任务：#abcdef12" in text
    assert "25.0%" in text
    assert "速率：10.0 MB/s" in text
    assert "预计剩余：约 30 秒" in text
    assert "正在下载 weights.bin" in text


def test_format_task_message_started_without_speed_uses_placeholders():
    text = format_task_message(
        {
            "id": "xyz",
            "name": "Qwen/demo",
            "source": "auto",
            "target": "vllm",
            "progress_bytes": 0,
            "total_bytes": None,
            "speed_bps": None,
            "message": "正在准备下载…",
        },
        "running",
    )
    assert "自动 → vLLM" in text
    assert "速率：测算中" in text
    assert "预计剩余：总量未知，待测速后估算" in text
    assert "正在准备下载" not in text
    assert "下载已启动" in text


def test_format_task_message_completed_eta():
    text = format_task_message(
        {
            "name": "org/model",
            "source": "modelscope",
            "target": "vllm",
            "progress_bytes": 2048,
            "total_bytes": 2048,
            "speed_bps": 0,
            "message": "下载已完成",
        },
        "completed",
    )
    assert "下载完成" in text
    assert "预计剩余：已完成" in text
    assert "ModelScope → vLLM" in text


@pytest.mark.asyncio
@respx.mock
async def test_send_robot_message_payloads():
    ding = respx.post("https://oapi.dingtalk.com/robot/send").mock(
        return_value=Response(200, json={"errcode": 0})
    )
    feishu = respx.post("https://open.feishu.cn/open-apis/bot/v2/hook/x").mock(
        return_value=Response(200, json={"code": 0})
    )
    wecom = respx.post("https://qyapi.weixin.qq.com/cgi-bin/webhook/send").mock(
        return_value=Response(200, json={"errcode": 0})
    )
    await send_robot_message(
        "dingtalk",
        "https://oapi.dingtalk.com/robot/send?access_token=t",
        "hello",
    )
    await send_robot_message(
        "feishu",
        "https://open.feishu.cn/open-apis/bot/v2/hook/x",
        "hello",
    )
    await send_robot_message(
        "wecom",
        "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=k",
        "hello",
    )
    assert ding.called
    assert ding.calls[0].request.content
    assert b'"msgtype":"text"' in ding.calls[0].request.content
    assert feishu.called
    assert b'"msg_type":"text"' in feishu.calls[0].request.content
    assert wecom.called


@pytest.mark.asyncio
@respx.mock
async def test_notify_skips_disabled_events_and_posts_enabled():
    route = respx.post("https://oapi.dingtalk.com/robot/send").mock(
        return_value=Response(200, json={"errcode": 0})
    )
    settings = {
        "notify_dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=t",
        "notify_on_completed": True,
        "notify_on_failed": False,
        "notify_on_started": False,
        "notify_on_cancelled": False,
    }
    task = {
        "name": "a/b",
        "source": "auto",
        "target": "vllm",
        "progress_bytes": 10,
        "total_bytes": None,
        "message": "ok",
    }
    await notify_download_event(settings, task, "failed")
    assert not route.called
    await notify_download_event(settings, task, "completed")
    assert route.called
