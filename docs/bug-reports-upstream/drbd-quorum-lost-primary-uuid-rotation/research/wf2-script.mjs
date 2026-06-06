export const meta = {
  name: 'drbd-native-quorum-solution',
  description: 'Exhaustively find the NATIVE (config-only) DRBD-9 way to get split-brain-free incremental heal on quorum-loss failover, before any kernel patch',
  phases: [
    { title: 'Research', detail: '10 parallel deep-dives: history, fencing/outdating, RULE_LOST_QUORUM, after-sb, LINSTOR/Proxmox practice, mailing list, re-refute the leak' },
    { title: 'Synthesize', detail: 'Is there a native config-only solution? Exact config, or proof of why a patch is unavoidable' },
    { title: 'Verify', detail: 'Adversarially verify each load-bearing claim against source + docs' },
    { title: 'Finalize', detail: 'Final native recipe vs patch verdict, with the exact config' },
  ],
}

const MISSION = [
  'MISSION: Tommy (the product owner) is rightly skeptical that a MATURE product like DRBD would need a',
  'kernel patch to honor "quorum all". Your job is to EXHAUST the native, LINBIT-intended configuration',
  'options FIRST. Default to trying to REFUTE the "must patch the kernel" conclusion. Only if every native',
  'avenue is genuinely closed do we even consider custom kernel code. Find the CORRECT native approach.',
  'Read the REMOTE documentation broadly and deeply (user guide, release notes, blog, KB, mailing list,',
  'GitHub issues) AND the kernel source. Quote actual text with file:line or URL. ZERO assumptions.',
].join('\n')

const EMPIRICS = [
  'BEDROCK CONTEXT: hyperconverged appliance. DRBD-9 (kernel module 9.3.2, utils 9.34.0) replicates an',
  'arbiter "cluster" resource AND every VM disk across 3-5 nodes. An external election daemon (bedrock-d)',
  'decides who is Primary; DRBD must NOT make Primary decisions itself (auto-promote no). The REAL scenario',
  'that matters is FAILOVER: the current DRBD Primary is in the MINORITY side of a partition (it gets cut',
  'off), and a NEW Primary must be promoted on the MAJORITY side. The old (minority) Primary freezes.',
  '',
  'OBSERVED on a 4-node cluster, resource options: "quorum all; on-no-quorum suspend-io;',
  'on-suspended-primary-outdated force-secondary; auto-promote no" (NO fencing handlers configured, NO',
  'after-sb-* policies set, disks were NOT explicitly outdated):',
  '(A) Isolate the Primary from ALL peers at once -> suspended:quorum, role stays Primary, current-UUID',
  '    did NOT change (frozen at pre-partition value).',
  '(B) Isolate the Primary from SOME peers but KEEP at least one -> suspended:quorum (lost quorum), wrote',
  '    NOTHING, BUT current-UUID DID rotate to a new value (kernel log: "new current UUID: CB50...").',
  'On heal of (B): the survivor force-promoted on the OTHER (majority) side ALSO rotated its current-UUID',
  '    from the same common ancestor -> two diverged UUIDs from a common ancestor -> DRBD declared',
  '    split-brain (StandAlone) and demanded full resync / --discard-my-data, NOT a clean incremental resync.',
  '',
  'CURRENT (POSSIBLY WRONG) CONCLUSION TO CHALLENGE: "No drbd.conf/drbdsetup option prevents the loser',
  'rotation; the two immediate paths drbd_state.c:4295-4305 and :4466-4471 reach drbd_uuid_new_current()',
  'without a quorum guard (unlike the guarded write route drbd_sender.c:3443 and disconnect route',
  'drbd_receiver.c:9886); therefore we must patch the kernel module to add !PRIMARY_LOST_QUORUM guards."',
  '',
  'GOAL we need to achieve NATIVELY if possible: a quorum-losing, zero-write Primary that fails over should',
  'heal as a clean INCREMENTAL resync (no split-brain, no --discard-my-data, no full resync), so that',
  '"quorum all" effectively means what it says: no autonomous data-generation divergence.',
].join('\n')

