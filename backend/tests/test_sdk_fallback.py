import httpx
import pytest
import respx
from httpx import Response
from unittest.mock import AsyncMock, patch

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
