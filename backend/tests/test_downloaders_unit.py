import httpx
import pytest
import requests
from pathlib import Path
from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError
from unittest.mock import AsyncMock, MagicMock, patch

from app.downloaders.ollama import download_ollama
from app.downloaders.hf import hf_repo_exists, download_hf
from app.downloaders.modelscope import ms_repo_exists, download_modelscope


def _hf_404():
    return RepositoryNotFoundError(
        "not found",
        response=httpx.Response(404, request=httpx.Request("GET", "https://hf.co/api/models/org/model")),
    )


def _hf_500():
    return HfHubHTTPError(
        "server error",
        response=httpx.Response(500, request=httpx.Request("GET", "https://hf.co/api/models/org/model")),
    )


def _ms_404():
    resp = requests.Response()
    resp.status_code = 404
    return requests.exceptions.HTTPError("not found", response=resp)


def _ms_500():
    resp = requests.Response()
    resp.status_code = 500
    return requests.exceptions.HTTPError("server error", response=resp)


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
        MockApi.return_value.repo_info = MagicMock(side_effect=_hf_404())
        assert await hf_repo_exists("org/model", "https://hf.co", None) is False


@pytest.mark.asyncio
async def test_hf_repo_exists_transport_error_raises():
    with patch("app.downloaders.hf.HfApi") as MockApi:
        MockApi.return_value.repo_info = MagicMock(side_effect=_hf_500())
        with pytest.raises(HfHubHTTPError):
            await hf_repo_exists("org/model", "https://hf.co", None)


@pytest.mark.asyncio
async def test_download_hf_uses_http_fallback_when_aria2_missing():
    logs = []

    async def on_log(m):
        logs.append(m)

    with (
        patch("app.downloaders.hf.HfApi") as MockApi,
        patch(
            "app.downloaders.hf.hf_hub_url", return_value="https://hf.co/file.bin"
        ) as url_mock,
        patch("app.downloaders.hf.http_download", new_callable=AsyncMock) as http_mock,
    ):
        MockApi.return_value.list_repo_files = MagicMock(return_value=["file.bin"])

        async def on_progress(d, t, s):
            pass

        await download_hf(
            "org/model",
            Path("/tmp/dest"),
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
        assert any("aria2 unavailable" in m for m in logs)


@pytest.mark.asyncio
async def test_download_hf_aria2_failure_falls_back():
    aria2 = MagicMock()
    aria2.is_available = AsyncMock(return_value=True)
    aria2.add_uri = AsyncMock(return_value="gid-1")
    aria2.tell_status = AsyncMock(side_effect=RuntimeError("aria2 down"))

    logs = []

    async def on_log(m):
        logs.append(m)

    with (
        patch("app.downloaders.hf.HfApi") as MockApi,
        patch("app.downloaders.hf.hf_hub_url", return_value="https://hf.co/file.bin"),
        patch("app.downloaders.hf.http_download", new_callable=AsyncMock) as http_mock,
        patch("app.downloaders.hf.asyncio.sleep", new_callable=AsyncMock),
    ):
        MockApi.return_value.list_repo_files = MagicMock(return_value=["file.bin"])

        async def on_progress(d, t, s):
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
        assert any("aria2 failed" in m for m in logs)


@pytest.mark.asyncio
async def test_download_hf_token_uses_authenticated_http():
    with (
        patch("app.downloaders.hf.HfApi") as MockApi,
        patch(
            "app.downloaders.hf.hf_hub_url", return_value="https://hf.co/file.bin"
        ),
        patch("app.downloaders.hf.http_download", new_callable=AsyncMock) as http_mock,
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
            token="secret-token",
            revision="main",
            aria2=None,
            connections=4,
            on_progress=on_progress,
            on_log=on_log,
        )
        http_mock.assert_awaited_once()
        _, kwargs = http_mock.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_ms_repo_exists_true():
    with patch("app.downloaders.modelscope.HubApi") as MockApi:
        MockApi.return_value.repo_exists = MagicMock(return_value=True)
        assert await ms_repo_exists("damo/model", None) is True


@pytest.mark.asyncio
async def test_ms_repo_exists_false():
    with patch("app.downloaders.modelscope.HubApi") as MockApi:
        MockApi.return_value.repo_exists = MagicMock(side_effect=_ms_404())
        assert await ms_repo_exists("damo/model", None) is False


@pytest.mark.asyncio
async def test_ms_repo_exists_transport_error_raises():
    with patch("app.downloaders.modelscope.HubApi") as MockApi:
        MockApi.return_value.repo_exists = MagicMock(side_effect=_ms_500())
        with pytest.raises(requests.exceptions.HTTPError):
            await ms_repo_exists("damo/model", None)


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
