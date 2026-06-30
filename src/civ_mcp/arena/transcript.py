from __future__ import annotations
import json, os
class TranscriptSink:
    def __init__(self, path: str): self.path = path
    def write(self, record: dict) -> None:
        with open(self.path, "a") as f: f.write(json.dumps(record) + "\n")
    @classmethod
    def for_run(cls, run_id: str, base: str = "arena_runs") -> "TranscriptSink":
        d = os.path.join(base, run_id); os.makedirs(d, exist_ok=True)
        return cls(os.path.join(d, "transcript.jsonl"))
class NullSink:
    def write(self, record: dict) -> None: pass