const SOURCES = [
  'AUTHORITATIVE SOURCES — quote actual text:',
  '- DRBD kernel source already cloned locally: /tmp/drbdsrc/drbd and /home/tommy/projects/drbdsrc_clone/drbd',
  '  (LINBIT/drbd REL_VERSION 9.3.2, HEAD a46cbd9, 2026-06-01). Use Bash grep -n / Read on:',
  '  drbd_state.c, drbd_main.c, drbd_receiver.c, drbd_sender.c, drbd_nl.c, drbd_req.c, and the ChangeLog.',
  '- DRBD 9 User Guide: linbit.com/drbd-user-guide/drbd-guide-9_0-en/ — chapters on quorum, fencing,',
  '  three-node, generation identifiers (16.2), "Resolving split brain", auto-recovery (after-sb-*).',
  '- Manpages: drbd.conf-9.0, drbdsetup-9.0 (manpages.debian.org) — quorum, on-no-quorum, fencing,',
  '  quorum-minimum-redundancy, on-suspended-primary-outdated, after-sb-0pri/1pri/2pri, the handlers.',
  '- LINBIT blog + KB: search "DRBD quorum", "DRBD split brain", "DRBD fencing", "tiebreaker".',
  '- drbd-user mailing list (mail-archive.com/drbd-user@lists.linbit.com), LINBIT forums, GitHub LINBIT/drbd issues.',
  'Use WebSearch + WebFetch for the remote docs/list/issues. If a tool schema is not loaded, use ToolSearch',
  '("select:WebFetch,WebSearch") first. Combine source-of-truth code with the official prose.',
].join('\n')

const FINDINGS = {
  type: 'object',
  additionalProperties: false,
  properties: {
    area: { type: 'string' },
    summary: { type: 'string', description: 'Evidence-based prose answering the assigned questions. State plainly whether it points toward a native solution or confirms a patch is needed.' },
    native_lever: { type: 'string', description: 'The specific native config/option/handler this area surfaces (if any) that could achieve the GOAL without a kernel patch, and how it would work. "none found" if truly none.' },
    rules: { type: 'array', items: { type: 'object', additionalProperties: false, properties: {
      rule: { type: 'string', description: 'A precise behavioral fact about DRBD, config option, or LINBIT guidance.' },
      evidence: { type: 'string', description: 'Quoted source/doc/list text or specific function/section.' },
      source: { type: 'string', description: 'file:line, URL, or manpage section.' },
      confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    }, required: ['rule', 'evidence', 'source', 'confidence'] } },
    open: { type: 'array', items: { type: 'string' }, description: 'Unresolved questions for the next phase.' },
  },
  required: ['area', 'summary', 'native_lever', 'rules', 'open'],
}

