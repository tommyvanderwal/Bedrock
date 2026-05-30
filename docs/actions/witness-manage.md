# Manage cluster witnesses

A **witness** is an external tiebreaker for the weighted-vote quorum: each node
is worth 100 votes, each valid witness 1. It matters only when the nodes alone
can't decide a majority — a **2-node cluster** (100 vs 100 needs the +1) or an
**even split** of a larger cluster. A 3+ node cluster that never splits evenly
can fail over without one. A witness only ever makes failover *safer*: a
configured-but-unreachable witness raises the bar by one vote it can't supply,
which biases toward "do not fail over" (never toward split-brain).

Two backends are supported:

- **BedRock Echo** — a tiny UDP appliance (ESP32 firmware, or
  `testbed/bedrock_echo_stub.py`) on UDP/12321. Stores one encrypted slot per
  node, returns them all on every reply. The canonical witness.
- **Fileshare** — a directory you have mounted the *same* NFS/SMB/object share
  at on **every** node. Bedrock writes one `slot-<NN>.bin` per node there and
  reads the others'. No appliance needed; reuses storage you already have.

**Triggered by:**

- Dashboard: `/witness` page → *Add witness* form, or *Scan LAN* for Echoes
- CLI: `bedrock witness add|list|rm` (writes the rqlite `witnesses` table on the
  master; Raft replicates; each node's `bedrock-net` election tick picks up the
  change on its next 1 Hz pass)
- HTTP: `POST /api/witnesses`, `GET /api/witnesses`,
  `DELETE /api/witnesses/{id}`, `GET /api/witnesses/discover`

**Source:** `mgmt/app.py:api_witness_add` / `_api_witness_add_fileshare` /
`api_witnesses_discover`; `installer/bedrock:cmd_witness`;
`installer/lib/witness.py` (slot protocol, `count_valid_confirmed`),
`installer/lib/witness_file.py` (fileshare backend),
`installer/lib/discovery.py:discover_echo_witnesses` (mDNS),
`installer/lib/netd.py` (election tick + off-hot-path fileshare worker).

## Add an Echo witness

By IP (works across subnets — netd directed-unicast-probes it) or on the local
L2 (zero-config broadcast discovery also finds it):

```
bedrock witness add office-echo 10.0.0.9:12321 <64-hex-x25519-pubkey>
```

- `witness_id` (`office-echo`) is a friendly name. **By convention it must equal
  the Echo's `echo_id`** — netd counts a reply's vote ONLY if its `echo_id`
  matches a configured `witness_id` (the identity binding that stops a rogue or
  a just-removed Echo from voting). Provision the Echo with `echo_id == the id
  you add here`.
- The address must be an **IPv4 unicast literal** (`host` or `host:port`,
  default port 12321). A hostname is refused — netd probes from the
  single-threaded 1 Hz election tick and a synchronous DNS lookup would stall
  failover detection. Multicast / broadcast / IPv6 are refused too.
- The pubkey is the Echo's 64-hex (32-byte) X25519 public key.

**Scan LAN (mDNS):** on the `/witness` page, *Scan LAN* multicast-queries
`bedrock-echo.local` and lists every Echo that answers, with its id and (if
advertised) pubkey. *Use* prefills id + address + pubkey for a one-click add.
An Echo on a routed segment that doesn't answer multicast is still added by IP.

## Add a fileshare witness

1. Mount the *same* share at the *same* path on **every** node (e.g.
   `/mnt/witness`) via your normal mechanism (`/etc/fstab` NFS/CIFS, an
   object-store FUSE, …). It must be present **and writable on every node**.
2. Add it:

```
bedrock witness add nas-witness --backend fileshare --path /mnt/witness
```

(Dashboard: choose backend *Fileshare* and enter the absolute *Share path*; no
pubkey.) The master probes that the path is a writable directory **on the
master** and refuses otherwise (a real create+unlink — a read-only export is
caught). Full per-node writability is enforced at vote time: a node that can't
write leaves its slot absent, so the witness simply stays at 0 votes (never a
silent miscount). Each node's off-hot-path worker then writes its slot every
~3 s; the share's latency never touches the 1 Hz election loop.

> Native Bedrock-managed SMB/S3 (creds supplied to Bedrock, which mounts it for
> you) is not built yet — mount the share yourself and use `fileshare`. The API
> refuses `--backend smb|s3` and points you here.

## List / remove

```
bedrock witness list
bedrock witness rm office-echo
```

Removing a witness lowers the quorum vote count by one and is effective on the
next election tick (its `witness_id` drops out of the configured set, so a
stale reply or slot stops counting immediately, not after the freshness window).

## Preconditions

- rqlite reachable with a leader (the master commits the `witnesses` row).
- Echo: the Echo is reachable on UDP/12321 from every node (or the same L2 for
  broadcast). Fileshare: the share is mounted + writable on every node.

## How a witness votes (what "valid + confirmed" means)

A witness contributes its +1 only while it is **valid** AND **confirmed**, the
same rules for both backends (`count_valid_confirmed`):

- **valid** — it holds a slot for *every* active cluster node (it has seen the
  whole cluster). A missing member's slot ⇒ 0 votes.
- **confirmed** — this node wrote its own slot there and read it back fresh with
  its current marker (the readback proof the takeover step relies on).

Multiple witnesses each contribute +1 (capped at the number configured); a
rogue/extra one answering can't inflate the tally. A fileshare witness's verdict
is computed off the election hot path and counts only while fresh, so a hung
share ages its witness out of the tally automatically.

## Failure modes and recovery

| Symptom | Cause | Recovery |
|---|---|---|
| `400 Echo witness address must be an IPv4 literal` | A hostname was given | Add the Echo by its IP (DNS on the election tick would stall failover). |
| `400 …not a usable IPv4 unicast address` | multicast/broadcast/loopback/link-local/IPv6 addr | Use the Echo's real unicast IPv4. |
| `400 witness_pubkey must be 64 hex chars` | Bad/short X25519 key paste | Re-copy the Echo's full 64-hex public key. |
| `400 fileshare witness path … is not usable on this node: not a directory` | Share not mounted on the master | Mount it at that path on the master (and every node) first. |
| `400 fileshare witness path … is not usable on this node: write failed` | Mounted read-only / wrong perms | Make the share writable for root on every node. |
| `400 witness backend 'smb'/'s3' is not a managed backend yet` | Tried native SMB/S3 | Mount the share and add it as `fileshare`. |
| Echo added but never votes; log warns `echo_id … matches no configured witness_id` | The Echo's `echo_id` ≠ the `witness_id` you added | Re-provision the Echo with `echo_id == witness_id`, or re-add under the Echo's actual id. |
| `404` on remove | No such `witness_id` | `bedrock witness list` for the exact id. |

## Operator perspective

- **Typical duration**: sub-second (one rqlite write + a daemon-config nudge).
  The vote becomes live within a second or two as each node's election tick
  reloads the witness list and the witness IO (UDP probe or fileshare slot
  write) lands.
- A 2-node cluster with **no** witness cannot auto-fail-over a node loss (100
  vs 100 is a tie, resolved toward "stay put" to avoid split-brain) — add one
  witness to make that cluster HA.
- A witness is **best-effort** for normal operation: it's read only at failover
  and cold boot, so a flapping witness doesn't disturb a healthy cluster — it
  just can't help arbitrate a partition while it's down.

See also: [`cluster-quorum-spec.md`](../cluster-quorum-spec.md) (the slot
protocol, the weighted vote, the backends table) and
[`quorum-design-notes.md`](../quorum-design-notes.md).
