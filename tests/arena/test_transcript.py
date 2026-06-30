import json
import os
from civ_mcp.arena.transcript import TranscriptSink, NullSink

def test_write_appends_jsonl(tmp_path):
    """Test that write() appends valid JSONL records."""
    p = tmp_path / "transcript.jsonl"
    sink = TranscriptSink(str(p))

    record1 = {"turn": 1, "action": "move"}
    record2 = {"turn": 2, "action": "build"}

    sink.write(record1)
    sink.write(record2)

    lines = p.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == record1
    assert json.loads(lines[1]) == record2

def test_for_run_makes_dir_and_returns_sink(tmp_path):
    """Test that for_run() creates the directory and returns a sink with correct path."""
    os.chdir(str(tmp_path))

    run_id = "test_run_123"
    sink = TranscriptSink.for_run(run_id)

    # Check that the directory was created
    assert os.path.isdir(os.path.join("arena_runs", run_id))

    # Check that the sink has the correct path
    expected_path = os.path.join("arena_runs", run_id, "transcript.jsonl")
    assert sink.path == expected_path

def test_null_sink_writes_nothing(tmp_path):
    """Test that NullSink.write() does nothing."""
    os.chdir(str(tmp_path))

    null_sink = NullSink()
    null_sink.write({"turn": 1, "action": "test"})

    # No files should be created
    assert not os.path.exists("arena_runs")
