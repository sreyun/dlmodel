from pathlib import Path


def parse_model_name(name: str) -> tuple[str, str]:
    raw = name.strip()
    if not raw:
        raise ValueError("模型名称无效")
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
