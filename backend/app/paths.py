import errno
import shutil
from pathlib import Path


def parse_model_name(name: str) -> tuple[str, str]:
    raw = name.strip()
    if not raw:
        raise ValueError("模型名称无效")
    if len(raw) > 256:
        raise ValueError("模型名称过长")
    if Path(raw).is_absolute() or raw.startswith(("/", "\\")):
        raise ValueError("拒绝路径穿越")
    if ".." in Path(raw).parts:
        raise ValueError("拒绝路径穿越")

    cleaned = raw.strip("/\\")
    parts = [part for part in cleaned.replace("\\", "/").split("/") if part]
    if not parts:
        raise ValueError("模型名称无效")
    if ".." in parts:
        raise ValueError("拒绝路径穿越")
    if any(Path(part).is_absolute() for part in parts):
        raise ValueError("拒绝路径穿越")
    if len(parts) == 1:
        return "library", parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise ValueError("模型名称无效")


def hf_model_dir(model_root: str, name: str) -> Path:
    org, repo = parse_model_name(name)
    root = Path(model_root).resolve()
    constructed = Path(model_root) / "hf" / org / repo
    if not constructed.resolve().is_relative_to(root):
        raise ValueError("拒绝路径穿越")
    return constructed


def ollama_root(model_root: str) -> Path:
    return Path(model_root) / "ollama"


def safe_path_under(root: Path, *parts: str) -> Path:
    """Join parts under root and reject absolute / .. escapes."""
    root = Path(root).resolve()
    cleaned: list[str] = []
    for part in parts:
        raw = str(part)
        if not raw or raw.strip() == "":
            continue
        # Reject absolute / drive paths before stripping separators.
        if Path(raw).is_absolute() or raw.startswith(("/", "\\")) or (
            len(raw) >= 2 and raw[1] == ":"
        ):
            raise ValueError(f"拒绝路径穿越：{part}")
        text = raw.replace("\\", "/").strip("/")
        if not text:
            continue
        for segment in text.split("/"):
            if not segment or segment in (".", ".."):
                raise ValueError(f"拒绝路径穿越：{part}")
            if Path(segment).is_absolute():
                raise ValueError(f"拒绝路径穿越：{part}")
            cleaned.append(segment)
    if not cleaned:
        raise ValueError("路径无效")
    candidate = (root.joinpath(*cleaned)).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"拒绝路径穿越：{'/'.join(cleaned)}")
    return candidate


def ensure_under_model_root(model_root: str, path: str | Path) -> Path:
    root = Path(model_root).resolve()
    target = Path(path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("目标路径超出 MODEL_ROOT")
    return target


def free_space_bytes(path: str | Path) -> int:
    """Free bytes on the filesystem backing ``path`` (nearest existing ancestor).

    Returns -1 when it cannot be determined, so callers treat that as unknown.
    """
    probe = Path(path)
    while probe.parent != probe and not probe.exists():
        probe = probe.parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return -1


def disk_error_to_message(exc: OSError, path: str | Path) -> str | None:
    """Map a write/permission OSError to a friendly, path-hinted message.

    Returns ``None`` for unrelated OSErrors so callers keep their own handling.
    """
    code = getattr(exc, "errno", None)
    if code in (errno.ENOSPC, errno.EDQUOT):
        return f"磁盘空间不足，无法写入 {path}（请清理 MODEL_ROOT 后重试）"
    if code in (errno.EACCES, errno.EPERM, errno.EROFS):
        return f"目标路径无写入权限：{path}"
    return None
