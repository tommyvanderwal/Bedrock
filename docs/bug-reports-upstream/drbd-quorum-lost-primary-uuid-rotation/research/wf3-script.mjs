export const meta = {
  name: 'drbd-resync-after-zerobyte-rotation',
  description: 'Factually trace DRBD-9 reconvergence after a 0-write quorum-lost minority rotates its current-UUID: does the clean dirty-bitmap make the two UUIDs equivalent for an efficient resync, or is it split-brain?',
  phases: [
    { title: 'Research', detail: '6 parallel: post-partition UUID+bitmap state, uuid_compare trace both directions, does bitmap content gate strategy, lost-quorum/weak flag reconcile, resync cost, weak-node semantics' },
    { title: 'Synthesize', detail: 'Definitive factual reconvergence sequence + answer the clean-bitmap hypothesis' },
    { title: 'Verify', detail: 'Adversarially verify each load-bearing claim against source' },
    { title: 'Finalize', detail: 'Factual answer: is an efficient resync possible, and under exactly what condition' },
  ],
}

const QUESTION = [
  'CORE QUESTION (Tommy): After a quorum-lost minority (2 of 4 nodes) rotates its current-UUID despite writing',
  'ZERO data bytes, how does reconvergence FACTUALLY take place when the partition heals against the quorate',
  'majority (where a new Primary was force-promoted and DID write)? Hypothesis to PROVE or BREAK: the minority',
  'did 0 updates, so its DIRTY-BLOCK BITMAP is completely clean relative to the peers it lost. Does that clean',
  'bitmap make the two diverged current-UUIDs effectively "the same" for resync purposes, so a fairly EFFICIENT',
  '(incremental, one-directional) resync is possible -- rather than a full resync / split-brain?',
  '',
  'CRITICAL DISTINCTION you must keep explicit throughout: DRBD has TWO different things both loosely called',
  '"bitmap": (1) the BITMAP-UUID = a per-peer GENERATION TAG stored in metadata (used by drbd_uuid_compare to',
  'pick the resync strategy); (2) the DIRTY-BLOCK BITMAP = the actual on-disk map of CHANGED BLOCKS (used to',
  'size an incremental resync once a direction is chosen). Tommy\'s "clean bitmap" refers to (2). Determine',
  'whether (2) ever influences the STRATEGY decision, or whether the strategy is decided purely from the',
  'generation UUIDs (1) and the "weak" flags, with (2) consulted only AFTER a sync direction is set.',
].join('\n')

const HYPOTHESIS_TO_TEST = [
  'A PARALLEL investigation (26 agents, source-verified against the same 9.3.2 tree) produced the following',
  'chain. Your job is to INDEPENDENTLY CONFIRM or REFUTE each link against the source, and to QUANTIFY the',
  'resync cost it implies. Do not assume it is correct -- re-derive from /tmp/drbdsrc.',
  '  H1: On heal, BOTH sides have the common ancestor C0 in their per-peer BITMAP-UUID slot (because',
  '      rotate_current_into_bitmap stored the old current C0 there, drbd_main.c:4875/4900) -- NOT in history.',
  '  H2: drbd_uuid_compare therefore hits RULE_BITMAP_BOTH (drbd_receiver.c:4888-4892): self==peer==C0!=0 ->',
  '      returns SPLIT_BRAIN_AUTO_RECOVER. (RULE_HISTORY_BOTH -> SPLIT_BRAIN_DISCONNECT at :4907 only if the',
  '      ancestor were solely in HISTORY.)',
  '  H3: SPLIT_BRAIN_AUTO_RECOVER does NOT auto-heal -- its descriptor has .disconnect=true; it ROUTES into',
  '      drbd_asb_recover_{0,1,2}p. Under the DEFAULT after-sb=ASB_DISCONNECT those return SPLIT_BRAIN_DISCONNECT',
  '      -> StandAlone. THIS (unset after-sb) is why our test split-braind, NOT a hard divergence.',
  '  H4: need_full_sync_after_split_brain=(strategy==SPLIT_BRAIN_DISCONNECT) is computed BEFORE remap',
  '      (drbd_receiver.c:5404), so it is FALSE for AUTO_RECOVER -> the full-sync promotion (:5434-5441) is',
  '      SKIPPED -> the resync stays INCREMENTAL (SYNC_TARGET_USE_BITMAP, distinct from full SYNC_TARGET_SET_BITMAP).',
  '  H5: with after-sb-0pri=discard-zero-changes, the zero-write loser (ch_self==comm_bm_set==0) maps to',
  '      SYNC_TARGET_USE_BITMAP (drbd_receiver.c:4330-4332); its out-of-sync bitmap toward the survivor is ~empty.',
  '  H6: ALTERNATIVELY, if bedrock-d OUTDATES the minority loser first (drbdadm outdate), drbd_data_accessible',
  '      becomes false so create_new_uuid never arms -> the loser NEVER rotates -> self==peer survives ->',
  '      RULE_LOST_QUORUM (drbd_receiver.c:4785-4798, keyed on UUID_FLAG_PRIMARY_LOST_QUORUM, inside the self==peer',
  '      block at :4742) -> a directional IF_BOTH_FAILED resync carrying ZERO diverged data.',
  'KEY QUANTIFICATION TASK: under H5 (loser rotated, discard-zero-changes) vs H6 (loser outdated, no rotation),',
  'how many BLOCKS actually transfer on heal? Is H5 truly "~0 blocks" or does the survivor\'s OWN writes-since-',
  'partition (its dirty bitmap toward the absent loser) get pushed to the loser too? Give best-case AND worst-case.',
].join('\n')

