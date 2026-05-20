# File-size reduction plan

Big-file inventory + concrete plan to bring each candidate under ~1500 lines.

## PEP8 / PEP20 says nothing normative

- **PEP8** governs style (line length 79/99, names, whitespace). No rule on file length.
- **PEP20** (Zen): "Flat is better than nested" and "Sparse is better than dense" — guidance, not a metric.
- **Python community wisdom**: ~500–1500 lines = comfortable, ≥3000 = code smell (low cohesion). No standard.

We pick our own threshold: **target ≤ 1500 lines per module**, except where a tightly coupled state machine is more readable as one file.

## Current heavyweights

| Lines | File | Cohesion | Plan |
|---|---|---|---|
| **5,381** | `mgmt/app.py` | mixed-domain (~15 sections, 9 distinct domains) | split into `mgmt/app/` package |
| **2,984** | `installer/lib/netd.py` | one daemon, one state machine | trim, do not split |
| **1,801** | `installer/lib/tier_storage.py` | one subsystem (DRBD + tiers) | extract `_local-reset` + diagnostic helpers |
| **1,035** | `mgmt/backup.py` | one subsystem | leave, near limit |
| **950** | `installer/lib/bedrock_state.py` | one subsystem (rqlite wrapper) | leave |
| **887** | `mgmt/orchestrator.py` | 5 async tasks | leave; clear seams already |

Anything ≤ 700 lines: leave alone.

The `iso-build/payload/lib/*.py` and `iso-build/build/iso-extract/bedrock/lib/*.py` duplicates are STAGED COPIES of `installer/lib/*.py`; fixing the source file fixes them all (build-iso.sh re-copies).

---

## Target 1 — `mgmt/app.py` → `mgmt/app/` package

5,381 lines → 12 files, each 60–1,300 lines.

### Layout

```
mgmt/
  app/
    __init__.py            ~100  FastAPI app, startup hook + lock, serve_main
    config.py              ~80   constants, paths, BedRock identity
    auth.py               ~200  middleware + operator login/management
    join.py               ~320  join handshake (piece #3 of inter-node auth)
    cluster_state.py      ~520  data gathering + topology rollup
    push_loop.py          ~110  WebSocket hub + state_push_loop
    routes_cluster.py     ~110  /api/cluster, /api/host
    routes_iso.py         ~65   ISO library
    vm/
      __init__.py         ~50   shared helpers
      actions.py         ~100   start/stop/destroy/migrate
      settings.py        ~110   vcpus/ram/disk/priority/cdrom
      creation.py        ~520   cattle/pet/vipet create
      conversion.py      ~700   workload conversion (cattle ↔ pet ↔ vipet)
      import_export.py   ~950   VMware→Bedrock + Bedrock→qcow2
    routes_backup.py     ~560   backup endpoints + secret propagation
    routes_obs.py        ~250   obs backend mgmt + metrics + logs
    routes_support.py    ~225   supportability checks
    routes_console.py     ~80   VNC redirect + WS-TCP proxy
```

### Order of operations (one PR per row, each ≤300 LOC moved)

| # | What | Sections cut from app.py | New file | Risk |
|---|---|---|---|---|
| 1 | extract VNC proxy | 5228–5309 | `routes_console.py` | low |
| 2 | extract supportability | 5001–5227 | `routes_support.py` | low |
| 3 | extract observability + metrics/logs APIs | 1339–1483 + 4909–5000 | `routes_obs.py` | low |
| 4 | extract ISO library | 1701–1764 | `routes_iso.py` | low |
| 5 | extract backup | 2825–3383 | `routes_backup.py` | medium (secret propagation has cross-imports) |
| 6 | extract VM actions/settings | 2714–2824 + 3384–3487 | `vm/actions.py`, `vm/settings.py` | medium |
| 7 | extract VM creation + conversion | 3488–4710 | `vm/creation.py`, `vm/conversion.py` | medium |
| 8 | extract VM import/export | 1765–2713 | `vm/import_export.py` | medium |
| 9 | extract auth + operator login | 815–1017 | `auth.py` | high (every route depends on middleware) |
| 10 | extract join handshake | 1018–1338 | `join.py` | high (HMAC + Ed25519 verification entry points) |
| 11 | extract cluster state + topology | 290–809 | `cluster_state.py` | high (rqlite + WebSocket hot path) |
| 12 | finalise: `__init__.py` + `push_loop.py` + `routes_cluster.py` | remainder | several | low |

