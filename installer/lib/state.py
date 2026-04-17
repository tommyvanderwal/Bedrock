"""Bedrock local state (/etc/bedrock/state.json)."""
import json
from pathlib import Path

STATE_FILE = Path("/etc/bedrock/state.json")


def load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))