const EMPIRICS = [
  'GROUND TRUTH (4-node cluster, DRBD 9.3.2, "quorum all; on-no-quorum suspend-io; auto-promote no"):',
  'Pre-partition: all 4 nodes share a common current-UUID; call it C.',
  'Scenario B: the Primary (sim-1) loses 2 of 3 peers (sim-3, sim-4) but keeps sim-2 -> suspended:quorum,',
  '  writes ZERO bytes, BUT rotates its current-UUID. Captured kernel line on sim-1:',
  '    "drbd1101: new current UUID: CB5081C7F838246D weak: FFFFFFFFFFFFFFFC"',
  '  weak mask FFFFFFFFFFFFFFFC = bits 0,1 CLEAR (node0 sim-1 self + node1 sim-2 kept peer NOT weak), bits 2,3',
  '  SET (node2 sim-3 + node3 sim-4 lost peers stamped WEAK); the old current C rotated into their bitmap-UUID slots.',
  'Majority side: a survivor (e.g. sim-3) was force-promoted -> minted its OWN new current-UUID M (child of C),',
  '  then served live IO -> its DIRTY-BLOCK bitmap toward the absent minority accumulates the blocks it wrote.',
  'On heal: DRBD declared StandAlone / split-brain and demanded full resync / --discard-my-data, NOT a clean',
  '  incremental resync. We need the FACTUAL reason, traced in source, and whether a config or natural path',
  '  yields an efficient resync given the minority wrote 0.',
  '',
  'UUID state to reason about (derive precisely from source -- correct me if wrong):',
  '  MINORITY sim-1: current = L (=CB50...), bitmap-uuid[sim-3]=C, bitmap-uuid[sim-4]=C, dirty-block-bitmap = EMPTY (0 writes), weak{sim-3,sim-4}.',
  '  MAJORITY sim-3: current = M (child of C), bitmap-uuid[sim-1]=C, bitmap-uuid[sim-2]=C, dirty-block-bitmap[sim-1] = blocks written since partition.',
].join('\n')

const SOURCES = [
  'SOURCES -- quote actual text with file:line or URL:',
  '- DRBD source cloned locally: /tmp/drbdsrc/drbd and /home/tommy/projects/drbdsrc_clone/drbd (9.3.2, HEAD a46cbd9).',
  '  Key: drbd_receiver.c drbd_uuid_compare() (~4678) + the RULE_* enum + sync_strategy enum; drbd_main.c',
  '  drbd_uuid_new_current / __new_current_uuid_prepare (4947-4981) / rotate_current_into_bitmap (~4837) /',
  '  __drbd_uuid_set_bitmap / the UUID flag bits (UUID_FLAG_*, the "weak" mask); drbd_bitmap.c (dirty-block',
  '  bitmap, drbd_bm_total_weight); drbd_state.c (RULE_LOST_QUORUM usage, weak-node handling, resync start);',
  '  drbd_nl.c (drbdadm --discard-my-data / new-current-uuid / invalidate paths).',
  '- DRBD 9 User Guide section 16.2 generation identifiers + "three-way handshake" / resync decision;',
  '  "Resolving split brain" + after-sb-* (linbit.com/drbd-user-guide/drbd-guide-9_0-en/).',
  '- drbdsetup-9.0 / drbd.conf-9.0 manpages; LINBIT blog on generation UUIDs + quorum.',
  '- Use WebSearch/WebFetch for docs; ToolSearch ("select:WebFetch,WebSearch") if a tool is not loaded.',
].join('\n')

const FINDINGS = {
  type: 'object',
  additionalProperties: false,
  properties: {
    area: { type: 'string' },
    summary: { type: 'string', description: 'Evidence-based factual prose answering the assigned questions.' },
    answer_to_hypothesis: { type: 'string', description: 'Directly: does the clean DIRTY-BLOCK bitmap make the two current-UUIDs equivalent for resync / enable an efficient resync? yes/no/partial + why, from source.' },
    rules: { type: 'array', items: { type: 'object', additionalProperties: false, properties: {
      rule: { type: 'string' },
      evidence: { type: 'string' },
      source: { type: 'string' },
      confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    }, required: ['rule', 'evidence', 'source', 'confidence'] } },
    open: { type: 'array', items: { type: 'string' } },
  },
  required: ['area', 'summary', 'answer_to_hypothesis', 'rules', 'open'],
}

