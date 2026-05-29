# Bedrock — Architectural findings from the v1.0 deploy/RCA loop (2026-05-29)

> Companion to `DISCREPANCY-REVIEW-2026-05-28.md` (static code review) and
> `EXECUTION-PLAN.md` (locked design). This file captures what the **live
> 4-node testbed deploy** taught us — the bugs static review couldn't see, and
> the architecture-level questions they surfaced for Tommy's review.

## Where we are

The full v1.0 rewrite (storage per-resource, BAD-1 consensus, boot ownership,
VM-saga cutover, self-heal) is committed + pushed, **333 unit tests green**, and
**validated end-to-end on a fresh-ISO 4-node cluster**:

- ✅ Fresh offline-ISO install on all 4 sims → firstboot `.bootstrap-done`
- ✅ `bedrock init` (N=1 storage), witness register, join 2→3→4 (all PASS),
  every node reaches `state=active` (C1 node-state works)
- ✅ `storage promote` → cluster-singleton DRBD Primary, `.254` arbiter VIP,
  arbiter rqlite + filer + s3 up, **5-voter two-tier rqlite** (`1,2,3,4,254`)
- ✅ VM create **cattle / pet / vipet** → correct **2-way / 3-way DRBD replicas
  UpToDate**, correct `failover_order`, and (after RCA #7) the libvirt domain
  defined on the failover peers — genuinely failover-ready
- ✅ rqlited now auto-starts on boot (reboot-resilient)

## RCA scorecard — 7 root-cause fixes the deploy surfaced (none catchable by unit tests)

| # | Bug | Root cause | Fix (commit) |
|---|---|---|---|
| 1 | join approval `JSONDecodeError` | testbed driver queried rqlite over plain `http`; rqlite is mTLS-HTTPS | driver → mTLS certs (`7be6c64`) |
| 2 | `drbdadm primary: Unknown resource` spam + latent data-loss | **dual-owner** DRBD promote: `cluster_arbiter` gated on `drbdadm dump` (true before `up`) → raced `tier_storage`'s create-md/up/restore | local `cluster-drbd-ready` marker; tier_storage sole owner of N=1→N=2 (`3967158`) |
| 3 | `umount: target is busy` on promote | `promote_local_to_drbd_master` not idempotent (watcher auto-promote + manual promote) | already-Primary+mounted early-return (`3967158`) |
| 4 | `bedrock vm create` → 401 | VM-saga cutover made the CLI a thin client to `/api/vms`, but the loopback API demanded operator auth | loopback (`:8001`) exempt; LAN (`:8443`) still authed (`0e4d925`) |
| 5 | master reboot → **0-byte `state.json`** → node deadlock | `save()` did tmp+rename but **no fsync** → unclean reboot lost the data blocks | fsync data + dir = crash-durable (`2ebd622`) |
| 6 | reboot → **rqlited never restarts** | `bedrock-rqlited` `WantedBy=` empty "by design" (bedrock-d starts it once at init); on reboot nothing does, and bedrock-d can't bootstrap from a lost state.json | rqlited auto-starts (`WantedBy=multi-user.target`) — it's the consensus foundation (`2ebd622`) |
| 7 | pet/vipet **domain missing on failover peers** (DRBD data replicated, but no domain to start) | `lvm._run_on` built `ssh host '{cmd}'` — single-quote-wrapped without escaping; the domain XML's `type='kvm'` broke the quoting → invalid XML → `virsh define` failed silently (`check=False`) | `shlex.quote(cmd)` + surface define failures (`55ad65b`) |

Plus hygiene: gitignored generated tarballs; persistent journald enabled on the sims (the reboot in #5/#6 was undiagnosable because journald was volatile — a real install gap).

## Architectural discrepancies for review (the bigger questions)

### A-1 — Dual ownership of the cluster-singleton DRBD promote/mount
`tier_storage` (the `cluster_tier_watcher` auto-promote saga) **and** `cluster_arbiter`
(the netd LEADER tick, post-H5) both promote + mount the `cluster` DRBD. The
`cluster-drbd-ready` marker (RCA #2) makes them coexist safely, but the clean shape
is **a single owner**: tier_storage does the one-time N=1→N=2 setup + data restore;
cluster_arbiter owns only the ongoing role (primary/.254/arbiter-rqlite on failover).
Worth a deliberate refactor to remove the shared responsibility entirely.

### A-2 — Reboot-resilience of *all* orchestrator-managed services
The daemon-unification model ("only bedrock-d auto-starts; it starts everything
else") is **fragile across reboots**: the init/join saga starts services *once*;
on a later reboot, only what's `WantedBy=multi-user.target` comes back. We fixed
rqlited (#6), but the same question applies to **weed-master/volume/filer, the obs
stack, libvirtd, the arbiter, and per-VM DRBD + VMs**. Decision needed: which
services systemd auto-starts (the foundations) vs. which bedrock-d's
`boot_orchestrator` brings up after quorum — and the boot_orchestrator must
reliably re-establish the latter on every boot, not just at init. This is the
core of the "power-loss recoverable on boot" promise; it currently isn't fully met.

### A-3 — Cluster-singleton DRBD replica cap not enforced  ⚠ blocks HA
Observed at N=4: the singleton is **4-way** (master + 3 secondaries, one
`Unconnected`) — the design caps it at **`min(3, N)` = 3-way**. Each join adds the
joiner as a singleton peer with no cap check. The over-wide set wedged the initial
sync (stuck at ~2.5%) and left a peer unconnected → **no UpToDate secondary → the
arbiter cannot fail over.** This is the SG-05 membership gap made concrete and is
the next thing to fix before failover testing can pass.

### A-4 — Cluster-singleton initial DRBD sync (5 GB, ~1% used)
The singleton is a 5 GB volume that DRBD initial-syncs in full (mostly zeros) even
though only ~1.4% holds data (filer leveldb3 + arbiter rqlite). `resync-rate 100M`
is configured (fine), but combined with A-3 the sync stalled. Options: enforce the
3-way cap (A-3), shrink the singleton (5 GB is generous for arbiter+filer), and/or
verify multi-secondary initial-sync doesn't serialize/stall. Per-VM DRBD (1 GB,
2-way) syncs fine, so the issue is specific to the wide singleton.

### A-5 — Operator-vs-auto promote ambiguity
`cluster_tier_watcher` **auto-promotes** the singleton on reaching N≥2, but
`state-flow.md` and the CLI present `bedrock storage promote` as the **operator**
trigger — so `setup_4node_cluster.sh` double-triggers it (now a harmless idempotent
no-op + a benign "peer transition returned 1" on the re-run). Pick one model:
auto (drop the operator verb / make it a status query) or operator (disable the
watcher's auto-fire). Today it's both.

### A-6 — Idempotency of the peer-side transitions
`transition_to_n2_peer` / `join_drbd_peer` and `create-md` aren't fully idempotent
(`Device 'NNNN' is configured!`, `peer transition returned 1` on re-run). Only bites
under the A-5 double-trigger today, but saga-resume could hit it — worth hardening
since the platform is saga-resumable by design.

### A-7 — State-durability audit
RCA #5 fixed `state.json`. The same crash-durability question applies to other
critical local writers — e.g. the rqlited.env render, witness slot persistence,
the local 7-day UUID history. A quick audit for fsync-before-rename across all
`/etc/bedrock` writers is warranted.

## Recommended next steps
1. **Fix A-3 (3-way cap)** — the immediate HA blocker; then a fresh-ISO reset re-validates failover.
2. Run `test_pet_vm_failover.sh` + `test_pet_vm_no_witness_isolation.sh` on the clean cluster (now that pet domains reach peers, #7).
3. Work A-2 (reboot-resilience) — verify every service returns after a reboot of each role.
4. A-1/A-5/A-6 cleanups (single-owner promote, one promote-trigger model, peer-side idempotency).
5. A-7 fsync audit; bake persistent journald into the install.
