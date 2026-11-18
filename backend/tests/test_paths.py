from pathlib import Path

import pytest

from app.paths import hf_model_dir, parse_model_name, safe_path_under, validate_ollama_ref


def test_parse_model_name_with_org():
    assert parse_model_name("Qwen/Qwen2.5-7B-Instruct") == ("Qwen", "Qwen2.5-7B-Instruct")


def test_parse_model_name_without_org():
    assert parse_model_name("llama3.2") == ("library", "llama3.2")


def test_hf_model_dir(tmp_path: Path):
    p = hf_model_dir(str(tmp_path), "Qwen/Qwen2.5-7B-Instruct")
    assert p == tmp_path / "hf" / "Qwen" / "Qwen2.5-7B-Instruct"


@pytest.mark.parametrize(
    "name",
    [
        "../etc/passwd",
        "foo/../../../tmp/x",
        "/etc/passwd",
        "org/../../outside",
    ],
)
def test_parse_model_name_rejects_traversal(name: str):
    with pytest.raises(ValueError, match="拒绝路径穿越"):
        parse_model_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "../etc/passwd",
        "foo/../../../tmp/x",
        "/etc/passwd",
    ],
)
def test_hf_model_dir_rejects_traversal(tmp_path: Path, name: str):
    with pytest.raises(ValueError, match="拒绝路径穿越"):
        hf_model_dir(str(tmp_path), name)


def test_safe_path_under_allows_nested(tmp_path: Path):
    target = safe_path_under(tmp_path, "config.json")
    assert target == (tmp_path / "config.json").resolve()
    nested = safe_path_under(tmp_path, "folder/weights.bin")
    assert nested == (tmp_path / "folder" / "weights.bin").resolve()


@pytest.mark.parametrize(
    "filename",
    ["../x", "/etc/passwd", "..\\x", "a/../../b"],
)
def test_safe_path_under_rejects_escape(tmp_path: Path, filename: str):
    with pytest.raises(ValueError, match="拒绝路径穿越"):
        safe_path_under(tmp_path, filename)


@pytest.mark.parametrize(
    "name",
    [
        "llama3.2",
        "llama3.2:3b",
        "library/llama3.2:latest",
        "registry.ollama.ai/library/llama3.2:latest",
        "hf.co/bartowski/Llama-3.2-1B-Instruct-GGUF:Q4_K_M",
        "localhost:5000/team/model",
    ],
)
def test_validate_ollama_ref_accepts_ollama_grammar(name: str):
    validate_ollama_ref(name)


@pytest.mark.parametrize(
    "name",
    ["", "  ", "../etc", "a/../b", "/abs", "a//b", "a/b/c/d", "a\\b", "a b", "a\nb", "x" * 257],
)
def test_validate_ollama_ref_rejects(name: str):
    with pytest.raises(ValueError):
        validate_ollama_ref(name)