const AREAS = [
  { key: 'post-partition-uuid-bitmap-state', q: 'Establish the EXACT on-disk state on BOTH sides after scenario B + force-promote. (1) On the MINORITY (sim-1) when drbd_uuid_new_current runs with 0 writes: trace rotate_current_into_bitmap (~drbd_main.c:4837) and __new_current_uuid_prepare -- what does it write into current-uuid, into each bitmap-UUID slot (the GENERATION TAG), into history, and into the "weak" mask? CRUCIAL: does it touch the DIRTY-BLOCK bitmap (drbd_bitmap.c) AT ALL, or only the UUID generation tags? Confirm the minority dirty-block bitmap stays all-zero (drbd_bm_total_weight == 0) because it wrote nothing. (2) On the MAJORITY (sim-3): force-promote mints M and live writes set dirty bits toward the absent minority. State both sides\' complete (current, bitmap-uuid[peer], history, weak, dirty-block-weight) tuple.' },
  { key: 'uuid-compare-trace-both-directions', q: 'Trace drbd_uuid_compare() (drbd_receiver.c ~4678) STEP BY STEP for BOTH directions with the scenario-B state: (a) majority self=M, peer=minority(L,bm=C); (b) minority self=L, peer=majority(M,bm=C). For each: which RULE_* branch matches (RULE_LOST_QUORUM, RULE_BITMAP_PEER, RULE_RECONNECTED, RULE_*_SELF/PEER, the diverged/split-brain rule)? What sync_strategy enum is returned (SYNC_SOURCE_*, SYNC_TARGET_*, SPLIT_BRAIN_*, UNRELATED_DATA, NO_SYNC)? Does DRBD require BOTH directions to agree before resyncing, and what happens if one side says incremental-SyncTarget but the other says split-brain? Quote the exact branches and enum values that produce the observed StandAlone/split-brain.' },
  { key: 'does-dirty-bitmap-gate-strategy', q: 'THE HYPOTHESIS. Determine factually: is the resync STRATEGY (split-brain vs incremental vs full) decided PURELY from the generation-UUIDs (bitmap-UUID tags + weak flags) in drbd_uuid_compare, or does the DIRTY-BLOCK bitmap CONTENT (drbd_bm_total_weight / whether it is clean) influence the strategy choice? Is there ANY code path where a clean dirty-block bitmap (0 dirty blocks) causes a node\'s rotated current-UUID to be treated as equivalent-to / reconciled-with the peer (so no split-brain)? Or is the dirty-block bitmap consulted ONLY after a sync direction is already chosen, purely to size the transfer? Find the exact line where strategy is decided vs where bm weight is read, and prove the ordering.' },
  { key: 'lost-quorum-and-weak-flag-reconcile', q: 'Investigate the RULE_LOST_QUORUM path and the UUID FLAG bits. (1) RULE_LOST_QUORUM in drbd_uuid_compare: under exactly what UUID/flag state does it yield a clean incremental resync? Does it require self.current==peer.current (no rotation), or can it reconcile a peer that rotated? (2) Is there a PERSISTED UUID flag bit (UUID_FLAG_LOST_QUORUM / UUID_FLAG_* / the "weak" bits) that records "this node lost quorum / is weak, accept resync from the quorate side", so the rotated minority can still be auto-recognized as a subordinate SyncTarget rather than a split-brain peer? (3) Does the "weak" mask the minority stamped (weak{sim-3,sim-4}) -- combined with the majority stamping weak{sim-1,sim-2} -- produce MUTUAL weakness that drbd_uuid_compare reads as split-brain? Trace the flag handling.' },
  { key: 'resync-cost-after-resolution', q: 'Suppose the split-brain is resolved (operator drbdadm --discard-my-data on the minority, or an after-sb-* policy auto-resolves it). What is the ACTUAL resync cost? Does --discard-my-data / the chosen recovery trigger a FULL resync (entire device) or a BITMAP-based incremental resync (only the blocks in the majority\'s dirty-block bitmap = only what the majority wrote since partition)? The minority wrote 0, so the optimal delta is just the majority\'s writes. Trace the discard-my-data code path (drbd_nl.c invalidate / bitmap handling) and determine best-case vs worst-case resync size. Does losing the UUID lineage force a full resync, or does the bitmap-UUID==C linkage still permit a bounded incremental?' },
  { key: 'weak-node-semantics', q: 'DRBD 9 multi-node "weak" node semantics and the "weak: %016llX" mask. What does it mean for a node/peer to be "weak" in a generation? When a weak node reconnects to a strong/quorate node, what resync rule applies -- is a node that bumped-while-weak automatically a SyncTarget (efficient one-way resync from the strong side), or is mutual-weak treated as split-brain? Is "weak" the mechanism that SHOULD let the 0-write minority heal cheaply (it knows it is weak/behind), and if so why did our test still split-brain? Trace the weak handling in drbd_uuid_compare / drbd_state.c / drbd_receiver.c and reconcile with the observed full-resync/split-brain outcome.' },
]

