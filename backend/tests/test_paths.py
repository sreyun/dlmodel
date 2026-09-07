from pathlib import Path

import pytest

from app.paths import parse_model_name, hf_model_dir


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
