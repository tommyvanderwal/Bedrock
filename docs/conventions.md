# Bedrock documentation conventions

A short, durable spec for how Bedrock's docs are organized. Reviewers and
future contributors should read this once.

## The two parallel doc trees

```
installer/lib/<module>.py            ← code
installer/lib/<module>.md            ← companion spec (CURRENT, clean)

docs/scenarios/<event>-<date>.md     ← POC / trial reports (dated, frozen)
docs/lessons-log.md                  ← consolidated journey (append-only)
docs/<topic>.md                      ← cross-cutting design docs
docs/conventions.md                  ← this file
```

## Rule 1 — Every Python module that executes commands or changes state has a companion `.md`

Any `.py` file whose functions:
- Run shell commands, write to `/etc/`, `/opt/`, `/var/lib/`, `/dev/`
- Make SSH calls to other nodes
- Mutate kernel state (mount, unmount, drbdadm, virsh, etc.)
- Change persistent system config

…must have an accompanying `<module>.md` next to it. The Python file's
docstring at the top must reference it. Example: `tier_storage.py`'s
docstring opens with "See `tier_storage.md` (next to this file) for the
full operational spec".

Pure-data modules (`state.py`, `workload.py` definitions, etc.) do not
need a companion `.md`.

## Rule 2 — `<module>.md` describes the CURRENT implementation, not the journey

The companion `.md` is a *clean reference* for the code as it stands
today. A new reviewer should be able to read it and understand:

- **What this module does** — high-level purpose
- **Design invariants** — what each operation must preserve. *This is
  the section that lets a reviewer reason about "can this reach a bad
  state?"* Make invariants explicit.
- **Where state lives** — table of every persistent / runtime state
  location, who owns it, when it changes
- **Operations** — per-function contracts: pre-conditions, what it
  changes, post-conditions, crash-safety properties
- **Known issues / current limitations** — gaps the reviewer should
  know about (avoid surprises)
- **Why each design choice** — rationale for non-obvious calls, with
  enough context that we can revisit them sensibly later
- **Sources** — at the bottom, every external claim cited (man pages,
  vendor docs, source code with file:line, RFCs)

The `.md` is **revised in place** as the code changes. It must stay
in sync with the implementation; an out-of-date spec is worse than no
spec.

## Rule 3 — The journey lives in `docs/lessons-log.md` (separate)

When we discover something non-obvious — a wrong assumption, a
correction, a surprise — it goes in `docs/lessons-log.md` as a new
numbered entry. Each entry has:

- **What we thought** — the original assumption
- **What we found** — the corrected understanding, with evidence
- **What we changed** — the resulting code or operational pattern
- **Reference** — the scenario report or commit where it surfaced

Lessons are **append-only**. Don't edit historical entries even if a
later finding supersedes them — write a new entry that links back.

This is the file to read when you want to understand *why* the current
code looks the way it does.

## Rule 4 — Dated scenario reports are frozen artifacts

`docs/scenarios/<event>-<date>.md` reports are written once, dated, and
not edited afterward. They capture what happened in a specific
debugging session, trial, or POC. Examples:

- `storage-tiers-1to4-2026-04-30.md` — the 4-node scale-up run
- `storage-tiers-deep-dive-2026-04-30.md` — root-cause analysis after
  the fact

Future scenario reports go alongside as new files; old ones stay as
historical record. The lessons-log distills findings from these into
the journey, the per-module `.md` distills them into the current spec.

## Rule 5 — Every external claim has a source

Inside `<module>.md`, every concrete behavioral claim about an
external tool (DRBD, Garage, s3fs, NFS, libvirt, LVM, the kernel) ends
with a citation. Sources go at the bottom of the `.md` under the
"Sources" heading, organized by tool/topic. Prefer:

1. Man pages (linked to a stable hosted version)
2. Vendor docs (linked to the relevant section)
3. Source code (file path + line number, ideally with permalink)
4. Official mailing list / forum threads
5. Bug trackers (when explaining a known limitation)

Avoid blog posts and Stack Overflow as primary sources unless they're
the only available source for a niche behavior.

## Rule 6 — Code comments are *not* a substitute

Code comments explain local subtleties at the line level. The `.md`
explains the design at the system level. Don't try to embed system-
level rationale in inline comments; it doesn't survive refactoring.

## What to do when adding a new Python action module

1. Write the code.
2. Write `<module>.md` covering the seven sections from Rule 2.
3. Open the `.py` file's docstring with a "See `<module>.md`" pointer
   and a brief summary of entry points.
4. If the work surfaced a non-obvious finding, add an entry to
   `docs/lessons-log.md`.
5. Commit code + .md together so history shows them moving in lock-step.

## What to do when fixing a bug or behavior

1. Fix the code.
2. Update `<module>.md` to match — invariants, known issues, design
   notes, whatever changed.
3. Add a `lessons-log.md` entry if the bug came from a non-obvious
   misunderstanding (so reviewers in 6 months understand why this
   shape, not the other shape).

## Good current examples to copy from

- [`installer/lib/tier_storage.md`](../installer/lib/tier_storage.md)
  — full template covering all seven sections.

## Modules currently lacking companion `.md` (queue)

These exist in the codebase, do real work, and will get companion
docs as they're substantively changed:

- `installer/lib/mgmt_install.py` — installs the full mgmt stack
- `installer/lib/agent_install.py` — joins agent nodes to a cluster
- `installer/lib/os_setup.py` — base OS configuration (SELinux,
  firewall, br0)
- `installer/lib/packages.py` — package installation
- `installer/lib/exporters.py` — Prometheus/VictoriaMetrics exporters
- `installer/lib/vm.py` — VM lifecycle (cattle / pet / vipet)
- `installer/lib/storage_install.py` — older RustFS-era storage
  installer (likely to be deprecated; mark as such if so)
- `installer/lib/discovery.py` — cluster discovery
- `bedrock-failover.py` — HA failover orchestrator (high-priority for
  reviewer attention)
- `testbed/spawn.py` — testbed manager (libvirt provisioning)
- `installer/bedrock` — main CLI (per-subcommand mini-spec is fine
  here)
- `installer/install.sh` — bootstrap shell

Don't try to write all of these at once — quality drops with breadth
in one pass. Each gets its proper `.md` when its module gets its next
substantive change.
