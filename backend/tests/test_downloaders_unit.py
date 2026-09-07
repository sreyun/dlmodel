import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from app.downloaders.ollama import download_ollama
from app.downloaders.hf import hf_repo_exists, download_hf
from app.downloaders.modelscope import ms_repo_exists, download_modelscope


@pytest.mark.asyncio
async def test_ollama_pull_parses_progress():
    import respx
    from httpx import Response

    progresses = []

    async def on_progress(d, t, s):
        progresses.append((d, t))

    async def on_log(m):
        pass

    with respx.mock:
        respx.post("http://ollama/api/pull").mock(
            return_value=Response(
                200,
                content=b'{"status":"pulling","total":100,"completed":40}\n{"status":"success"}\n',
            )
        )
        await download_ollama(
            "llama3.2", "http://ollama", on_progress=on_progress, on_log=on_log
        )

    assert any(p[0] == 40 and p[1] == 100 for p in progresses)


@pytest.mark.asyncio
async def test_hf_repo_exists_true():
    with patch("app.downloaders.hf.HfApi") as MockApi:
        MockApi.return_value.repo_info = MagicMock(return_value={"id": "repo"})
        assert await hf_repo_exists("org/model", "https://hf.co", None) is True


@pytest.mark.asyncio
async def test_hf_repo_exists_false():
    with patch("app.downloaders.hf.HfApi") as MockApi:
        MockApi.return_value.repo_info = MagicMock(side_effect=Exception("not found"))
        assert await hf_repo_exists("org/model", "https://hf.co", None) is False


@pytest.mark.asyncio
async def test_download_hf_uses_http_fallback_when_aria2_missing():
    with (
        patch("app.downloaders.hf.HfApi") as MockApi,
        patch(
            "app.downloaders.hf.hf_hub_url", return_value="https://hf.co/file.bin"
        ) as url_mock,
        patch("app.downloaders.hf.http_download", new_callable=AsyncMock) as http_mock,
        patch("app.downloaders.hf.os") as os_mock,
    ):
        MockApi.return_value.list_repo_files = MagicMock(return_value=["file.bin"])
        dest = Path("/tmp/dest")

        async def on_progress(d, t, s):
            pass

        async def on_log(m):
            pass

        await download_hf(
            "org/model",
            dest,
            endpoint="https://hf.co",
            token=None,
            revision="main",
            aria2=None,
            connections=4,
            on_progress=on_progress,
            on_log=on_log,
        )
        url_mock.assert_called_once()
        http_mock.assert_awaited_once()
        assert os_mock.environ.__setitem__.called


@pytest.mark.asyncio
async def test_download_hf_aria2_failure_falls_back():
    aria2 = MagicMock()
    aria2.is_available = AsyncMock(return_value=True)
    aria2.add_uri = AsyncMock(return_value="gid-1")
    aria2.tell_status = AsyncMock(side_effect=RuntimeError("aria2 down"))

    with (
        patch("app.downloaders.hf.HfApi") as MockApi,
        patch("app.downloaders.hf.hf_hub_url", return_value="https://hf.co/file.bin"),
        patch("app.downloaders.hf.http_download", new_callable=AsyncMock) as http_mock,
        patch("app.downloaders.hf.asyncio.sleep", new_callable=AsyncMock),
        patch("app.downloaders.hf.os") as os_mock,
    ):
        MockApi.return_value.list_repo_files = MagicMock(return_value=["file.bin"])

        async def on_progress(d, t, s):
            pass

        async def on_log(m):
            pass

        await download_hf(
            "org/model",
            Path("/tmp/dest"),
            endpoint="https://hf.co",
            token=None,
            revision="main",
            aria2=aria2,
            connections=4,
            on_progress=on_progress,
            on_log=on_log,
        )
        aria2.add_uri.assert_awaited_once()
        http_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_ms_repo_exists_true():
    with patch("app.downloaders.modelscope.HubApi") as MockApi:
        MockApi.return_value.repo_exists = MagicMock(return_value=True)
        assert await ms_repo_exists("damo/model", None) is True


@pytest.mark.asyncio
async def test_ms_repo_exists_false():
    with patch("app.downloaders.modelscope.HubApi") as MockApi:
        MockApi.return_value.repo_exists = MagicMock(side_effect=Exception("not found"))
        assert await ms_repo_exists("damo/model", None) is False


@pytest.mark.asyncio
async def test_download_modelscope_calls_snapshot_download():
    with patch("app.downloaders.modelscope.snapshot_download") as sd:
        sd.return_value = str(Path("/tmp/modelscope/repo"))

        async def on_progress(d, t, s):
            pass

        async def on_log(m):
            pass

        await download_modelscope(
            "damo/model",
            Path("/tmp/dest"),
            token=None,
            revision="v1.0",
            on_progress=on_progress,
            on_log=on_log,
        )
        sd.assert_called_once()
