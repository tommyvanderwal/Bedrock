export const meta = {
  name: 'drbd-quorum-all-actual-effect',
  description: 'Determine precisely what quorum=all enforces in DRBD-9: does a 2-of-4 minority actually keep writing / mint UUIDs, or only the metadata bump slips past suspend-io?',
  phases: [
    { title: 'Research', detail: '6 parallel: enumerate every quorum-gate, data-vs-metadata write path, voter-count correctness, what LINBIT says quorum guarantees' },
    { title: 'Synthesize', detail: 'What quorum=all actually does and does not enforce, decisively' },
    { title: 'Verify', detail: 'Adversarially verify each load-bearing claim against source' },
    { title: 'Finalize', detail: 'Plain-language answer to: does quorum=all do anything, and is the UUID bump a contradiction of it' },
  ],
}

const QUESTION = [
  'CORE QUESTION (Tommy, product owner): "What is the point of quorum=all if a 2-of-3 or 2-of-4 minority',
  'just generates a new UUID and goes on? Does the quorum=all tag actually do anything at all?"',
  '',
  'We must answer this with ZERO assumptions, from the DRBD kernel source + official docs. The suspected',
  'resolution (PROVE or BREAK it): suspend-io suspends the DATA path (application writes never reach disk on',
  'the minority -> no user-data divergence, which IS the core safety property quorum=all provides), but the',
  'current-UUID rotation is a METADATA operation (drbd_uuid_new_current -> drbd_md_sync) on the worker thread',
  'that is NOT covered by the data-IO suspension -> the generation bump slips past quorum=all. If true:',
  'quorum=all DOES do its job (no divergent committed data) but does NOT stop the internal UUID bump (a heal-',
  'efficiency leak, not a data-loss bug). Determine the truth from source. Distinguish DATA writes from',
  'METADATA writes explicitly.',
].join('\n')

const EMPIRICS = [
  'OBSERVED (4-node cluster, DRBD 9.3.2, options "quorum all; on-no-quorum suspend-io; auto-promote no"):',
  '(A) Primary loses ALL peers at once -> "suspended:quorum", role stays Primary, wrote nothing, current-UUID',
  '    did NOT change.',
  '(B) Primary loses SOME peers but keeps >=1 -> "suspended:quorum" (2 of 4, lost quorum), wrote nothing,',
  '    BUT kernel log shows "new current UUID: CB50...". So: NO user-data write, but YES a UUID rotation.',
  'Question the framing "goes on writing": confirm whether the minority actually writes ANY user data, or only',
  'performs the internal metadata UUID bump while the data path stays frozen.',
].join('\n')

const SOURCES = [
  'SOURCES — quote actual text with file:line or URL:',
  '- DRBD source cloned locally: /tmp/drbdsrc/drbd and /home/tommy/projects/drbdsrc_clone/drbd (9.3.2, HEAD a46cbd9).',
  '  Grep/Read: drbd_state.c, drbd_main.c, drbd_req.c (the request/write path + suspend checks), drbd_sender.c,',
  '  drbd_receiver.c, drbd_nl.c, drbd_worker.c. The metadata write is drbd_md_sync; the data-IO suspend is',
  '  susp / susp_quorum / drbd_suspended(); the request path is drbd_make_request / __drbd_make_request / w_send_*.',
  '- DRBD 9 User Guide quorum chapter (linbit.com/drbd-user-guide/drbd-guide-9_0-en/), drbd.conf-9.0 + drbdsetup-9.0',
  '  manpages (quorum, on-no-quorum, quorum-minimum-redundancy), LINBIT blog "DRBD quorum".',
  '- Use WebSearch/WebFetch for the docs; ToolSearch ("select:WebFetch,WebSearch") if a tool is not loaded.',
].join('\n')

