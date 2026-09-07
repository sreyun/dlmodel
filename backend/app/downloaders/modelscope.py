import asyncio
from pathlib import Path

import requests

from modelscope import snapshot_download
from modelscope.hub.api import HubApi

from app.downloaders.base import LogCallback, ProgressCallback


async def ms_repo_exists(name: str, token: str | None) -> bool:
    api = HubApi(token=token)
    try:
        return await asyncio.to_thread(
            api.repo_exists, repo_id=name, repo_type="model", re_raise=True
        )
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code == 404:
            return False
        raise


async def download_modelscope(
    name: str,
    dest: Path,
    *,
    token: str | None,
    revision: str | None,
    on_progress: ProgressCallback,
    on_log: LogCallback,
) -> None:
    await on_log(f"Starting ModelScope download for {name}")

    def _download() -> str:
        return snapshot_download(
            model_id=name,
            revision=revision,
            local_dir=str(dest),
            token=token,
        )

    result = await asyncio.to_thread(_download)
    await on_log(f"ModelScope download complete: {result}")
