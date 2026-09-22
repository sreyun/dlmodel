import httpx
import pytest
import requests
from pathlib import Path
from types import SimpleNamespace
from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError
from unittest.mock import AsyncMock, MagicMock, patch

import app.downloaders.modelscope as modelscope_mod
from app.downloaders.ollama import download_ollama
from app.downloaders.hf import (
    _download_with_aria2,
    _projected_total,
    _sizes_from_model_info,
    _sizes_from_paths_info,
    hf_repo_exists,
    download_hf,
)
from app.downloaders.modelscope import (
    ms_repo_exists,
    ms_repo_file_sizes,
    download_modelscope,
    # 直引函数对象：autouse fixture 只替换模块属性，这里需要真实实现
    _probe_sizes,
)
from app.downloaders.progress import dir_size_bytes, format_bytes
from app.downloaders.base import DownloadPaused


@pytest.fixture(autouse=True)
def _offline_ms_size_probe(monkeypatch):
    """ModelScope 的总大小探测默认离线（单测不打网络）。"""
    monkeypatch.setattr(
        modelscope_mod, "_probe_sizes", AsyncMock(return_value={})
    )


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
async def test_hf_repo_exists_blank_token_omits_auth():
    with patch("app.downloaders.hf.HfApi") as MockApi:
        MockApi.return_value.repo_info = MagicMock(return_value={"id": "repo"})
        assert await hf_repo_exists("org/model", "https://hf.co", "") is True
        MockApi.assert_called_once_with(endpoint="https://hf.co", token=None)


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
        assert any("aria2 不可用" in m for m in logs)


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
        assert any("aria2 下载" in m and "回退" in m for m in logs)


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
async def test_download_hf_token_prefers_aria2_with_auth_header():
    aria2 = MagicMock()
    aria2.is_available = AsyncMock(return_value=True)
    aria2.add_uri = AsyncMock(return_value="gid-1")
    aria2.tell_status = AsyncMock(
        return_value={
            "status": "complete",
            "completed_length": 10,
            "total_length": 10,
            "download_speed": 0,
            "error_message": "",
        }
    )

    with (
        patch("app.downloaders.hf.HfApi") as MockApi,
        patch("app.downloaders.hf.hf_hub_url", return_value="https://hf.co/file.bin"),
        patch("app.downloaders.hf.http_download", new_callable=AsyncMock) as http_mock,
        patch("app.downloaders.hf.asyncio.sleep", new_callable=AsyncMock),
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
            aria2=aria2,
            connections=4,
            on_progress=on_progress,
            on_log=on_log,
        )
        http_mock.assert_not_awaited()
        aria2.add_uri.assert_awaited_once()
        kwargs = aria2.add_uri.await_args.kwargs
        assert kwargs["headers"] == ["Authorization: Bearer secret-token"]


@pytest.mark.asyncio
async def test_download_hf_blank_token_skips_auth_header():
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
            token="   ",
            revision="main",
            aria2=None,
            connections=4,
            on_progress=on_progress,
            on_log=on_log,
        )
        MockApi.assert_called_once_with(endpoint="https://hf.co", token=None)
        http_mock.assert_awaited_once()
        _, kwargs = http_mock.call_args
        assert not kwargs.get("headers")


@pytest.mark.asyncio
async def test_download_hf_probes_aria2_once_per_task():
    # Regression: availability was probed per file, so an unreachable aria2 added
    # a connect-timeout to every file. It must now be probed once and cached.
    aria2 = MagicMock()
    aria2.is_available = AsyncMock(return_value=False)

    logs = []

    async def on_log(m):
        logs.append(m)

    async def on_progress(d, t, s):
        pass

    with (
        patch("app.downloaders.hf.HfApi") as MockApi,
        patch("app.downloaders.hf.hf_hub_url", return_value="https://hf.co/f"),
        patch("app.downloaders.hf.http_download", new_callable=AsyncMock),
    ):
        MockApi.return_value.list_repo_files = MagicMock(
            return_value=["a.bin", "b.bin", "c.bin"]
        )
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
    assert aria2.is_available.await_count == 1


