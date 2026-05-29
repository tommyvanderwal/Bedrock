# bedrock_d/orchestrator/sagas/__init__.py

Package root for Bedrock's sagas — crash-safe, resumable multi-step cluster
operations (cluster_init, node_join, vm_create, …). It carries the module
docstring describing the saga pattern and re-exports the public API from the
package's submodules so callers import from `bedrock_d.orchestrator.sagas`
directly. The saga executor inside `bedrock-d` is the consumer.

## Re-exports

From `.executor`:
- `SAGAS` — registry of saga classes.
- `SagaBackend` — protocol for the rqlite-backed persistence layer the executor talks to.
- `SagaExecutor` — runs a saga's `@step`-decorated methods in declaration order and resumes in-flight sagas on restart.
- `SagaResult`, `SagaState` — result and state types for a saga run.
- `saga`, `step` — decorators that register a saga class and mark its step methods.

From `.file_backend`:
- `FileSagaBackend` — file-based implementation of `SagaBackend`.

## How it works

A saga is submitted by writing an `operations` row to rqlite; the executor (the
only component here that talks to rqlite, via `SagaBackend`) runs each idempotent
step in declaration order and records per-step success/failure in
`operation_steps`. On crash and restart it finds in-flight sagas for this node
and resumes from the first step lacking an `operation_steps(op_id, step_name)`
row with `state='done'`.

```
submit ─► operations row (rqlite)
            │
            ▼
   SagaExecutor (sole rqlite talker, via SagaBackend)
     step₁ ─► step₂ ─► … ─► stepₙ      (declaration order)
       └────────┴───── … ──► operation_steps rows (done / failed)

crash + restart ─► resume from first step without a 'done' row
```

Step bodies are pure methods mutating a shared `context` dict — no rqlite calls
or global state inside them; the executor owns all persistence. Each step checks
"already done? → return" so a resumed run replays cleanly.
