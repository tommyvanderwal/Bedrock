# (superseded)

This early draft had the **wrong trigger** (it framed the mint as needing force-promote+write).
Empirical RCA on a source-built DRBD 9.3.2 testbed proved the trigger is **`drbdadm resume-io`**
on a quorum-lost frozen Primary. The corrected, validated, long-form report lives at:

→ `docs/bug-reports-upstream/drbd-quorum-lost-primary-uuid-rotation/BUGREPORT.md`

with the full RCA in `…/research/EMPIRICAL-FINDINGS.md` and the before/after proof in
`…/evidence/PATCH-VALIDATION.md`.
