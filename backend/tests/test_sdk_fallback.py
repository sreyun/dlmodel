from unittest.mock import AsyncMock, patch

import httpx
import pytest
import respx
from httpx import Response

from app.downloaders.sdk_fallback import http_download


@pytest.mark.asyncio
@respx.mock
async def test_http_download_retries_ssl_eof_then_succeeds(tmp_path):
    dest = tmp_path / "file.bin"
    route = respx.get("https://mirror.example/file.bin").mock(
        side_effect=[
            httpx.ConnectError("SSL: UNEXPECTED_EOF_WHILE_READING"),
            Response(200, content=b"hello-world"),
        ]
    )
    logs = []

    async def on_progress(d, t, s):
        pass

    async def on_log(m):
        logs.append(m)

    with patch("app.downloaders.sdk_fallback.asyncio.sleep", new_callable=AsyncMock):
        await http_download(
            "https://mirror.example/file.bin",
            dest,
            on_progress=on_progress,
            retries=2,
            on_log=on_log,
        )
    assert dest.read_bytes() == b"hello-world"
    assert route.call_count == 2
    assert any("重试" in m for m in logs)


@pytest.mark.asyncio
@respx.mock
async def test_http_download_resumes_with_range(tmp_path):
    dest = tmp_path / "file.bin"
    dest.write_bytes(b"hello")
    route = respx.get("https://mirror.example/file.bin").mock(
        return_value=Response(
            206,
            content=b"-world",
            headers={
                "Content-Length": "6",
                "Content-Range": "bytes 5-10/11",
            },
        )
    )

    async def on_progress(d, t, s):
        pass

    await http_download(
        "https://mirror.example/file.bin",
        dest,
        on_progress=on_progress,
        retries=0,
    )
    assert dest.read_bytes() == b"hello-world"
    assert route.calls[0].request.headers.get("range") == "bytes=5-"


@pytest.mark.asyncio
@respx.mock
async def test_http_download_non_transient_fails_immediately(tmp_path):
    dest = tmp_path / "file.bin"
    route = respx.get("https://mirror.example/file.bin").mock(
        return_value=Response(404, content=b"missing")
    )

    async def on_progress(d, t, s):
        pass

    with pytest.raises(httpx.HTTPStatusError):
        await http_download(
            "https://mirror.example/file.bin",
            dest,
            on_progress=on_progress,
            retries=3,
        )
    assert route.call_count == 1


@pytest.mark.asyncio
@respx.mock
async def test_http_download_restarts_aria2_partial_instead_of_appending(tmp_path):
    """aria2 的半成品是稀疏文件：长度已满但中间有空洞，不能按长度续传。"""
    dest = tmp_path / "file.bin"
    dest.write_bytes(b"hello\x00\x00\x00\x00\x00d")  # 长度已是 11，中间是空洞
    control = tmp_path / "file.bin.aria2"
    control.write_bytes(b"control")
    route = respx.get("https://mirror.example/file.bin").mock(
        return_value=Response(200, content=b"hello-world")
    )
    logs: list[str] = []

    async def on_progress(d, t, s):
        pass

    async def on_log(m):
        logs.append(m)

    await http_download(
        "https://mirror.example/file.bin",
        dest,
        on_progress=on_progress,
        retries=0,
        on_log=on_log,
    )
    assert "range" not in route.calls[0].request.headers
    assert dest.read_bytes() == b"hello-world"
    assert not control.exists()
    assert any("从头下载" in m for m in logs)


@pytest.mark.asyncio
@respx.mock
async def test_http_download_restarts_when_local_file_longer_than_remote(tmp_path):
    dest = tmp_path / "file.bin"
    dest.write_bytes(b"stale-and-much-longer")
    route = respx.get("https://mirror.example/file.bin").mock(
        side_effect=[
            Response(416, headers={"Content-Range": "bytes */11"}),
            Response(200, content=b"hello-world"),
        ]
    )

    async def on_progress(d, t, s):
        pass

    await http_download(
        "https://mirror.example/file.bin", dest, on_progress=on_progress, retries=0
    )
    assert dest.read_bytes() == b"hello-world"
    assert route.call_count == 2
    assert "range" not in route.calls[1].request.headers


@pytest.mark.asyncio
@respx.mock
async def test_http_download_416_on_complete_file_is_done(tmp_path):
    dest = tmp_path / "file.bin"
    dest.write_bytes(b"hello-world")
    route = respx.get("https://mirror.example/file.bin").mock(
        return_value=Response(416, headers={"Content-Range": "bytes */11"})
    )
    seen: list[tuple[int, int | None]] = []

    async def on_progress(d, t, s):
        seen.append((d, t))

    await http_download(
        "https://mirror.example/file.bin", dest, on_progress=on_progress, retries=0
    )
    assert dest.read_bytes() == b"hello-world"
    assert route.call_count == 1
    assert seen == [(11, 11)]