@pytest.mark.asyncio
async def test_download_hf_aria2_pause_does_not_fall_back_to_http():
    # A pause raised from the aria2 progress hook must propagate to the queue, not
    # be mistaken for an aria2 failure and silently retried over HTTP.
    aria2 = MagicMock()
    aria2.is_available = AsyncMock(return_value=True)
    aria2.add_uri = AsyncMock(return_value="gid-1")
    aria2.tell_status = AsyncMock(
        return_value={
            "status": "downloading",
            "completed_length": 10,
            "total_length": 100,
            "download_speed": 5,
            "error_message": "",
        }
    )

    async def on_progress(d, t, s):
        raise DownloadPaused()

    async def on_log(m):
        pass

    with (
        patch("app.downloaders.hf.HfApi") as MockApi,
        patch("app.downloaders.hf.hf_hub_url", return_value="https://hf.co/file.bin"),
        patch("app.downloaders.hf.http_download", new_callable=AsyncMock) as http_mock,
        patch("app.downloaders.hf.asyncio.sleep", new_callable=AsyncMock),
    ):
        MockApi.return_value.list_repo_files = MagicMock(return_value=["file.bin"])
        with pytest.raises(DownloadPaused):
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
        http_mock.assert_not_awaited()
        aria2.add_uri.assert_awaited_once()


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
async def test_download_modelscope_calls_snapshot_download(tmp_path):
    dest = tmp_path / "dest"
    progresses: list[tuple[int, int | None]] = []

    def fake_snapshot_download(**kwargs):
        local = Path(kwargs["local_dir"])
        local.mkdir(parents=True, exist_ok=True)
        (local / "weights.bin").write_bytes(b"x" * 2048)
        return str(local)

    with patch(
        "app.downloaders.modelscope.snapshot_download",
        side_effect=fake_snapshot_download,
    ) as sd:

        async def on_progress(d, t, s):
            progresses.append((d, t))

        async def on_log(m):
            pass

        await download_modelscope(
            "damo/model",
            dest,
            token=None,
            revision="v1.0",
            on_progress=on_progress,
            on_log=on_log,
        )
        sd.assert_called_once()
    assert progresses
    assert any(done >= 2048 for done, _total in progresses)


@pytest.mark.asyncio
async def test_download_modelscope_polls_progress_while_running(tmp_path):
    import time

    dest = tmp_path / "dest"
    progresses: list[int] = []

    def fake_snapshot_download(**kwargs):
        local = Path(kwargs["local_dir"])
        local.mkdir(parents=True, exist_ok=True)
        time.sleep(1.2)
        (local / "chunk.bin").write_bytes(b"y" * 4096)
        time.sleep(0.2)
        return str(local)

    with patch(
        "app.downloaders.modelscope.snapshot_download",
        side_effect=fake_snapshot_download,
    ):

        async def on_progress(d, t, s):
            progresses.append(d)

        async def on_log(m):
            pass

        await download_modelscope(
            "damo/model",
            dest,
            token=None,
            revision=None,
            on_progress=on_progress,
            on_log=on_log,
        )

    assert len(progresses) >= 2
    assert max(progresses) >= 4096


