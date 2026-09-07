from pathlib import Path
from app.paths import parse_model_name, hf_model_dir


def test_parse_model_name_with_org():
    assert parse_model_name("Qwen/Qwen2.5-7B-Instruct") == ("Qwen", "Qwen2.5-7B-Instruct")


def test_parse_model_name_without_org():
    assert parse_model_name("llama3.2") == ("library", "llama3.2")


def test_hf_model_dir(tmp_path: Path):
    p = hf_model_dir(str(tmp_path), "Qwen/Qwen2.5-7B-Instruct")
    assert p == tmp_path / "hf" / "Qwen" / "Qwen2.5-7B-Instruct"
