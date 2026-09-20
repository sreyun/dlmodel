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
    if target == "ollama":
        if source not in ("auto", "ollama"):
            return [source]
        return ["ollama"]

    if source != "auto":
        return [source]

    # Prefer ModelScope when present, but always keep Hugging Face as download fallback.
    # A transient probe error must not fail resolution: treat "unknown" as present so
    # the source stays in the attempt order and the download loop can still fall back.
    order: list[str] = []
    if await _safe_exists(ms_exists, name):
        order.append("modelscope")
    if await _safe_exists(hf_exists, name):
        order.append("huggingface")
    if not order:
        return ["modelscope", "huggingface"]
    for candidate in ("modelscope", "huggingface"):
        if candidate not in order:
            order.append(candidate)
    return order


async def _safe_exists(probe, name: str) -> bool:
    try:
        return bool(await probe(name))
    except Exception:
        return True