const AREAS = [
  { key: 'quorum-history-intent', q: 'When were DRBD quorum, on-no-quorum, quorum-minimum-redundancy, and the diskless tiebreaker introduced (which DRBD 9.0.x version + date)? Were they designed for 3+ node clusters from the start, or retrofitted onto 2-node replication? Quote the ChangeLog/release notes (grep /tmp/drbdsrc ChangeLog and the git log). CRUCIAL: does any release note, commit message, or doc ACKNOWLEDGE the "quorum-lost primary rotates current-UUID -> split-brain on heal" behavior as intended, as a known limitation, or as a fixed bug? Search git log for "quorum" + "uuid" + "new current".' },
  { key: 'fencing-outdating', q: 'DRBD FENCING is the classic LINBIT split-brain-prevention mechanism that PREDATES quorum. Investigate fully: the "fencing" resource option (dont-care / resource-only / resource-and-stonith), the fence-peer / unfence-peer / after-resync-target handlers, and DISK OUTDATING (D_OUTDATED, drbdadm outdate, the "outdated" disk state). KEY QUESTIONS: (1) Does an OUTDATED node suppress current-UUID rotation? (2) If the losing minority Primary is OUTDATED (by a fence handler) before/when it loses quorum, does heal become a clean incremental resync because an outdated node always yields as SyncTarget? (3) Is "fencing resource-only/resource-and-stonith" the INTENDED companion to quorum for split-brain-free failover? Quote user-guide fencing chapter + drbd_nl.c / drbd_state.c outdating code.' },
  { key: 'rule-lost-quorum', q: 'Read drbd_uuid_compare() in drbd_receiver.c (around line 4678) and especially the RULE_LOST_QUORUM rule (~4785-4798) and RULE_BITMAP_PEER (~4888). Under EXACTLY what UUID + flag state does DRBD return a CLEAN incremental resync after one side lost quorum? Does RULE_LOST_QUORUM require self.current == peer.current (i.e. the loser did NOT rotate), or can it reconcile a loser that rotated to a sibling generation? Is there a PERSISTED on-disk flag/bit (in the metadata / UUID flags) that records "this node lost quorum, accept resync from the quorum side"? If such a persisted marker exists, the loser rotating may still heal incrementally. Trace the exact bits.' },
  { key: 'outdated-suppresses-rotation', q: 'Re-read the UUID-rotation ARM path (drbd_state.c:3096-3099 create_new_uuid, :3135 set NEW_CUR_UUID) and the EXECUTE paths (drbd_state.c:4295-4305, :4466-4471 -> drbd_uuid_new_current). Question with FRESH eyes: is create_new_uuid or the execute SUPPRESSED when the node is D_OUTDATED, D_INCONSISTENT, or has PRIMARY_LOST_QUORUM set, anywhere in the chain? Is there a disk-state or quorum condition that ALREADY prevents the rotation that our test simply did not trigger (because we never outdated the loser)? Specifically: if the loser were outdated, would create_new_uuid (gated on drbd_data_accessible / UpToDate at :3096-3099) become false and skip the rotation entirely?' },
  { key: 'native-failover-recipe', q: 'Find LINBIT\'s DOCUMENTED recipe for: Primary in the minority partition, a NEW Primary promoted in the majority, old Primary returns -> clean incremental resync with NO split-brain and NO manual --discard-my-data. Search the user guide quorum chapter, three-node chapter, "Integrating DRBD with Pacemaker", and any "automatic failover" docs. What EXACT option set does LINBIT prescribe? Is the documented expectation that quorum+suspend-io alone yields incremental heal, or that you ALSO need fencing/outdating and/or after-sb policies? Quote the prescription.' },
  { key: 'after-sb-policies', q: 'Investigate after-sb-0pri, after-sb-1pri, after-sb-2pri auto-recovery policies (disconnect, discard-younger-primary, discard-older-primary, discard-zero-changes, discard-least-changes, discard-secondary, consensus, call-pri-lost-after-sb, violently-as0p, etc.) and the rr-conflict option. CRUCIAL QUESTION: could a correct after-sb-* config make DRBD AUTOMATICALLY resolve the heal by discarding the loser side and doing an incremental (or at worst automated full) resync toward the bedrock-blessed Primary, WITHOUT operator action and WITHOUT a kernel patch? Is the "split-brain" we observed actually a config-resolvable auto-recovery rather than a fatal divergence? What does each policy do to the UUIDs, and which one matches "always keep the side bedrock promoted"? Quote drbd.conf manpage + drbd_receiver.c handling.' },
  { key: 'mailing-list-reality', q: 'Search the drbd-user mailing list (mail-archive.com/drbd-user@lists.linbit.com), LINBIT community forum, and GitHub LINBIT/drbd issues for REAL threads about: "quorum lost primary new current UUID", "split brain after quorum loss", "incremental resync after quorum", "minority primary divergence", suspended primary rotates uuid. What do LINBIT engineers (Lars Ellenberg, Philipp Reisner, Joel Colledge) say is the CORRECT configuration? Is the loser-rotation an acknowledged bug, intended behavior, or a misconfiguration on the reporter\'s side? Quote specific messages with URLs.' },
  { key: 'linstor-proxmox-practice', q: 'How do production DRBD-9 stacks configure DRBD to survive Primary failover without split-brain? Examine: LINSTOR (resource-definition / drbd-options defaults it sets — does it set quorum, on-no-quorum, fencing, after-sb? what values?), Proxmox VE DRBD plugin, and Pacemaker+DRBD (the drbd resource agent + fencing). Do ANY of them patch the DRBD module, or do they all rely on native config (fencing/quorum/after-sb)? What is the de-facto correct option set used in the field? Search LINSTOR docs/source, Proxmox wiki, ClusterLabs DRBD docs.' },
  { key: 'on-no-quorum-variants', q: 'Compare on-no-quorum=io-error vs on-no-quorum=suspend-io in detail. Does io-error (instead of suspend-io) change whether the losing Primary runs the 2PC and rotates its current-UUID? In some HA designs io-error is preferred so the minority instantly errors and the upper layer fails over cleanly. Does io-error avoid the susp_uuid false->true edge (drbd_state.c:4466) that fires under suspend-io? Also fully specify quorum-minimum-redundancy semantics and whether it affects rotation. Quote source + manpage.' },
  { key: 'refute-the-leak', q: 'ADVERSARIAL re-derivation: try HARD to REFUTE the claim that "a frozen quorum-lost Primary keeping one peer self-rotates its current-UUID with no quorum guard." Re-read drbd_state.c:4295-4305 and :4466-4471 IN FULL CONTEXT (the enclosing function, any outer if/guard, the w_after_state_change caller). Is the rotation path actually reachable for a node with PRIMARY_LOST_QUORUM set and zero quorum? Could our empirical "new current UUID: CB50" line have a DIFFERENT cause (e.g., the kept peer briefly gave quorum, a transient quorum regain, the 2PC promoting the surviving minority node, or the rotation being the CORRECT delta-tracking bump that actually still heals incrementally)? Is the rotation maybe HARMLESS because the old current goes into the bitmap slot and the comparison still matches? Determine from source whether the observed split-brain is INEVITABLE from this rotation or was an artifact of the missing fencing/after-sb config.' },
]

