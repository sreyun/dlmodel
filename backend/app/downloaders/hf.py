import asyncio
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_url
from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError

from app.aria2_client import Aria2Client
from app.config import optional_secret
from app.downloaders.base import LogCallback, ProgressCallback
from app.downloaders.sdk_fallback import http_download


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
) -> None:
    file_dest.parent.mkdir(parents=True, exist_ok=True)
    gid = await aria2.add_uri(
        [url],
        out_dir=str(file_dest.parent),
        out_name=file_dest.name,
        connections=connections,
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
        if status["status"] in ("error", "removed"):
            raise RuntimeError(f"aria2 returned status {status['status']}")
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
) -> None:
    url = _hf_download_url(name, filename, revision, endpoint)
    file_dest = dest / filename
    await on_log(f"正在下载 {filename}")

    token = optional_secret(token)
    auth_headers = {"Authorization": f"Bearer {token}"} if token else None

    if token:
        await on_log("HF token set; using authenticated HTTP download")
        await http_download(
            url, file_dest, on_progress=on_progress, headers=auth_headers
        )
    elif aria2 and await aria2.is_available():
        try:
            await _download_with_aria2(
                url, file_dest, aria2, connections, on_progress
            )
            return
        except Exception as exc:
            await on_log(
                f"aria2 下载 {filename} 失败：{exc}；回退到 HTTP"
            )
            await http_download(url, file_dest, on_progress=on_progress)
    else:
        await on_log("aria2 不可用；使用 SDK HTTP 回退下载")
        await http_download(url, file_dest, on_progress=on_progress)


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
) -> None:
    token = optional_secret(token)
    api = HfApi(endpoint=endpoint, token=token)
    files = await asyncio.to_thread(
        api.list_repo_files, repo_id=name, revision=revision, repo_type="model"
    )
    await on_log(f"Found {len(files)} file(s) in {name}")

    for filename in files:
        await _download_hf_file(
            name,
            filename,
            revision,
            Path(dest),
            endpoint,
            token,
            aria2,
            connections,
            on_progress,
            on_log,
        )

    await on_log(f"HF 下载完成：{name}")
