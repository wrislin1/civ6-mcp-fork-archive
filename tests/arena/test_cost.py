import json
from civ_mcp.arena.cost import CostLog

def test_record_and_summary(tmp_path):
    p = tmp_path / "cost.jsonl"
    log = CostLog(str(p))
    log.record(player_id=1, model="qwen3-coder-30b", provider="local",
               prompt_tokens=100, completion_tokens=20, turn=2)
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["provider"] == "local" and rec["usd"] == 0.0
    s = log.summary()
    assert s["by_player"][1]["prompt_tokens"] == 100
    assert s["total_usd"] == 0.0
