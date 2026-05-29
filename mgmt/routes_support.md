# mgmt/routes_support.py

Owns the dashboard's supportability endpoint. The `/support` page calls
`GET /api/support/checks`; this module attaches that route to the mgmt FastAPI
app and runs the checks. Every check is read-only and side-effect-free, so the
endpoint is safe to hit on every dashboard load. It lives in the asyncio mgmt
half of `bedrock-d` and takes its cluster/SSH helpers via dependency injection
from `mgmt/app.py` (rather than importing them) to avoid a circular import.

## Functions / Classes

### `register_routes(app, *, load_cluster, get_nodes, ssh_cmd_rc) -> None`
Attaches the `GET /api/support/checks` route to the given app.
- **In:**
  - `app` — the FastAPI app to register the route on.
  - `load_cluster` — callable returning the cluster dict (reads `witnesses`,
    `backup_targets`).
  - `get_nodes` — callable returning the per-node config dict, keyed by node
    name; each value carries `host` and `loopback_ip`.
  - `ssh_cmd_rc(host, cmd, timeout=...)` — callable that runs a shell command on
    a node over SSH and returns `(stdout, returncode)`.
- **Out:** `None`. Side effect: defines the inner `api_support_checks` handler
  on `app`.

### `GET /api/support/checks` (handler `api_support_checks()`)
Runs all supportability checks live and returns the results.
- **In:** none (no query params).
- **Out:** JSON `{"checks": [ ... ], "overall": <"ok"|"warn"|"fail">}`. Each
  check entry is `{"id", "label", "status", "note", "remediation"}` with
  `status` one of `ok`/`warn`/`fail`. Side effects: SSH commands to each node
  (read-only probes — `grep`, `systemctl is-enabled`, `ping -c1`, `lvs`); no
  files written, no rqlite writes.

## How it works

The handler builds a `checks` list by running six independent checks, then
computes a roll-up `overall`. It pulls cluster state once via `load_cluster()`
and the node map once via `get_nodes()`.

```
api_support_checks()
  ├─1 trim_stack    per-node SSH: lvm.conf thin_pool_discards passdown,
  │                 fstrim.timer is-enabled, count of DRBD .res files with
  │                 discard-zeroes-if-aligned
  ├─2 drbd_cable    each ordered pair (src,dst): ping dst.loopback_ip from src
  ├─3 witness       cluster["witnesses"] present?
  ├─4 advanced_mode always ok (placeholder)
  ├─5 backups       cluster["backup_targets"] present?
  ├─6 disk_fill     per-node SSH: lvs thin-pool data_percent
  └─ roll-up        fail > warn > ok
```

**1. TRIM / discard (`trim_stack`).** For each node with a `host`, runs one SSH
command that emits three `key=value` lines: `lvm_passdown` (grep of
`thin_pool_discards` from `/etc/lvm/lvm.conf`, or the literal `passdown_default`
when absent), `fstrim_timer` (`systemctl is-enabled fstrim.timer`, or
`missing`), and `drbd_discard` (count of `.res` files declaring
`discard-zeroes-if-aligned`). The lines are parsed into a `facts` dict. A node
is `ok` when its `lvm_passdown` either contains `passdown` or equals
`passdown_default`, AND `fstrim_timer` is `enabled` or `static`; otherwise it
lands in `warn`. A non-zero SSH return code or any exception puts the node in
`fail`. Overall check status: `fail` if any node failed to query, else `warn` if
any node is mis-configured, else `ok`. The DRBD-discard count is gathered but
does not gate the verdict.

**2. Dedicated DRBD path (`drbd_cable`).** Only meaningful at N>=2. For every
ordered pair of distinct nodes, SSHes to the source and `ping -c1 -W2`s the
destination's `loopback_ip`. A missing `loopback_ip`, a non-zero ping rc, or an
exception records an `src→dst` failure string. All pairs reachable -> `ok`; any
failure -> `fail` with the list of failed pairs and remediation to wire a
dedicated DRBD link. A single-node cluster yields `warn` ("DRBD path not yet
meaningful"). The probe targets each peer's loopback `/32` because bedrock-net
routes cluster-internal traffic to that address regardless of which NIC carries
it, so a single ping confirms the path end-to-end.

**3. Witness (`witness`).** Reads `cluster["witnesses"]`. Empty -> `warn`
(recommends `bedrock witness register <host>`). Non-empty -> `ok` with the
count.

**4. Advanced-mode overrides (`advanced_mode`).** Always appends an `ok` entry;
a placeholder slot for operator-override detection.

**5. Backups (`backups`).** Reads `cluster["backup_targets"]`. Present -> `ok`
listing the target keys. Absent -> `warn`.

**6. Thin-pool fill (`disk_fill`).** For each node with a `host`, runs
`lvs --noheadings -o lv_name,data_percent --separator='|' -S 'lv_role=thin,pool'`
and parses each pipe-split line into `(pool, data_percent)`. A pool at >=80 %
is recorded `level=alarm`; >=70 % is `level=warn`; non-numeric percentages and
SSH exceptions are skipped silently. No warnings -> `ok`. Otherwise the check
status is `fail` if any pool hit the alarm threshold, else `warn`, and the note
lists each offending `node/pool pct%`. Advisory only — Bedrock never blocks
operations on fill level.

**Roll-up.** `overall` walks the checks: any `fail` short-circuits to `fail`;
otherwise any `warn` makes it `warn`; all-`ok` stays `ok`.

## Why

Dependency injection (passing `load_cluster`, `get_nodes`, `ssh_cmd_rc` into
`register_routes`) keeps this module from importing `mgmt/app.py`, which would
create a circular import. All probes are read-only so the endpoint can be
re-run on every dashboard render without changing node state.
