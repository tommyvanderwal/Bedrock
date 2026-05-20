"""Bedrock local state (/etc/bedrock/state.json)."""
import json
from pathlib import Path

STATE_FILE = Path("/etc/bedrock/state.json")


def load() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save(state: dict):
    # Atomic per-call: plain write_text races with concurrent readers
    # (rqlite_setup --render-env reads state.json on every rqlited
    # restart) and crash-truncation can leave a 0-byte file (observed
    # v30 5c — sim-1 rejoined, state.json went 0 bytes, bedrock-rqlited
    # crash-looped forever with "cannot render env yet — node_name=''
    # loopback_ip=''"). Tempfile + rename is atomic; readers never see
    # a partial file.
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    import os, tempfile
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{STATE_FILE.name}.", suffix=".tmp",
        dir=str(STATE_FILE.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(state, indent=2))
        os.replace(tmp_path, str(STATE_FILE))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
