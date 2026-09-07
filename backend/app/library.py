import shutil
from pathlib import Path


def _dir_size_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _resolve_model_path(model_root: str, model_id: str) -> Path:
    if ".." in Path(model_id).parts:
        raise ValueError("path traversal rejected")

    parts = model_id.strip("/\\").split("/")
    if len(parts) != 3 or parts[0] != "hf":
        raise ValueError("invalid model id")

    root = Path(model_root).resolve()
    target = (root / Path(*parts)).resolve()
    if not target.is_relative_to(root):
        raise ValueError("path traversal rejected")
    return target


def scan_hf_library(model_root: str) -> list[dict]:
    hf_root = Path(model_root) / "hf"
    if not hf_root.is_dir():
        return []

    items: list[dict] = []
    for org_dir in sorted(hf_root.iterdir()):
        if not org_dir.is_dir():
            continue
        for repo_dir in sorted(org_dir.iterdir()):
            if not repo_dir.is_dir():
                continue
            name = f"{org_dir.name}/{repo_dir.name}"
            items.append(
                {
                    "id": f"hf/{name}",
                    "name": name,
                    "path": str(repo_dir),
                    "target": "vllm",
                    "size_bytes": _dir_size_bytes(repo_dir),
                }
            )
    return items


def delete_model(model_root: str, model_id: str) -> None:
    target = _resolve_model_path(model_root, model_id)
    if not target.exists():
        raise FileNotFoundError(model_id)
    shutil.rmtree(target)
