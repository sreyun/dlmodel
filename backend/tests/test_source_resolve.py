import pytest
from app.source_resolve import looks_like_ollama_library, resolve_source


def test_ollama_library_tag():
    assert looks_like_ollama_library("llama3.2")
    assert looks_like_ollama_library("llama3.2:latest")
    assert not looks_like_ollama_library("Qwen/Qwen2.5-7B")


@pytest.mark.asyncio
async def test_auto_prefers_modelscope_then_hf():
    async def ms_ok(n): return True
    async def hf_ok(n): return True
    order = await resolve_source("Qwen/Qwen2.5", "auto", "vllm", ms_exists=ms_ok, hf_exists=hf_ok)
    assert order == ["modelscope"]


@pytest.mark.asyncio
async def test_auto_falls_back_to_hf():
    async def ms_ok(n): return False
    async def hf_ok(n): return True
    order = await resolve_source("Qwen/Qwen2.5", "auto", "vllm", ms_exists=ms_ok, hf_exists=hf_ok)
    assert order == ["huggingface"]


@pytest.mark.asyncio
async def test_target_ollama_library_uses_ollama():
    async def ms_ok(n): return False
    async def hf_ok(n): return False
    order = await resolve_source("llama3.2", "auto", "ollama", ms_exists=ms_ok, hf_exists=hf_ok)
    assert order == ["ollama"]


@pytest.mark.asyncio
async def test_forced_source():
    async def ms_ok(n): return False
    async def hf_ok(n): return False
    order = await resolve_source("x/y", "huggingface", "vllm", ms_exists=ms_ok, hf_exists=hf_ok)
    assert order == ["huggingface"]
