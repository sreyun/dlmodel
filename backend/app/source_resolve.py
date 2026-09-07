import re

_OLLAMA_LIBRARY = re.compile(r"^[a-z0-9._-]+(:[a-z0-9._-]+)?$")


def looks_like_ollama_library(name: str) -> bool:
    name = name.strip()
    if "/" in name:
        return False
    return bool(_OLLAMA_LIBRARY.match(name))


async def resolve_source(
    name: str,
    source: str,
    target: str,
    *,
    ms_exists,
    hf_exists,
) -> list[str]:
    if source != "auto":
        return [source]

    if target == "ollama" and looks_like_ollama_library(name):
        return ["ollama"]

    if await ms_exists(name):
        return ["modelscope"]
    if await hf_exists(name):
        return ["huggingface"]
    return ["modelscope", "huggingface"]
