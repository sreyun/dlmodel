from pathlib import Path

import pytest

from app.library import delete_model, scan_hf_library


def test_scan_and_delete(tmp_path: Path):
    dest = tmp_path / "hf" / "Org" / "Mod"
    dest.mkdir(parents=True)
    (dest / "config.json").write_text("{}")
    items = scan_hf_library(str(tmp_path))
    assert len(items) == 1
    assert items[0]["name"] == "Org/Mod"
    delete_model(str(tmp_path), items[0]["id"])
    assert scan_hf_library(str(tmp_path)) == []


def test_delete_rejects_traversal(tmp_path: Path):
    with pytest.raises(ValueError):
        delete_model(str(tmp_path), "../etc/passwd")
