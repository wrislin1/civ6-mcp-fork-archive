from __future__ import annotations
import json
from collections import defaultdict

# USD per 1k tokens (prompt, completion). Local = free. Extend in the next plan.
PRICES = {"local": (0.0, 0.0)}

class CostLog:
    def __init__(self, path: str):
        self.path = path
        self._records: list[dict] = []

    def _usd(self, provider, model, pt, ct) -> float:
        pin, pout = PRICES.get(provider, (0.0, 0.0))
        return round(pt / 1000 * pin + ct / 1000 * pout, 6)

    def record(self, player_id, model, provider, prompt_tokens, completion_tokens, turn, usd=None):
        rec = {
            "turn": turn, "player_id": player_id, "provider": provider, "model": model,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
            "usd": self._usd(provider, model, prompt_tokens, completion_tokens) if usd is None else round(float(usd), 6),
        }
        self._records.append(rec)
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def summary(self) -> dict:
        by_player: dict = defaultdict(lambda: {"prompt_tokens": 0, "completion_tokens": 0, "usd": 0.0})
        total = 0.0
        for r in self._records:
            bp = by_player[r["player_id"]]
            bp["prompt_tokens"] += r["prompt_tokens"]
            bp["completion_tokens"] += r["completion_tokens"]
            bp["usd"] += r["usd"]
            total += r["usd"]
        return {"by_player": dict(by_player), "total_usd": round(total, 6)}
