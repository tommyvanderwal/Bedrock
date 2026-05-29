"""Bedrock local state (/etc/bedrock/state.json).

Beyond the per-node identity fields (node_name, loopback_ip,
cluster_uuid, bootstrap_done, …) this file also carries two cluster
base-layer facts that must survive reboot and need no rqlite (they are
what *recovers* rqlite — see EXECUTION-PLAN BAD-1):

  * believed_master — who THIS node last believed was mgmt master.
    Read on cold boot before rqlite quorum exists.
  * arbiter_uuid_history — a local, 7-day rolling log of the arbiter
    (`cluster` singleton) DRBD current-UUIDs this node has observed,
    newest last. Each entry is {uuid, ts_seen, ts_superseded}. This is
    the split-brain guard: an election candidate advertises its arbiter
    UUID, and a voter classifies it against its own history —
    superseded => REFUSE (stale), current/unseen => votable. Capped at
    7 days so a long-dead generation can't veto forever.
"""
import json
import time
from pathlib import Path

STATE_FILE = Path("/etc/bedrock/state.json")

# How long a UUID observation is retained for eligibility decisions.
UUID_HISTORY_RETENTION_S = 7 * 24 * 3600

# Eligibility classifications for a candidate's advertised arbiter UUID.
UUID_CURRENT = "current"        # newest UUID we've recorded — votable
UUID_UNSEEN = "unseen"          # never seen in the last 7 days — assume
                                # newer than anything we know — votable
UUID_SUPERSEDED = "superseded"  # seen, but a later UUID superseded it — REFUSE


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
            f.flush()
            os.fsync(f.fileno())          # data durable BEFORE the rename
        os.replace(tmp_path, str(STATE_FILE))
        # fsync the directory so the rename itself survives a crash.
        # Without this, an unclean reboot can leave a 0-byte state.json
        # (the rename is journaled but the tmp file's data blocks aren't)
        # — observed on sim-1 2026-05-29 after a master reboot mid-sync,
        # which then deadlocked rqlited's env-render. tmp+rename is atomic
        # vs concurrent readers; fsync makes it durable vs power loss.
        _dfd = os.open(str(STATE_FILE.parent), os.O_DIRECTORY)
        try:
            os.fsync(_dfd)
        finally:
            os.close(_dfd)
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


# ─────────────────────────────────────────────────────────────────
#  Believed-master (survives reboot; cold-boot reads it before rqlite)
# ─────────────────────────────────────────────────────────────────

def get_believed_master(state: dict | None = None) -> str | None:
    """Who this node last believed was the mgmt master, or None."""
    if state is None:
        state = load()
    return state.get("believed_master") or None


def set_believed_master(node_name: str | None,
                        state: dict | None = None) -> dict:
    """Persist the believed master. Returns the saved dict. Pass a
    pre-loaded `state` to fold this into a larger update without an
    extra read."""
    if state is None:
        state = load()
    state["believed_master"] = node_name or None
    save(state)
    return state


# ─────────────────────────────────────────────────────────────────
#  Arbiter-DRBD UUID history (local, 7-day, drives eligibility)
# ─────────────────────────────────────────────────────────────────

def _normalize_uuid(uuid: str) -> str:
    """Lower-case, strip an 0x prefix and surrounding whitespace so
    UUIDs compare equal regardless of how the source formatted them
    (debugfs gives 0x…, dump-md gives 0x…;, the witness marker is the
    bare hex). Matches cluster_arbiter._read_local_drbd_uuid()."""
    u = (uuid or "").strip().lower()
    if u.startswith("0x"):
        u = u[2:]
    return u.rstrip(";")


def _prune_history(history: list[dict], now: float) -> list[dict]:
    """Drop observations older than the 7-day window. An entry's age is
    measured from ts_superseded if set (the generation is over), else
    from ts_seen (still possibly current). The newest entry is always
    kept so the current generation never expires out from under us."""
    cutoff = now - UUID_HISTORY_RETENTION_S
    pruned: list[dict] = []
    for i, e in enumerate(history):
        is_newest = i == len(history) - 1
        age_ts = e.get("ts_superseded") or e.get("ts_seen") or 0
        if is_newest or age_ts >= cutoff:
            pruned.append(e)
    return pruned


def record_arbiter_uuid(uuid: str, state: dict | None = None,
                        now: float | None = None) -> dict:
    """Record an observation of the arbiter (`cluster` singleton) DRBD
    current-UUID, newest last. If `uuid` matches the current newest
    entry it's a no-op (just refreshes nothing); a *different* UUID
    supersedes the previous newest (stamps its ts_superseded) and is
    appended as the new current generation. Prunes >7-day-old entries.
    Persists and returns the saved dict.

    No-ops on an empty/blank uuid (early boot, no DRBD yet)."""
    if state is None:
        state = load()
    norm = _normalize_uuid(uuid)
    if not norm:
        return state
    if now is None:
        now = time.time()
    history: list[dict] = list(state.get("arbiter_uuid_history") or [])
    if history and history[-1].get("uuid") == norm:
        # Already current — nothing changes.
        return state
    if history:
        history[-1]["ts_superseded"] = now
    history.append({"uuid": norm, "ts_seen": now, "ts_superseded": None})
    state["arbiter_uuid_history"] = _prune_history(history, now)
    save(state)
    return state


def classify_arbiter_uuid(uuid: str, state: dict | None = None,
                          now: float | None = None) -> str:
    """Classify a candidate's advertised arbiter UUID against THIS
    node's local 7-day history. Returns one of UUID_CURRENT,
    UUID_UNSEEN, UUID_SUPERSEDED.

      * matches the newest recorded entry -> CURRENT  (votable)
      * not present in the (pruned) history at all -> UNSEEN
        (assume newer than anything we know -> votable)
      * present but a later UUID superseded it -> SUPERSEDED (REFUSE)

    A blank candidate UUID is treated as UNSEEN (nothing to refuse on).
    Pure read — does not mutate or persist state."""
    if state is None:
        state = load()
    norm = _normalize_uuid(uuid)
    if not norm:
        return UUID_UNSEEN
    if now is None:
        now = time.time()
    history = _prune_history(list(state.get("arbiter_uuid_history") or []), now)
    if not history:
        return UUID_UNSEEN
    if history[-1].get("uuid") == norm:
        return UUID_CURRENT
    if any(e.get("uuid") == norm for e in history):
        return UUID_SUPERSEDED
    return UUID_UNSEEN


def is_uuid_eligible(uuid: str, state: dict | None = None,
                     now: float | None = None) -> bool:
    """True iff a candidate advertising `uuid` is eligible to win our
    vote: current or unseen are votable, superseded is refused."""
    return classify_arbiter_uuid(uuid, state, now) != UUID_SUPERSEDED
