# Patch validation — before/after, only the patch differs

Same testbed, same harness (`testbed/drbd_uuid_bug/repro.sh`), same trigger
(`drbdadm resume-io` on a frozen quorum-lost minority Primary, sim-1 keeps sim-2, loses
sim-3/sim-4). Two modules, both built from the LINBIT `drbd-9.3.2` release tarball on the sim
kernel `6.12.0-124.8.1.el10_1`; the only difference is the one-line ARM guard.

## The patch (single line, the `lost_contact_to_peer_data` arm in `drbd_state.c`)

```diff
 			if (lost_contact_to_peer_data(peer_disk_state)) {
 				if (role[NEW] == R_PRIMARY && !test_bit(UNREGISTERED, &device->flags) &&
-				    drbd_data_accessible(device, NEW))
+				    drbd_data_accessible(device, NEW) &&
+				    !test_bit(PRIMARY_LOST_QUORUM, &device->flags))
 					create_new_uuid = true;
```
`PRIMARY_LOST_QUORUM` is set earlier in the same `finish_state_change` pass (`drbd_state.c:2879`).
srcversion: unpatched `620CF40B5F80831F0CD3E9E`, patched `D19E28ACF5CDA0026B6545C`.

## Result (3 rounds each)

| Round | unpatched: `resume-io` → | patched: `resume-io` → |
|---|---|---|
| 1 | **MINT** `F7AE071392E4BC4B` (ROTATED=YES, mint-lines=1) | no mint, `3D61397D409E3EAA` unchanged (ROTATED=NO, mint-lines=0) |
| 2 | **MINT** `6E00B2F12C149731` | no mint, `144D7E469D20AABA` unchanged |
| 3 | **MINT** `F58DFD7F63BA24D1` | no mint, `0E25D3042EEA3DD2` unchanged |

In every case the node stayed `suspended:quorum` and wrote zero bytes. Unpatched mints a spurious
generation (absent peers stamped `weak:FFFFFFFFFFFFFFFC`) → false split-brain / full resync on
heal. **Patched: the frozen minority never mints, even when `resume-io` is wrongly issued.**

Raw per-round captures: `research/EMPIRICAL-FINDINGS.md` table + `evidence/resumeio-round-{1,2,3}/`
(unpatched) and `evidence/resumeio-PATCHED-round-{1,2,3}/` (patched).

## Conclusion

The bug is real and the ARM guard fixes it. This is **defense in depth** at the DRBD layer; the
root-cause trigger (bedrock-d issuing `resume-io` to the minority) is fixed separately in
Bedrock so the minority is never resumed in the first place.
