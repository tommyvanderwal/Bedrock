# AGENTS.md — guidance for AI assistants working in Bedrock

Read this before changing code. For subsystem detail, see `BEDROCK.md`, `docs/conventions.md`, and the companion `.md` next to each module under `lib/`.

## Load-bearing code only

Every line you add must earn its place. Bedrock is an infrastructure daemon: dead code, speculative abstractions, and “just in case” layers make failures harder to see and harder to fix.

- **Write load-bearing code** — if removing a line would not change behavior, reliability, or operability, do not add it.
- **No new dependencies** unless the task cannot be done cleanly with what the repo already uses (stdlib, existing Bedrock libs, shipped binaries). State the concrete gap before adding a package or service.
- **No wrappers without a job** — do not add try/except, indirection, interfaces, or config toggles that only log and continue when the correct behavior is fail loud or fix the root cause.
- **Minimal diff** — solve the requested problem; do not refactor, reformat, or “improve” adjacent code unless asked.
- **Match existing style** — read surrounding code first; extend patterns already in the file.

## When in doubt

Prefer: one obvious path, explicit errors, systemd/journal visibility, and docs updated only when behavior or operator workflow changes.

Avoid: feature flags for unfinished ideas, backward-compat shims nobody asked for, and comments that restate what the code already says.