const FINDINGS = {
  type: 'object',
  additionalProperties: false,
  properties: {
    area: { type: 'string' },
    summary: { type: 'string' },
    quorum_enforces: { type: 'array', items: { type: 'string' }, description: 'Concrete things losing quorum (QOU_ALL) ACTUALLY forbids, each with a source cite.' },
    quorum_does_not_enforce: { type: 'array', items: { type: 'string' }, description: 'Things that STILL happen on a quorum-lost node (e.g. metadata UUID bump), each with a source cite.' },
    rules: { type: 'array', items: { type: 'object', additionalProperties: false, properties: {
      rule: { type: 'string' },
      evidence: { type: 'string' },
      source: { type: 'string' },
      confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    }, required: ['rule', 'evidence', 'source', 'confidence'] } },
    open: { type: 'array', items: { type: 'string' } },
  },
  required: ['area', 'summary', 'quorum_enforces', 'quorum_does_not_enforce', 'rules', 'open'],
}

const AREAS = [
  { key: 'enumerate-quorum-gates', q: 'Enumerate EVERY place in the DRBD source where quorum / have_quorum / susp_quorum / PRIMARY_LOST_QUORUM / drbd_data_accessible is consulted to ALLOW or FORBID an action. For each: what is gated (application write, barrier/flush, promotion to Primary, state change, UUID generation, resync start)? Build the complete forbidden-when-no-quorum vs still-permitted lists. The point: is "minting a new current-UUID" supposed to be in the forbidden set, and on which code paths is it actually gated vs not?' },
  { key: 'data-vs-metadata-write', q: 'Decisively separate DATA writes from METADATA writes under no-quorum. (1) Trace the application write path (drbd_make_request / __drbd_make_request / drbd_request_prepare) and show EXACTLY where suspend-io / susp_quorum blocks an application write so it never reaches the backing disk. (2) Trace drbd_uuid_new_current -> drbd_md_sync and show it writes DRBD METADATA (the on-disk UUID area), NOT user data, and runs on the worker thread OUTSIDE the suspended request path. Confirm or refute: with quorum=all + suspend-io, NO user data is written on the minority, but the metadata UUID can still change. This is the crux of the whole question.' },
  { key: 'does-minority-ever-write-data', q: 'Could a 2-of-4 (or 2-of-3) minority EVER commit application data to disk under quorum=all + on-no-quorum=suspend-io? Examine: is suspend-io a HARD block (requests queued/blocked indefinitely) or a soft one? Any path where a write completes locally before replication confirms quorum (protocol A/B vs C)? Any window between quorum loss detection and IO freeze where an in-flight write commits? The user fears the minority "goes on writing" — establish definitively whether that is possible or not. Quote drbd_req.c.' },
  { key: 'voter-count-correctness', q: 'How is the voter count (the denominator for QOU_ALL = voters) computed (calc_quorum_at, the voters/quorum bookkeeping)? Do DISKLESS nodes count as voters? Does a diskless arbiter / tiebreaker change it? CRUCIAL for Bedrock: our arbiter resource was observed to be 4-WAY. Is quorum=all on a 4-way resource behaving as expected (need all 4), and could a misconfigured voter count make quorum=all weaker or stronger than intended? How do we VERIFY via drbdsetup status/show that quorum=all is actually in effect and not silently ignored/defaulted?' },
  { key: 'what-linbit-says-quorum-guarantees', q: 'From LINBIT official docs/blog/manpages: what does LINBIT SAY quorum=all guarantees? Quote the exact promise. Does LINBIT claim quorum prevents DATA divergence (split-brain of committed data), or does it only promise "the minority stops doing IO"? Does LINBIT anywhere claim quorum prevents autonomous UUID/generation changes? Is the generation-UUID bump on a frozen minority documented anywhere as intended bookkeeping (so the majority knows the minority is stale) vs an oversight? This tells us whether the UUID bump CONTRADICTS quorum=all\'s stated purpose or is consistent with it.' },
  { key: 'uuid-bump-purpose-when-frozen', q: 'WHY does DRBD bump the current-UUID on a node that is frozen and writing nothing? What is the bump FOR in this case? Normally a Primary bumps its UUID when it makes changes a disconnected peer lacks (so resync is needed). But here there are zero changes. Is the bump (a) correct bookkeeping that still heals incrementally because the old UUID lands in the bitmap slot, or (b) a genuine defect that creates a sibling generation and breaks incremental heal? Trace what drbd_uuid_new_current does to bitmap-uuid and history, and whether the resulting state heals incrementally against a majority that also rotated from the same ancestor. Reconcile with the observed split-brain.' },
]

