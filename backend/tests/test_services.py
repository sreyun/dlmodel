import pytest
import respx
from httpx import Response
from app.services_ollama import ollama_health
from app.services_vllm import vllm_launch_command


@pytest.mark.asyncio
@respx.mock
async def test_ollama_health_ok():
    respx.get("http://ollama:11434/api/tags").mock(return_value=Response(200, json={"models": []}))
    h = await ollama_health("http://ollama:11434")
    assert h["ok"] is True


def test_vllm_launch_command():
    cmd = vllm_launch_command("/models/hf/Qwen/Qwen2.5-7B-Instruct")
    assert "--model" in cmd
    assert "/models/hf/Qwen/Qwen2.5-7B-Instruct" in cmd
    assert "--port 8000" in cmd
