import asyncio
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_url
from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError

from app.aria2_client import Aria2Client
from app.config import get_settings, optional_secret
from app.downloaders.base import LogCallback, ProgressCallback
from app.downloaders.sdk_fallback import http_download
from app.paths import safe_path_under


class DownloadRemoved(RuntimeError):
    """aria2 download was force-removed (typically user cancel)."""


async def hf_repo_exists(name: str, endpoint: str, token: str | None) -> bool:
    api = HfApi(endpoint=endpoint, token=optional_secret(token))
    try:
        await asyncio.to_thread(api.repo_info, repo_id=name, repo_type="model")
        return True
    except RepositoryNotFoundError:
        return False
    except HfHubHTTPError as exc:
        if exc.response.status_code == 404:
            return False
        raise


def _hf_download_url(
    name: str, filename: str, revision: str | None, endpoint: str
) -> str:
    return hf_hub_url(
        repo_id=name,
        filename=filename,
        revision=revision,
        repo_type="model",
        endpoint=endpoint,
    )


async def _download_with_aria2(
    url: str,
    file_dest: Path,
    aria2: Aria2Client,
    connections: int,
    on_progress: ProgressCallback,
    *,
    headers: list[str] | None = None,
    max_tries: int = 5,
) -> None:
    file_dest.parent.mkdir(parents=True, exist_ok=True)
    gid = await aria2.add_uri(
        [url],
        out_dir=str(file_dest.parent),
        out_name=file_dest.name,
        connections=connections,
        headers=headers,
        max_tries=max_tries,
    )
    while True:
        status = await aria2.tell_status(gid)
        total = status.get("total_length") or 0
        completed = status.get("completed_length") or 0
        speed = status.get("download_speed") or 0
        await on_progress(
            completed,
            total if total else None,
            float(speed) if speed else None,
        )
        if status["status"] == "complete":
            break
        if status["status"] == "removed":
            raise DownloadRemoved("aria2 下载已被移除")
        if status["status"] == "error":
            detail = status.get("error_message") or "error"
            raise RuntimeError(f"aria2 下载失败：{detail}")
        await asyncio.sleep(1)


async def _download_hf_file(
    name: str,
    filename: str,
    revision: str | None,
    dest: Path,
    endpoint: str,
    token: str | None,
    aria2: Aria2Client | None,
    connections: int,
    on_progress: ProgressCallback,
    on_log: LogCallback,
    retries: int,
) -> None:
    url = _hf_download_url(name, filename, revision, endpoint)
    file_dest = safe_path_under(Path(dest), filename)
    await on_log(f"正在下载 {filename}")

    token = optional_secret(token)
    auth_headers = {"Authorization": f"Bearer {token}"} if token else None
    aria2_headers = [f"Authorization: Bearer {token}"] if token else None

    if aria2 and await aria2.is_available():
        try:
            if token:
                await on_log("使用 aria2（带 HF Token）加速下载")
            await _download_with_aria2(
                url,
                file_dest,
                aria2,
                connections,
                on_progress,
                headers=aria2_headers,
                max_tries=max(1, retries + 1),
            )
            return
        except DownloadRemoved:
            raise
        except Exception as exc:
            await on_log(f"aria2 下载 {filename} 失败：{exc}；回退到 HTTP 断点续传")
            await http_download(
                url,
                file_dest,
                on_progress=on_progress,
                headers=auth_headers,
                retries=retries,
                on_log=on_log,
            )
            return

    await on_log("aria2 不可用；使用 HTTP 断点续传下载")
    await http_download(
        url,
        file_dest,
        on_progress=on_progress,
        headers=auth_headers,
        retries=retries,
        on_log=on_log,
    )


async def _repo_file_sizes(
    api: HfApi, name: str, revision: str | None, files: list[str]
) -> dict[str, int]:
    sizes: dict[str, int] = {}
    try:
        infos = await asyncio.to_thread(
            api.get_paths_info,
            repo_id=name,
            paths=files,
            revision=revision,
            repo_type="model",
        )
        for info in infos or []:
            path = getattr(info, "path", None) or getattr(info, "rfilename", None)
            size = getattr(info, "size", None)
            if path and isinstance(size, int) and size >= 0:
                sizes[path] = size
    except Exception:
        return {}
    return sizes


async def download_hf(
    name: str,
    dest: Path,
    *,
    endpoint: str,
    token: str | None,
    revision: str | None,
    aria2: Aria2Client | None,
    connections: int,
    on_progress: ProgressCallback,
    on_log: LogCallback,
    retries: int | None = None,
) -> None:
    token = optional_secret(token)
    if retries is None:
        retries = max(0, int(get_settings().download_retries))
    api = HfApi(endpoint=endpoint, token=token)
    files = await asyncio.to_thread(
        api.list_repo_files, repo_id=name, revision=revision, repo_type="model"
    )
    # Jail every remote filename before touching disk.
    for filename in files:
        safe_path_under(Path(dest), filename)

    await on_log(f"共 {len(files)} 个文件：{name}")
    sizes = await _repo_file_sizes(api, name, revision, files)
    total_known = sum(sizes.values()) if sizes and len(sizes) == len(files) else None
    completed_before = 0

    for index, filename in enumerate(files, start=1):
        file_path = safe_path_under(Path(dest), filename)
        expected = sizes.get(filename)
        if (
            expected is not None
            and file_path.exists()
            and file_path.is_file()
            and file_path.stat().st_size == expected
        ):
            await on_log(f"({index}/{len(files)}) 已存在，跳过 {filename}")
            completed_before += expected
            await on_progress(completed_before, total_known, 0.0)
            continue

        await on_log(f"({index}/{len(files)}) 正在下载 {filename}")

        async def file_progress(done: int, total: int | None, speed: float | None) -> None:
            overall_done = completed_before + max(done, 0)
            overall_total = total_known
            if overall_total is None and total:
                overall_total = completed_before + total
            await on_progress(overall_done, overall_total, speed)

        await _download_hf_file(
            name,
            filename,
            revision,
            Path(dest),
            endpoint,
            token,
            aria2,
            connections,
            file_progress,
            on_log,
            retries,
        )
        if filename in sizes:
            completed_before += sizes[filename]
        elif file_path.exists():
            completed_before += file_path.stat().st_size
        # When repo-wide size is unknown, keep total None so the UI stays indeterminate
        # instead of falsely jumping to 100% between files.
        await on_progress(completed_before, total_known, 0.0)

    await on_log(f"HF 下载完成：{name}")
