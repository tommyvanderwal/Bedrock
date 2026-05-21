"""Cluster rename saga.

Single-step saga that updates the display tag (``cluster_name``) in
rqlite's ``cluster_info`` row. The cluster's real identity is
``cluster_uuid`` — immutable for the cluster's life — so this is a
pure rename, not a re-identity.

rqlite is the single writer. Every node's rqlite_subscriber picks
up the new revision within ~2 s and re-projects:

  * ``/etc/bedrock/cluster.json`` (via ``view_builder._cluster_view``)
  * ``/etc/bedrock/state.json``   (via ``view_builder._state_view``)

The mDNS responder re-reads state.json every ~60 s, so the TXT
record's ``cluster_name`` field reflects the new value within that
window. Nothing else needs to be touched — daemons, services, DRBD,
SeaweedFS all key off ``cluster_uuid``, not the name.
"""
from __future__ import annotations

import logging
import re

from bedrock_d.orchestrator.sagas import saga, step

log = logging.getLogger(__name__)

# Allowed-name policy: 1..64 chars, ascii letters/digits/dash/underscore/dot.
# Avoids characters that would need escaping in cluster.json, the mDNS TXT
# record, shell-rendered log lines, or systemd unit paths. Operators who
# want to display something fancier can format the name client-side from
# `cluster_uuid` — that's why this row is a tag and not an identity.
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


@saga("cluster_rename")
class ClusterRename:
    """Rename the cluster's display tag.

    ctx inputs (set by the caller / ``bedrock cluster rename``):
      - new_name: str   (1..64 ASCII chars, [A-Za-z0-9_.-])

    No ctx outputs.
    """

    @step("validate_request")
    def step_validate_request(self, ctx):
        """Refuse empty / too-long / unsafe names. Cheap; runs first
        so we fail before touching rqlite."""
        new_name = (ctx.get("new_name") or "").strip()
        if not new_name:
            raise ValueError("new_name is required and must be non-empty")
        if not _NAME_PATTERN.fullmatch(new_name):
            raise ValueError(
                f"new_name {new_name!r} doesn't match [A-Za-z0-9_.-]{{1,64}}"
            )
        ctx["new_name"] = new_name   # canonicalised (stripped)

    @step("write_rqlite_cluster_info")
    def step_write_rqlite_cluster_info(self, ctx):
        """Single UPDATE against ``cluster_info``. Bumps
        ``bedrock_meta.revision`` so every subscriber wakes and the
        new name lands in cluster.json + state.json across the
        cluster.

        Idempotent: re-running with the same name is a no-op write
        at the rqlite level (UPDATE … WHERE cluster_name != ? is
        an option if we want to skip the revision bump too; today
        we always bump, which is harmless — at most one extra
        subscriber tick)."""
        from bedrock_d import state as _st
        with _st.RqliteClient() as client:
            rev = _st.set_cluster_name(ctx["new_name"], client=client)
        log.info(
            "cluster_rename: cluster_name=%r recorded (rqlite rev=%s)",
            ctx["new_name"], rev)