phase('Research')
const research = (await parallel(AREAS.map(a => () => agent(
  QUESTION + '\n\nAREA: ' + a.key + '\n\nQUESTIONS:\n' + a.q + '\n\n' + HYPOTHESIS_TO_TEST + '\n\n' + EMPIRICS + '\n\n' + SOURCES,
  { label: 'r:' + a.key, phase: 'Research', schema: FINDINGS, agentType: 'general-purpose' }
)))).filter(Boolean)

phase('Synthesize')
const SYNTH = {
  type: 'object',
  additionalProperties: false,
  properties: {
    reconvergence_sequence: { type: 'string', description: 'The factual step-by-step of what DRBD does on heal, from reconnect handshake through resync-or-split-brain, for the scenario-B state.' },
    clean_bitmap_makes_uuids_equivalent: { type: 'string', enum: ['yes', 'no', 'partial'], description: 'Decisive answer to Tommy\'s hypothesis.' },
    why: { type: 'string', description: 'Mechanistic why, citing where strategy is decided (UUIDs/weak) vs where the dirty-block bitmap is read (sizing only).' },
    efficient_resync_possible: { type: 'string', enum: ['yes-natively', 'yes-after-manual-resolve', 'only-full-resync', 'no'], description: 'Is an efficient (bounded incremental) resync achievable, and how.' },
    resync_cost: { type: 'string', description: 'Best-case and worst-case resync size for the scenario-B heal, and what determines which.' },
    weak_flag_role: { type: 'string', description: 'What the "weak" mask does to the heal outcome; is mutual-weak the split-brain cause.' },
    condition_for_efficiency: { type: 'string', description: 'The exact condition under which the resync is efficient (e.g. minority must NOT have rotated, or a flag must be set, or a specific recovery command).' },
    load_bearing_claims: { type: 'array', items: { type: 'string' } },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
  required: ['reconvergence_sequence', 'clean_bitmap_makes_uuids_equivalent', 'why', 'efficient_resync_possible', 'resync_cost', 'weak_flag_role', 'condition_for_efficiency', 'load_bearing_claims', 'confidence'],
}
const synth = await agent(
  'Synthesize the FACTUAL reconvergence trace. Keep BITMAP-UUID (generation tag) and DIRTY-BLOCK bitmap strictly separate. Answer Tommy\'s hypothesis decisively.\n\n' + QUESTION + '\n\n' + EMPIRICS + '\n\nDOSSIERS:\n' + JSON.stringify(research, null, 1),
  { label: 'synthesize', phase: 'Synthesize', schema: SYNTH }
)

phase('Verify')
const VERDICT = {
  type: 'object',
  additionalProperties: false,
  properties: {
    claim: { type: 'string' },
    verdict: { type: 'string', enum: ['CONFIRMED', 'PLAUSIBLE', 'REFUTED'] },
    evidence: { type: 'string' },
  },
  required: ['claim', 'verdict', 'evidence'],
}
const verdicts = (await parallel((synth.load_bearing_claims || []).map((c, i) => () => agent(
  'Verify this DRBD resync/UUID claim against the kernel source (/tmp/drbdsrc/drbd) and official docs. Try HARD to REFUTE. Quote the exact line/section.\n\nCLAIM: ' + c + '\n\n' + SOURCES,
  { label: 'verify:' + (i + 1), phase: 'Verify', schema: VERDICT, agentType: 'general-purpose' }
)))).filter(Boolean)

phase('Finalize')
const final = await agent(
  'Produce the FINAL factual answer to Tommy: after a 0-write quorum-lost minority rotates its current-UUID, how does reconvergence actually take place, and does the clean dirty-block bitmap make an efficient resync possible? Incorporate verification (drop/adjust REFUTED claims). Be concrete: (1) the step-by-step heal sequence; (2) yes/no does clean bitmap make the UUIDs equivalent for resync, with the mechanism; (3) is an efficient incremental resync achievable and under exactly what condition; (4) best/worst-case resync size; (5) the role of the "weak" mask.\n\nSYNTHESIS:\n' + JSON.stringify(synth, null, 1) + '\n\nVERIFICATION:\n' + JSON.stringify(verdicts, null, 1),
  { label: 'finalize', phase: 'Finalize', schema: SYNTH }
)

return { final, verdicts }