@pytest.mark.asyncio
async def test_download_modelscope_retries_transient_then_succeeds(tmp_path):
    dest = tmp_path / "dest"
    calls = {"n": 0}
    logs: list[str] = []

    def fake_snapshot_download(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.exceptions.ConnectionError(
                "SSL: UNEXPECTED_EOF_WHILE_READING"
            )
        local = Path(kwargs["local_dir"])
        local.mkdir(parents=True, exist_ok=True)
        (local / "ok.bin").write_bytes(b"z" * 128)
        return str(local)

    with (
        patch(
            "app.downloaders.modelscope.snapshot_download",
            side_effect=fake_snapshot_download,
        ),
        patch(
            "app.downloaders.modelscope.retry_backoff_seconds",
            return_value=0,
        ),
    ):

        async def on_progress(d, t, s):
            pass

        async def on_log(m):
            logs.append(m)

        await download_modelscope(
            "damo/model",
            dest,
            token=None,
            revision=None,
            on_progress=on_progress,
            on_log=on_log,
            retries=2,
        )

    assert calls["n"] == 2
    assert any("重试" in m for m in logs)
    assert (dest / "ok.bin").exists()


# ---- on_detached 释放契约（目标目录占位不得泄漏）----


@pytest.mark.asyncio
async def test_download_modelscope_pause_before_thread_releases_immediately(tmp_path):
    """线程未创建就收到暂停：必须同步以 on_detached(False) 释放，而非留永久占位。"""
    dest = tmp_path / "dest"
    releases: list[bool] = []

    async def on_progress(d, t, s):
        raise DownloadPaused()

    async def on_log(m):
        pass

    with pytest.raises(DownloadPaused):
        await download_modelscope(
            "damo/model",
            dest,
            token=None,
            revision=None,
            on_progress=on_progress,
            on_log=on_log,
            on_detached=releases.append,
            retries=0,
        )

    assert releases == [False]


@pytest.mark.asyncio
async def test_download_modelscope_log_pause_before_thread_releases_claim(tmp_path):
    """初始日志行上的暂停同样不得漏掉释放（历史泄漏点）。"""
    dest = tmp_path / "dest"
    releases: list[bool] = []

    async def on_progress(d, t, s):
        raise AssertionError("不应走到进度上报")

    async def on_log(m):
        raise DownloadPaused()

    with pytest.raises(DownloadPaused):
        await download_modelscope(
            "damo/model",
            dest,
            token=None,
            revision=None,
            on_progress=on_progress,
            on_log=on_log,
            on_detached=releases.append,
            retries=0,
        )

    assert releases == [False]


@pytest.mark.asyncio
async def test_download_modelscope_detach_releases_only_after_thread_finishes(tmp_path):
    """真实双写风险：先同步标 True，后台线程排空后补 False。"""
    import asyncio
    import time

    dest = tmp_path / "dest"
    releases: list[bool] = []
    ticks = {"n": 0}

    def fake_snapshot_download(**kwargs):
        local = Path(kwargs["local_dir"])
        local.mkdir(parents=True, exist_ok=True)
        (local / "part.bin").write_bytes(b"z" * 1024)
        time.sleep(1.2)  # 不可中断的阻塞线程，仍在写入
        return str(local)

    async def on_progress(d, t, s):
        ticks["n"] += 1
        if ticks["n"] >= 2:  # 轮询周期内暂停：线程已 spawn
            raise DownloadPaused()

    async def on_log(m):
        pass

    with patch(
        "app.downloaders.modelscope.snapshot_download",
        side_effect=fake_snapshot_download,
    ):
        with pytest.raises(DownloadPaused):
            await download_modelscope(
                "damo/model",
                dest,
                token=None,
                revision=None,
                on_progress=on_progress,
                on_log=on_log,
                on_detached=releases.append,
                retries=0,
            )

    assert releases[:1] == [True]
    for _ in range(80):  # 线程真正结束后才补上释放
        if releases == [True, False]:
            break
        await asyncio.sleep(0.05)
    assert releases == [True, False]


# --------------------------------------------------------------------------- #
# 总大小（进度条分母）来源
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ms_repo_file_sizes_parses_api_rows():
    """清单行式字段（Path/Size，含字符串数字）必须能拼出分母。"""
    with patch("app.downloaders.modelscope.HubApi") as MockApi:
        MockApi.return_value.get_model_files = MagicMock(
            return_value=[
                {"Path": "model-1.safetensors", "Size": "1000"},
                {"Path": "sub/model-2.safetensors", "Size": 700},
                {"Path": "README.md"},  # 无大小：不计入
                {"Path": "sub"},  # 目录项：不计入
                "垃圾行",
            ]
        )
        sizes = await ms_repo_file_sizes("damo/model", None, "v1")
    assert sizes == {"model-1.safetensors": 1000, "sub/model-2.safetensors": 700}


@pytest.mark.asyncio
async def test_ms_repo_file_sizes_swallows_api_error():
    with patch("app.downloaders.modelscope.HubApi") as MockApi:
        MockApi.return_value.get_model_files = MagicMock(
            side_effect=RuntimeError("mirror down")
        )
        assert await ms_repo_file_sizes("damo/model", None) == {}


@pytest.mark.asyncio
async def test_download_modelscope_reports_probed_total(tmp_path, monkeypatch):
    """探测到清单后，每一拍进度都要带上真实分母。"""
    dest = tmp_path / "dest"
    monkeypatch.setattr(
        modelscope_mod,
        "_probe_sizes",
        AsyncMock(return_value={"a.bin": 1000, "b.bin": 700}),
    )
    progresses: list[tuple[int, int | None]] = []
    logs: list[str] = []

    def fake_snapshot_download(**kwargs):
        local = Path(kwargs["local_dir"])
        local.mkdir(parents=True, exist_ok=True)
        (local / "a.bin").write_bytes(b"z" * 1000)
        (local / "b.bin").write_bytes(b"z" * 700)
        return str(local)

    async def on_progress(d, t, s):
        progresses.append((d, t))

    async def on_log(m):
        logs.append(m)

    with patch.object(
        modelscope_mod, "snapshot_download", side_effect=fake_snapshot_download
    ):
        await download_modelscope(
            "damo/model",
            dest,
            token=None,
            revision=None,
            on_progress=on_progress,
            on_log=on_log,
            retries=0,
        )

    assert progresses and all(total == 1700 for _done, total in progresses)
    assert any("预计 1.7 KB" in m for m in logs)
    assert progresses[-1] == (1700, 1700)


@pytest.mark.asyncio
async def test_download_modelscope_without_listing_keeps_total_unknown(
    tmp_path, monkeypatch
):
    """清单缺失时保持未知，绝不用已下载字节伪造 100%。"""
    dest = tmp_path / "dest"
    monkeypatch.setattr(modelscope_mod, "_probe_sizes", AsyncMock(return_value={}))
    progresses: list[tuple[int, int | None]] = []

    def fake_snapshot_download(**kwargs):
        local = Path(kwargs["local_dir"])
        local.mkdir(parents=True, exist_ok=True)
        (local / "a.bin").write_bytes(b"z" * 2048)
        return str(local)

    async def on_progress(d, t, s):
        progresses.append((d, t))

    async def on_log(m):
        pass

    with patch.object(
        modelscope_mod, "snapshot_download", side_effect=fake_snapshot_download
    ):
        await download_modelscope(
            "damo/model",
            dest,
            token=None,
            revision=None,
            on_progress=on_progress,
            on_log=on_log,
            retries=0,
        )

    assert any(done >= 2048 for done, _t in progresses)
    assert all(total is None for _done, total in progresses)


def test_projected_total_extrapolates_unsized_files():
    """分母不是「只算元数据确认的部分」：未知道文件要按同类外推。"""
    files = ["a.bin", "b.bin", "c.bin"]
    assert _projected_total(files, {"a.bin": 100, "b.bin": 100, "c.bin": 100}) == 300
    assert _projected_total(files, {"a.bin": 100}) == 300  # 100 + 2*100
    assert _projected_total(files, {}) == 0  # 一点都不知道：宁缺勿假
    # 已知的全是 0 字节文件：无法外推，保持未知优于伪造分母
    assert _projected_total(files, {"a.bin": 0}) == 0


def test_projected_total_prices_small_files_by_their_own_class():
    """配置文件不得按权重均值定价（旧行为会把 4G 模型外推成几百 G）。"""
    GB = 1024**3
    MiB = 1024 * 1024
    files = [
        "model-00001-of-00003.safetensors",
        "model-00002-of-00003.safetensors",
        "model-00003-of-00003.safetensors",
    ] + [f"part{i}.json" for i in range(30)]
    sizes = {"model-00001-of-00003.safetensors": GB}
    total = _projected_total(files, sizes)
    # 2 个未知分片按已知分片外推，30 个小文件按小文件价，不跟着分片一起放大
    assert total == GB + 2 * GB + 30 * MiB
    assert total < 4 * GB  # 跨类均值会给出 33 * GB 的幽灵分母


def test_projected_total_uses_the_median_not_the_mean():
    """一个异常大的分片不得带偏整批未知文件。"""
    files = ["a.bin", "b.bin", "c.bin", "d.bin", "e.bin", "f.bin"]
    sizes = {"a.bin": 10_000, "b.bin": 1_000, "c.bin": 1_000}
    # median([10000,1000,1000]) = 1000 → 12000 + 3*1000；均值会算成 4000 → 24000
    assert _projected_total(files, sizes) == 15_000


def test_sizes_prefer_the_lfs_blob_size_over_the_pointer():
    """LFS 仓库的顶层 size 可能是 git 指针长度，必须取 lfs.size。"""
    info = SimpleNamespace(
        siblings=[
            SimpleNamespace(
                rfilename="model.safetensors",
                size=1350,
                lfs={"size": 5_000_000_000, "pointer_size": 1350},
            ),
            SimpleNamespace(rfilename="config.json", size=42, lfs=None),
            SimpleNamespace(
                rfilename="tokenizer.json", size=None, lfs=SimpleNamespace(size=9_000_000)
            ),
            SimpleNamespace(rfilename="broken.bin", size=None, lfs=None),
        ]
    )
    assert _sizes_from_model_info(info) == {
        "model.safetensors": 5_000_000_000,
        "config.json": 42,
        "tokenizer.json": 9_000_000,
    }


def test_sizes_from_paths_info_accepts_dict_rows():
    """不同 SDK 版本返回 dict 或对象，两种形状都不能丢大小。"""
    infos = [
        {"path": "a.safetensors", "size": 10, "lfs": {"size": 20}},
        {"rfilename": "b.json", "size": 3},
        "垃圾行",
    ]
    assert _sizes_from_paths_info(infos) == {"a.safetensors": 20, "b.json": 3}


@pytest.mark.asyncio
async def test_probe_sizes_retries_until_the_listing_lands(monkeypatch):
    """镜像瞬时失败必须重试：晚到的分母也比「永远未知」强。"""
    calls = {"n": 0}

    async def flaky(name, token, revision=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("mirror timeout")
        return {"a.bin": 10}

    monkeypatch.setattr(modelscope_mod, "ms_repo_file_sizes", flaky)
    sizes = await _probe_sizes("damo/m", None, None, delay=0)
    assert sizes == {"a.bin": 10}
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_probe_sizes_gives_up_quietly_after_attempts(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("unreachable")

    monkeypatch.setattr(modelscope_mod, "ms_repo_file_sizes", boom)
    assert await _probe_sizes("damo/m", None, None, attempts=2, delay=0) == {}


@pytest.mark.asyncio
async def test_download_hf_partial_metadata_still_has_total(tmp_path):
    """元数据只盖住部分文件时，分母不得直接消失（旧行为：永远无百分比）。"""
    dest = tmp_path / "dest"
    progresses: list[tuple[int, int | None]] = []
    logs: list[str] = []
    real_sizes = {"big.bin": 1000, "small.bin": 700}

    async def fake_http(url, file_dest, on_progress=None, headers=None, retries=3, on_log=None):
        written = real_sizes[Path(file_dest).name]
        target = Path(file_dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"z" * written)
        await on_progress(written, written, 1024.0)

    with (
        patch("app.downloaders.hf.HfApi") as MockApi,
        patch("app.downloaders.hf.hf_hub_url", return_value="https://hf.co/f"),
        patch("app.downloaders.hf.http_download", side_effect=fake_http),
    ):
        MockApi.return_value.list_repo_files = MagicMock(
            return_value=["big.bin", "small.bin"]
        )
        # 镜像只回答了 big.bin 的大小
        MockApi.return_value.model_info = MagicMock(
            return_value=SimpleNamespace(
                siblings=[SimpleNamespace(rfilename="big.bin", size=1000)]
            )
        )

        async def on_progress(d, t, s):
            progresses.append((d, t))

        async def on_log(m):
            logs.append(m)

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

    totals = [t for _d, t in progresses if t]
    assert totals, "部分元数据也必须给出分母"
    assert any("预计总大小" in m for m in logs)
    assert progresses[-1] == (1700, 1700)  # 收口：下完就是 100%
    assert all(t >= d for d, t in progresses if t)  # 绝不 >100%


@pytest.mark.asyncio
async def test_download_hf_no_metadata_stays_indeterminate_first_file(tmp_path):
    """零元数据时，第一个文件不得把任务读成「几乎完成」。"""
    dest = tmp_path / "dest"
    progresses: list[tuple[int, int | None]] = []

    async def fake_http(url, file_dest, on_progress=None, headers=None, retries=3, on_log=None):
        target = Path(file_dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"z" * 500)
        await on_progress(500, 500, 1024.0)

    with (
        patch("app.downloaders.hf.HfApi") as MockApi,
        patch("app.downloaders.hf.hf_hub_url", return_value="https://hf.co/f"),
        patch("app.downloaders.hf.http_download", side_effect=fake_http),
    ):
        MockApi.return_value.list_repo_files = MagicMock(
            return_value=["a.bin", "b.bin", "c.bin"]
        )

        async def on_progress(d, t, s):
            progresses.append((d, t))

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

    # 第一个文件进行中：无仓信息 -> 诚实的未知
    assert any(d == 500 and t is None for d, t in progresses)
    # 但一个文件落盘后就能给出百分比，并且永远达不到假的 100%
    mid = [(d, t) for d, t in progresses if t]
    assert mid and all(d < t for d, t in mid[:-1])
    assert mid[-1] == (1500, 1500)


@pytest.mark.asyncio
async def test_aria2_progress_keeps_last_known_total(tmp_path):
    """HEAD 前 totalLength=0 不得把分母闪回未知。"""
    gid_calls = [
        {"status": "active", "completed_length": 0, "total_length": 0, "download_speed": 0, "error_message": ""},
        {"status": "active", "completed_length": 10, "total_length": 100, "download_speed": 5, "error_message": ""},
        {"status": "active", "completed_length": 20, "total_length": 0, "download_speed": 5, "error_message": ""},
        {"status": "complete", "completed_length": 100, "total_length": 100, "download_speed": 0, "error_message": ""},
    ]
    aria2 = MagicMock()
    aria2.add_uri = AsyncMock(return_value="gid-1")
    aria2.tell_status = AsyncMock(side_effect=gid_calls)
    seen: list[tuple[int, int | None]] = []

    async def on_progress(d, t, s):
        seen.append((d, t))

    with patch("app.downloaders.hf.asyncio.sleep", new_callable=AsyncMock):
        await _download_with_aria2(
            "https://hf.co/f", tmp_path / "dest" / "f.bin", aria2, 4, on_progress
        )

    assert seen == [(0, None), (10, 100), (20, 100), (100, 100)]


@pytest.mark.asyncio
async def test_ollama_pull_aggregates_layers_into_one_ratio():
    """多分片交错上报时，进度条只展示全局占比，不逐层重置。"""
    import respx
    from httpx import Response

    body = b"\n".join(
        [
            b'{"status":"pulling","digest":"sha256:a","total":1000,"completed":100}',
            b'{"status":"pulling","digest":"sha256:b","total":500,"completed":50}',
            b'{"status":"pulling","digest":"sha256:a","total":1000,"completed":600}',
            b'{"status":"pulling","digest":"sha256:a","total":1000,"completed":300}',
            b'{"status":"success"}',
        ]
    ) + b"\n"
    progresses: list[tuple[int, int | None]] = []

    async def on_progress(d, t, s):
        progresses.append((d, t))

    async def on_log(m):
        pass

    with respx.mock:
        respx.post("http://ollama/api/pull").mock(return_value=Response(200, content=body))
        await download_ollama(
            "llama3.2", "http://ollama", on_progress=on_progress, on_log=on_log
        )

    assert progresses[0] == (100, 1000)  # 只有 a 已知
    assert progresses[1] == (150, 1500)  # b 的分片尺寸累加进总量
    assert progresses[2] == (650, 1500)
    assert progresses[3] == (650, 1500)  # 回退的陈旧偏移被丢弃


def test_format_bytes_and_dir_size_bytes(tmp_path):
    assert format_bytes(None) == "—"
    assert format_bytes(0) == "0 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(200 * 1024**3) == "200.0 GB"
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.bin").write_bytes(b"z" * 10)
    (tmp_path / "sub" / "b.bin").write_bytes(b"z" * 5)
    assert dir_size_bytes(tmp_path) == 15
    assert dir_size_bytes(tmp_path / "missing") == 0
