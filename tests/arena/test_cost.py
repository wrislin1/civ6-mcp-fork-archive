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

def test_usd_override(tmp_path):
    log = CostLog(str(tmp_path / "c.jsonl"))
    log.record(player_id=2, model="claude", provider="cli-claude",
               prompt_tokens=1000, completion_tokens=200, turn=3, usd=0.0123)
    s = log.summary()
    assert s["by_player"][2]["usd"] == 0.0123
    assert s["total_usd"] == 0.0123

def test_cli_codex_pricing_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CIV_ARENA_CLI_CODEX_PROMPT_USD_PER_1K", "0.01")
    monkeypatch.setenv("CIV_ARENA_CLI_CODEX_COMPLETION_USD_PER_1K", "0.02")
    log = CostLog(str(tmp_path / "codex.jsonl"))
    log.record(player_id=2, model="gpt-5.5", provider="cli-codex",
               prompt_tokens=2000, completion_tokens=500, turn=4)
    s = log.summary()
    assert s["by_player"][2]["usd"] == 0.03
    assert s["total_usd"] == 0.03