phase('Research')
const research = (await parallel(AREAS.map(a => () => agent(
  MISSION + '\n\nAREA: ' + a.key + '\n\nQUESTIONS:\n' + a.q + '\n\n' + EMPIRICS + '\n\n' + SOURCES,
  { label: 'study:' + a.key, phase: 'Research', schema: FINDINGS, agentType: 'general-purpose' }
)))).filter(Boolean)

phase('Synthesize')
const SYNTH = {
  type: 'object',
  additionalProperties: false,
  properties: {
    native_solution_exists: { type: 'string', enum: ['yes-config-only', 'yes-with-handlers', 'partial', 'no-patch-required'], description: 'Verdict on whether the GOAL is achievable with stock DRBD config/handlers, no kernel patch.' },
    exact_config: { type: 'string', description: 'The EXACT drbd.conf/drbdsetup options + any fence/after-sb handlers that achieve the goal natively. Concrete, paste-ready. If no native solution, write "NONE".' },
    why_it_works: { type: 'string', description: 'Mechanistically why this config yields incremental, split-brain-free heal on quorum-loss failover. Or, if no-patch-required is false, why every native lever fails.' },
    fencing_role: { type: 'string', description: 'Exact role of fencing/outdating in the solution (or why it does not help).' },
    after_sb_role: { type: 'string', description: 'Exact role of after-sb-* auto-recovery (or why it does not help).' },
    bedrock_d_operations: { type: 'string', description: 'What bedrock-d must do (promote/resume/outdate/fence-handler) given this native config.' },
    residual_risks: { type: 'string', description: 'Remaining risks/edge cases of the native solution; data-safety in worst case.' },
    patch_still_needed: { type: 'string', description: 'If a kernel patch is still needed for full correctness, say exactly for what residual case and why config cannot cover it. Else "no".' },
    load_bearing_claims: { type: 'array', items: { type: 'string' }, description: 'Every claim the recommendation depends on, each independently checkable.' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
  required: ['native_solution_exists', 'exact_config', 'why_it_works', 'fencing_role', 'after_sb_role', 'bedrock_d_operations', 'residual_risks', 'patch_still_needed', 'load_bearing_claims', 'confidence'],
}
const synth = await agent(
  'You are synthesizing a decisive answer to: CAN we make DRBD-9 honor "quorum all" (split-brain-free incremental heal on quorum-loss failover) with NATIVE config only, NO kernel patch? Lean toward finding the native solution if the evidence supports one; do not default to "patch needed" unless the source genuinely forecloses every native lever.\n\n' + MISSION + '\n\n' + EMPIRICS + '\n\nRESEARCH DOSSIERS:\n' + JSON.stringify(research, null, 1),
  { label: 'synthesize', phase: 'Synthesize', schema: SYNTH }
)

phase('Verify')
const VERDICT = {
  type: 'object',
  additionalProperties: false,
  properties: {
    claim: { type: 'string' },
    verdict: { type: 'string', enum: ['CONFIRMED', 'PLAUSIBLE', 'REFUTED'] },
    evidence: { type: 'string', description: 'Quoted source/doc/manpage text with file:line or URL that confirms or refutes.' },
  },
  required: ['claim', 'verdict', 'evidence'],
}
const verdicts = (await parallel((synth.load_bearing_claims || []).map((c, i) => () => agent(
  'Verify this DRBD claim against the kernel source (/tmp/drbdsrc/drbd, /home/tommy/projects/drbdsrc_clone/drbd) and official docs/manpages/mailing-list. Try HARD to REFUTE it. Quote the exact line/section.\n\nCLAIM: ' + c + '\n\n' + SOURCES,
  { label: 'verify:' + (i + 1), phase: 'Verify', schema: VERDICT, agentType: 'general-purpose' }
)))).filter(Boolean)

phase('Finalize')
const final = await agent(
  'Produce the FINAL verdict and recipe. Incorporate the verification — if a load-bearing claim was REFUTED, adjust the recommendation accordingly. Be decisive: state whether the native config-only solution is real, give the exact paste-ready config + handlers, the bedrock-d operation sequence, and whether ANY kernel patch remains necessary (and for exactly what residual case). If native config fully solves it, say so plainly and explain why the earlier "must patch" conclusion was wrong.\n\nSYNTHESIS:\n' + JSON.stringify(synth, null, 1) + '\n\nVERIFICATION:\n' + JSON.stringify(verdicts, null, 1),
  { label: 'finalize', phase: 'Finalize', schema: SYNTH }
)

return { final, verdicts, synth_native_verdict: synth.native_solution_exists }