Total: ~12 PRs, each touching < 600 LOC, each green on `test_e2e_offline.sh`.

### Why a package, not parallel modules

The 63 `@app.get/post/put/...` decorators all attach to ONE FastAPI `app` instance. The startup hook + state globals (`_last_state`, `_STARTUP_LOCK`, `task_registry()`, hub) must live in `__init__.py` so every route file does `from . import app, hub, _last_state`. Parallel modules would either need a singleton-getter pattern or circular imports.

### Risk mitigation

- **No behaviour change in any single PR.** Pure move-and-import.
- **Each PR runs `test_e2e_offline.sh` before merge.** 90 PASS lines is the baseline.
- **The mgmt.tar.gz packaging is unchanged.** Still extracts to `/opt/bedrock/mgmt/`; `app/` is a sub-directory.
- **Old import sites** — anywhere else that does `from mgmt.app import X` — get one search-replace pass per PR.

---

## Target 2 — `installer/lib/netd.py` (2,984 lines)

Single-daemon state machine. Splitting it would scatter the protocol across files. **Do not split.** Instead, trim:

- Extract `l2disc.*` + MikroTik MNDP discovery → already mostly external (`l2disc.py`).
- Extract `route_apply` / `route_revoke` (~150 lines of `ip route` shell-out wrappers) → `installer/lib/route_apply.py`.
- Extract `_election_tick` body into `lib/election_tick.py` if it grows further (currently ~250 lines, acceptable).

Estimated reduction: ~300 lines → 2,700. Still over 1,500 but a single coherent state machine is more readable than a fragmented one.

---

## Target 3 — `installer/lib/tier_storage.py` (1,801 lines)

Mostly one subsystem (DRBD + LVM + tier mounts). Two clear extractions:

- `_local-reset` CLI subcommand handler (~300 lines) → `installer/lib/local_reset.py`. Only ever runs on `bedrock node leave` cleanup.
- diagnostic helpers (`tier_status_json`, `_drbd_uuid_chain`) → `installer/lib/tier_diagnostics.py` (~250 lines).

After: ~1,250 lines core + 550 split. Target met.

---

## Hardening before any split

1. **Run v27/v28 cluster_quorum_spec compliance test green.** All 90 PASS lines.
2. **Add a `tests/` dir.** Right now the only test is `test_e2e_offline.sh` (4 VMs, ~30 min). For refactor confidence we need:
   - import smoke tests (`python -c "import mgmt.app"`) per PR
   - unit tests for the new split modules' pure functions (election.compute, cluster_arbiter.i_should_host_arbiter)
3. **Commit the file-size baseline** so each PR can prove it shrunk net lines.

---

## Ordering relative to v1.0

| Phase | Work | When |
|---|---|---|
| now | finish quorum spec compliance (INV-2/3/4) | this session |
| pre-v1.0 | tag v1.0 with current shape (5,381 lines acceptable) | once 90 PASS line clean |
| v1.0+1 week | PRs 1–5 (low-risk extracts) | next sprint |
| v1.0+2 weeks | PRs 6–8 (medium-risk VM domain) | next sprint |
| v1.0+3 weeks | PRs 9–11 (high-risk: auth, join, cluster_state) | only after dashboard browser-test pass |
| v1.0+4 weeks | PR 12 finalise + tier_storage split | end of refactor |

**Don't split mid-feature.** Wait for green main.

## Anti-patterns to avoid

- **Splitting by layer (routes / services / repos).** That's Java-brain. We have one Bedrock domain across these files — split by domain instead.
- **One-class-per-file rule.** Python isn't Java. A 200-line file with a class + 3 helper functions is fine.
- **Premature interface extraction.** No `BackupServiceProtocol` ABCs unless we have a second implementation in flight.
- **Top-level `utils.py`.** That's where every dead code path goes to hide.

## Open question (low-priority)

`installer/iso-build/{payload,build}/lib/*.py` are exact copies of `installer/lib/*.py` (`build-iso.sh` re-stages them). After the netd/tier_storage extracts, both staged copies need to be re-checked. Consider a CI step that asserts `diff -r installer/lib installer/iso-build/payload/lib` is empty before tagging an ISO release.
