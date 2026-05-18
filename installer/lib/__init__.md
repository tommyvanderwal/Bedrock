# `installer/lib/` — Python module package

**Package purpose.** Bedrock's installer + cluster-protocol
library. Every Python file here either:

- runs at `bedrock bootstrap / init / join / storage / node …`
  CLI time (`mgmt_install`, `agent_install`, `tier_storage`,
  etc.), OR
- runs inside the `bedrock-net` daemon (`netd`, `election`,
  `witness`, `l2disc`, `mdns_responder`), OR
- runs inside `bedrock-mgmt.service` via the `mgmt/` orchestrator
  (`view_builder`, `bedrock_state`, `rqlite_client`,
  `cluster_arbiter`, `seaweedfs`, `observability`,
  `cert_manager`, `peer_auth`, `operator_auth`, `join_handshake`),
  OR
- is run by a small systemd unit (`bedrock-redirect`,
  `bedrock-mdns`, `bedrock-cert-refresh.timer` exec script).

The `__init__.py` is empty; module discovery is via Python's
normal package mechanics.

Each `*.py` file has a companion `*.md` next to it with the
module's purpose statement + one-paragraph-per-function
descriptions. The high-level state-flow doc that ties the
modules together lives in `docs/state-flow.md`.
