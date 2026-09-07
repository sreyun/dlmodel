from pathlib import Path


def parse_model_name(name: str) -> tuple[str, str]:
    name = name.strip().strip("/")
    if "/" in name:
        org, repo = name.split("/", 1)
        return org, repo
    return "library", name


def hf_model_dir(model_root: str, name: str) -> Path:
    org, repo = parse_model_name(name)
    return Path(model_root) / "hf" / org / repo


def ollama_root(model_root: str) -> Path:
    return Path(model_root) / "ollama"
