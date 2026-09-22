import asyncio
from pathlib import Path

from huggingface_hub import HfApi, hf_hub_url
from huggingface_hub.errors import HfHubHTTPError, RepositoryNotFoundError

from app.aria2_client import Aria2Client
from app.config import get_settings, optional_secret
from app.downloaders.base import (
    DownloadControl,
    DownloadRemoved,
    LogCallback,
    ProgressCallback,
)
from app.downloaders.progress import format_bytes
from app.downloaders.sdk_fallback import http_download
from app.paths import safe_path_under

# ``DownloadRemoved`` is re-exported here for backward compatibility: existing
# callers (and tests) import it from ``app.downloaders.hf``.
__all__ = ["hf_repo_exists", "download_hf", "DownloadRemoved"]


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
    last_total = 0
    while True:
        status = await aria2.tell_status(gid)
        total = status.get("total_length") or 0
        completed = status.get("completed_length") or 0
        speed = status.get("download_speed") or 0
        if total:
            last_total = total
        # aria2 reports totalLength=0 until the HEAD lands; carrying the last
        # known size keeps the caller's denominator from flickering to unknown.
        await on_progress(
            completed,
            last_total if last_total else None,
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
    use_aria2: bool,
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

    if use_aria2:
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
        except DownloadControl:
            # cancel / pause / aria2-removed must not be mistaken for an aria2
            # failure and silently retried over HTTP; propagate to the queue.
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


def _field(entry: object, *names: str) -> object:
    """Read the first present attribute/dict key (payload shape varies by SDK)."""
    if isinstance(entry, dict):
        for name in names:
            if entry.get(name) is not None:
                return entry[name]
        return None
    for name in names:
        value = getattr(entry, name, None)
        if value is not None:
            return value
    return None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _entry_size(entry: object) -> int | None:
    """Real blob size: the LFS record wins over the plain one.

    Depending on backend (LFS vs. Xet) and mirror age, ``size`` can be missing or
    carry the ~1 KB git-pointer length instead of the blob's real length. Taking
    the larger of ``size`` and ``lfs.size`` is what stopped a 200 GB repo from
    being totalled at a few megabytes, which made the bar run far ahead of the
    transfer. Anything unusable stays missing: no size beats a wrong size.
    """
    candidates = [
        size
        for size in (
            _non_negative_int(_field(entry, "size")),
            _non_negative_int(_field(_field(entry, "lfs"), "size", "size_bytes")),
        )
        if size is not None
    ]
    return max(candidates) if candidates else None


def _sizes_from_model_info(info: object) -> dict[str, int]:
    """``model_info(files_metadata=True)`` siblings carry the real LFS size."""
    sizes: dict[str, int] = {}
    try:
        siblings = list(getattr(info, "siblings", None) or [])
    except TypeError:  # unexpected payload shape: no sizes rather than no download
        return sizes
    for sibling in siblings:
        path = _field(sibling, "rfilename", "path")
        size = _entry_size(sibling)
        if isinstance(path, str) and size is not None:
            sizes[path] = size
    return sizes


def _sizes_from_paths_info(infos: object) -> dict[str, int]:
    sizes: dict[str, int] = {}
    try:
        entries = list(infos or [])
    except TypeError:
        return sizes
    for info in entries:
        path = _field(info, "path", "rfilename")
        size = _entry_size(info)
        if isinstance(path, str) and size is not None:
            sizes[path] = size
    return sizes


async def _repo_file_sizes(
    api: HfApi, name: str, revision: str | None, files: list[str]
) -> dict[str, int]:
    """Best-effort ``filename -> bytes`` map; partial results are kept.

    Two independent endpoints are queried because mirrors differ in which one
    they answer: ``model_info(files_metadata=True)`` is a single request that
    sizes every sibling, while ``get_paths_info`` is queried in batches (a big
    repo's single POST can be rejected wholesale, which used to throw the whole
    total away). Anything still missing is simply not counted in the total.
    """
    wanted = set(files)
    sizes: dict[str, int] = {}
    try:
        info = await asyncio.to_thread(
            api.model_info,
            repo_id=name,
            revision=revision,
            files_metadata=True,
        )
        sizes = {p: s for p, s in _sizes_from_model_info(info).items() if p in wanted}
    except Exception:
        sizes = {}
    if len(sizes) < len(wanted):
        missing = [path for path in files if path not in sizes]
        for start in range(0, len(missing), 500):
            batch = missing[start : start + 500]
            try:
                infos = await asyncio.to_thread(
                    api.get_paths_info,
                    repo_id=name,
                    paths=batch,
                    revision=revision,
                    repo_type="model",
                )
            except Exception:
                break
            for path, size in _sizes_from_paths_info(infos).items():
                if path in wanted:
                    sizes.setdefault(path, size)
    return sizes


# Weight files are the only thing that carries a model's bytes; everything else
# (config, tokenizer tables, README) is kilobytes.
_WEIGHT_SUFFIXES = (
    ".safetensors",
    ".bin",
    ".pt",
    ".pth",
    ".ckpt",
    ".gguf",
    ".onnx",
    ".h5",
    ".keras",
    ".msgpack",
    ".npz",
    ".tflite",
)


def _is_weight_file(path: str) -> bool:
    return path.lower().endswith(_WEIGHT_SUFFIXES)


# A supporting file we cannot size is priced at this instead of at "the average of
# whatever we do know": once a shard is in the sample, that average would bill 30
# JSONs for 30 shards' worth of bytes. One megabyte keeps the estimate honest in
# both directions (a tokenizer table is around that size) and the caller still
# refuses to show a full bar while any file remains unsized.
_SMALL_FILE_ESTIMATE = 1024 * 1024


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _projected_total(files: list[str], sizes: dict[str, int]) -> int:
    """Expected bytes overall, projecting the files the API could not size.

    Sized files count exactly. Each unsized file is projected at the *median* size
    of the known files of its own class (weights vs. supporting files), because
    averaging across classes lets one 5 GB shard price 30 config JSONs at 150 GB of
    phantom weight — the bar then read "a third done" on a 150 GB / 200 GB pull.
    Every unit is at least one byte so an unfinished repo can never divide to a
    full bar. A weight class with no sample yet falls back to the overall average
    until its first shard lands and supplies a real number; a support class with no
    sample is priced at ``_SMALL_FILE_ESTIMATE`` rather than at a shard's size.
    """
    known = sum(sizes.values())
    if not known:
        return 0
    unsized = [path for path in files if path not in sizes]
    if not unsized:
        return int(known)
    weights = [size for path, size in sizes.items() if _is_weight_file(path)]
    others = [size for path, size in sizes.items() if not _is_weight_file(path)]
    average = round(known / len(sizes)) if sizes else 0
    weight_unit = max(1, _median(weights) if weights else average)
    other_unit = max(1, _median(others) if others else _SMALL_FILE_ESTIMATE)
    projected = sum(
        weight_unit if _is_weight_file(path) else other_unit for path in unsized
    )
    return int(known + projected)


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
    # Denominator = every file we expect to land. Repo metadata covers it in the
    # normal case; a file the API could not size is folded in with its real size once
    # fetched, and while unsized files remain their share is projected per class (a
    # missing shard is priced like the shards we know, a missing JSON like the small
    # files we know). Both rules matter: an all-or-nothing total is what left the bar
    # stuck indeterminate on a partial listing, and a cross-class average is what
    # would inflate that total enough to keep the bar under half on a 150/200 GB pull.
    unsized_left = max(0, len(files) - len(sizes))
    expected_total = _projected_total(files, sizes)
    if expected_total:
        await on_log(f"预计总大小 {format_bytes(expected_total)}")
    completed_before = 0
    if expected_total:
        # Publish the denominator before the first transfer starts, so the bar has a
        # real percentage from the very first tick instead of sweeping "unknown"
        # through aria2's HEAD window.
        await on_progress(completed_before, expected_total, None)

    # Probe aria2 once per download instead of per file: an unreachable aria2 RPC
    # otherwise costs a (2s) connect timeout on every file before HTTP fallback.
    use_aria2 = aria2 is not None and await aria2.is_available()

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
            await on_progress(completed_before, expected_total or None, 0.0)
            continue

        await on_log(f"({index}/{len(files)}) 正在下载 {filename}")

        async def file_progress(done: int, total: int | None, speed: float | None) -> None:
            overall_done = completed_before + max(int(done or 0), 0)
            if not expected_total:
                # Nothing is known about the repo yet (no metadata, no finished file):
                # one file's size says nothing about the rest, so stay indeterminate
                # instead of reading "99 %" on shard 1 of 40.
                await on_progress(overall_done, None, speed)
                return
            overall_total = max(expected_total, overall_done)
            if total:
                # Metadata under-counts this file (or never sized it): widen the
                # denominator so the bar never claims more than it has moved.
                overall_total = max(overall_total, completed_before + int(total))
            if unsized_left:
                # Something still unmeasured may follow: a full bar would lie.
                overall_total = max(overall_total, overall_done + 1)
            await on_progress(overall_done, overall_total, speed)

        await _download_hf_file(
            name,
            filename,
            revision,
            Path(dest),
            endpoint,
            token,
            aria2,
            use_aria2,
            connections,
            file_progress,
            on_log,
            retries,
        )
        actual = sizes.get(filename)
        if actual is None:
            actual = file_path.stat().st_size if file_path.is_file() else 0
            sizes[filename] = actual
            unsized_left = max(0, unsized_left - 1)
        completed_before += actual
        # Re-project with the freshly learned size: it belongs to both sides of the
        # ratio, and it improves the average the remaining unsized files are judged by
        # (so an early over-estimate is corrected, not frozen in).
        expected_total = _projected_total(files, sizes)
        await on_progress(completed_before, expected_total or None, 0.0)

    await on_log(f"HF 下载完成：{name}")
