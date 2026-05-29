# Bedrock documentation conventions

How Bedrock's docs are organized. Read this once before adding or editing docs.

## The doc trees

```
installer/lib/<module>.py            ← code
installer/lib/<module>.md            ← companion spec (current, clean)
installer/lib/<module>__<fn>.md      ← optional per-function deep-dive

docs/<topic>.md                      ← cross-cutting design docs
docs/<NN>-<topic>.md                 ← numbered core-subsystem references
docs/reference/<topic>.md            ← reference tables (ports, files, api, logs)
docs/components/<topic>.md           ← component guides (dashboard, exporters, …)
docs/actions/<verb-noun>.md          ← per-operator-action guides
docs/sagas/<saga>.md                 ← per-saga step references (+ README index)
docs/scenarios/<event>.md            ← trial / failure-mode reports
docs/lessons-log.md                  ← consolidated journey (append-only)
docs/conventions.md                  ← this file
```

## Rule 1 — Every Python module that executes commands or changes state has a companion `.md`

Any `.py` whose functions run shell commands, write under
`/etc /opt /var/lib /dev`, SSH other nodes, mutate kernel state (mount,
unmount, `drbdadm`, `virsh`, …), or change persistent system config must have a
`<module>.md` beside it. The module's top docstring points to it, e.g.
`tier_storage.py` opens with "See `tier_storage.md` (next to this file) for the
full operational spec".

Pure-data modules (`state.py`, workload definitions, …) do not need a companion.

## Rule 2 — `<module>.md` describes the current implementation

The companion `.md` is a clean reference for the code as it stands. It is often
**longer than the source** because the WHY needs as much room as the WHAT. The
audience is **4+ human reviewers AND 4+ LLM reviewers** hunting for bad reachable
states; both want explicit invariants, ASCII diagrams, and traceable citations.

Structure:

1. **Top-of-file summary** — read-this-first paragraph: entry points,
   prerequisites, operating model. A caller who stops here still uses the module
   correctly.
2. **ASCII diagrams where they clarify** — relationships, control flow, data
   flow, on-disk/LV layouts. Diagrams beat prose for "what calls what" and "what
   state goes where".
3. **Design invariants** — what each operation must preserve. One numbered
   invariant per load-bearing property; this is what lets a reviewer reason about
   "can this reach a bad state?".
4. **Where state lives** — table of every persistent / runtime state location:
   who owns it, when it changes.
5. **Operations explained in detail** — per-entry-point contract: pre-conditions,
   what it changes, the exact sequence of underlying commands, post-conditions,
   crash-safety. The bulk of the doc.
6. **Known issues / current limitations** — gaps a reviewer should know about.
7. **Why each design choice** — rationale for non-obvious calls.
8. **Sources** — every external behavioral claim cited to its primary source
   (man page, vendor doc, source `file:line`, RFC) so any reviewer can verify it.

The `.md` is revised in place as the code changes; an out-of-date spec is worse
than none.

### Per-function deep-dive `.md`

When one function is complex enough to warrant its own full ASCII flow, exact
command sequence, crash-safety table, and citations, give it
`installer/lib/<module>__<function>.md` (double-underscore separator). The
parent `<module>.md` keeps a short summary plus a relative link, so it stays
skimmable as the top-level reference.

## Rule 3 — The journey lives in `docs/lessons-log.md`

A non-obvious finding — a wrong assumption, a correction, a surprise — gets a new
numbered entry:

- **What we thought** — the original assumption
- **What we found** — the corrected understanding, with evidence
- **What we changed** — the resulting code or operational pattern
- **Reference** — the scenario report or commit where it surfaced

Lessons are **append-only**: don't edit historical entries even when a later
finding supersedes one; write a new entry that links back. Read this file to
understand *why* the current code looks the way it does.

## Rule 4 — Scenario reports are frozen artifacts

`docs/scenarios/*.md` are written once and not edited afterward. Each captures
one debugging session, trial, or failure-mode run (e.g. `network-partition.md`,
`power-loss-primary.md`, `split-brain.md`). Date-stamp the filename when a report
is tied to a specific dated run. New reports go alongside as new files; the
lessons-log distills findings into the journey, the per-module `.md` distills
them into the current spec.

## Rule 5 — Every external claim has a source

In `<module>.md`, every concrete behavioral claim about an external tool (DRBD,
SeaweedFS, libvirt, LVM, rqlite, the kernel) ends with a citation, collected
under a bottom "Sources" heading organized by tool/topic. Prefer, in order:

1. Man pages (stable hosted version)
2. Vendor docs (linked to the relevant section)
3. Source code (`file:line`, ideally a permalink)
4. Official mailing-list / forum threads
5. Bug trackers (when explaining a known limitation)

Avoid blogs and Stack Overflow as primary sources unless nothing else covers a
niche behavior.

## Rule 6 — Code comments stay; they complement the `.md`

High-level remarks, function-level intent, hints, and short clarifications belong
in the source — they're what a reader sees first on jumping to a function. The
`.md` carries the extensive explanation: the full sequence, every command, every
invariant, every citation.

- 1-3 line comment above a function explaining intent: yes
- Short comment beside a non-obvious line: yes
- Multi-paragraph commentary embedded in the source: no — that goes in the `.md`

Code and `.md` serve different reading modes (skim-the-code vs study-the-design);
both are valuable.

## Adding a new action module

1. Write the code.
2. Write `<module>.md` covering the eight sections of Rule 2.
3. Open the `.py` docstring with a "See `<module>.md`" pointer and a brief
   summary of entry points.
4. If the work surfaced a non-obvious finding, add a `docs/lessons-log.md` entry.
5. Commit code + `.md` together so they move in lock-step.

## Fixing a bug or behavior

1. Fix the code.
2. Update `<module>.md` to match — invariants, known issues, design notes.
3. Add a `docs/lessons-log.md` entry if the bug came from a non-obvious
   misunderstanding, so a future reviewer understands this shape, not another.

## Examples to copy from

- [`installer/lib/cluster_arbiter.md`](../installer/lib/cluster_arbiter.md) —
  module overview + invariants + entry points.
- [`installer/lib/witness.md`](../installer/lib/witness.md) — module purpose +
  constants + per-function summaries.
- [`installer/lib/tier_storage.md`](../installer/lib/tier_storage.md) —
  per-node thinpool + per-resource DRBD/thin-meta layout.
- [`docs/sagas/README.md`](sagas/README.md) — saga index and per-saga doc format.
