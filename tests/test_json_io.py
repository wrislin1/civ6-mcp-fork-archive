import json

from civ_mcp.json_io import read_json_file, write_json_file_atomic


def test_read_json_file_missing_returns_none(tmp_path):
    assert read_json_file(tmp_path / "missing.json") is None


def test_read_json_file_malformed_returns_none(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json")
    assert read_json_file(path) is None


def test_write_json_file_atomic_creates_parent_and_round_trips(tmp_path):
    path = tmp_path / "nested" / "data.json"

    write_json_file_atomic(path, {"a": 1, "b": [2, 3]})

    assert json.loads(path.read_text()) == {"a": 1, "b": [2, 3]}
    assert read_json_file(path) == {"a": 1, "b": [2, 3]}
    assert not path.with_name(path.name + ".tmp").exists()
