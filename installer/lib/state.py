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
    #
    # Defensive: refuse to write a state dict that lacks the
    # bootstrap-essentials. A caller that loaded state.json (got an
    # empty dict because the file was missing/0-bytes) and then
    # save()'d the empty dict would persist the corruption. Better
    # to raise loudly so the operator sees something is wrong than
    # to silently turn a corrupt state.json into a corrupt-but-now-
    # 2-byte state.json. Recurrence of this happened on sim-4
    # 2026-05-26 during a sync-to-sims --restart cycle; the
    # subsequent bedrock-rqlited crash-loop hid the original cause.
    if not state.get("bootstrap_done") and not state.get("node_name"):
        import sys as _sys
        raise RuntimeError(
            "state.save: refusing to write state without "
            "bootstrap_done or node_name — would corrupt state.json. "
            f"Caller stack: {_summarize_stack()}"
        )
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


def _summarize_stack() -> str:
    """Caller-stack snippet for the empty-save trap. Three frames
    above this helper's caller is usually enough to identify the
    saga step / orchestrator path that mis-saved."""
    import traceback
    frames = traceback.extract_stack()[-5:-2]
    return " <- ".join(f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno}"
                       for f in frames)