phase('Research')
const research = (await parallel(AREAS.map(a => () => agent(
  QUESTION + '\n\nAREA: ' + a.key + '\n\nQUESTIONS:\n' + a.q + '\n\n' + EMPIRICS + '\n\n' + SOURCES,
  { label: 'q:' + a.key, phase: 'Research', schema: FINDINGS, agentType: 'general-purpose' }
)))).filter(Boolean)

phase('Synthesize')
const SYNTH = {
  type: 'object',
  additionalProperties: false,
  properties: {
    does_quorum_all_do_anything: { type: 'string', enum: ['yes-it-blocks-data-writes', 'no-effectively-useless', 'partial'], description: 'Decisive verdict.' },
    what_it_enforces: { type: 'string', description: 'Plain statement of exactly what quorum=all + suspend-io enforces (and the source of the guarantee).' },
    what_it_does_not_enforce: { type: 'string', description: 'Exactly what still happens on a quorum-lost minority (the UUID bump), and why.' },
    does_minority_write_user_data: { type: 'string', description: 'yes/no + proof. The direct answer to "does it go on writing".' },
    uuid_bump_is: { type: 'string', enum: ['harmless-bookkeeping', 'heal-efficiency-leak', 'data-safety-bug'], description: 'Classify the UUID bump.' },
    voter_count_note: { type: 'string', description: 'Whether quorum=all is actually in effect on our 4-way arbiter and how to verify.' },
    plain_answer: { type: 'string', description: 'A direct, plain-language answer to Tommy: does quorum=all do anything, and is the UUID bump a contradiction of it.' },
    load_bearing_claims: { type: 'array', items: { type: 'string' } },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
  required: ['does_quorum_all_do_anything', 'what_it_enforces', 'what_it_does_not_enforce', 'does_minority_write_user_data', 'uuid_bump_is', 'voter_count_note', 'plain_answer', 'load_bearing_claims', 'confidence'],
}
const synth = await agent(
  'Synthesize a decisive answer to: does quorum=all actually do anything, given a 2-of-4 minority mints a new UUID? Separate DATA from METADATA crisply.\n\n' + QUESTION + '\n\n' + EMPIRICS + '\n\nDOSSIERS:\n' + JSON.stringify(research, null, 1),
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
  'Verify this DRBD claim against the kernel source (/tmp/drbdsrc/drbd) and official docs. Try HARD to REFUTE. Quote exact line/section.\n\nCLAIM: ' + c + '\n\n' + SOURCES,
  { label: 'verify:' + (i + 1), phase: 'Verify', schema: VERDICT, agentType: 'general-purpose' }
)))).filter(Boolean)

phase('Finalize')
const final = await agent(
  'Produce the FINAL plain-language answer to Tommy: does quorum=all do anything, or does a 2-of-4 minority just mint a UUID and carry on? Be decisive and concrete. Incorporate the verification (drop/adjust any REFUTED claim). Cover: (1) does the minority write user data — yes/no with proof; (2) what quorum=all actually enforces; (3) what slips past it (the metadata UUID bump) and why; (4) is that a data-safety problem or only a heal-efficiency one; (5) whether quorum=all is genuinely in effect on our 4-way arbiter.\n\nSYNTHESIS:\n' + JSON.stringify(synth, null, 1) + '\n\nVERIFICATION:\n' + JSON.stringify(verdicts, null, 1),
  { label: 'finalize', phase: 'Finalize', schema: SYNTH }
)

return { final, verdicts }
