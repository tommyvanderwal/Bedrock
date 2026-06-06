export const meta = {
  name: 'drbd-racefree-prevent-uuid-rotation',
  description: 'Exhaust every race-free way to prevent a quorum-lost Primary self-minting a UUID with stock DRBD; rigorously confirm the bug; draft the LINBIT bug report',
  phases: [
    { title: 'Research', detail: '7 parallel: io-error mode, every config/module knob, force the guarded route, is-it-a-bug adversarial, the 9.2.18 diskless precedent, minimal kernel fix, LINBIT report format' },
    { title: 'Synthesize', detail: 'Is there a race-free config prevention? Is it a confirmed bug? The minimal fix.' },
    { title: 'Verify', detail: 'Adversarially verify each load-bearing claim against source' },
    { title: 'Finalize', detail: 'Decision + full LINBIT bug-report draft' },
  ],
}

const MISSION = [
  'MISSION: A quorum-lost, FROZEN, ZERO-WRITE DRBD-9 Primary that keeps >=1 peer self-mints a new current-UUID',
  '(via an UNGUARDED state-change path), causing a false split-brain / unnecessary full resync on heal. The product',
  'owner (Tommy) has REJECTED two proposed workarounds as too dodgy: (1) after-sb discard-zero-changes (depends on',
  'the 0-write guarantee; unsafe the moment both sides have a real dirty bit), and (2) bedrock-d running',
  '`drbdadm outdate` before the kernel rotates (a RACE the userspace daemon usually LOSES, because the kernel arms',
  'create_new_uuid synchronously in finish_state_change at partition-detection time, before bedrock-d even learns of',
  'the partition). GOAL OF THIS INVESTIGATION:',
  '  (A) EXHAUST every STOCK, RACE-FREE way to PREVENT the rotation at its source with unmodified DRBD 9.3.2 -- config',
  '      option, module parameter, sysfs, resource setup, on-no-quorum mode, anything. Be thorough and skeptical;',
  '      do not invent knobs -- prove from source/manpages whether each candidate actually disarms the rotation',
  '      WITHOUT a timing race. If NO race-free config prevention exists, say so plainly and prove it.',
  '  (B) RIGOROUSLY CONFIRM whether this is a genuine DRBD BUG (an incomplete/oversight guard) or intended behavior,',
  '      to high confidence, with the evidence a LINBIT engineer would need.',
  '  (C) Specify the MINIMAL kernel fix LINBIT would most likely accept, and draft a bug report in their preferred',
  '      format. We bundle drbd9x in our ISO so we can ship a patched module ourselves, but we want upstream to fix',
  '      it cleanly long-term.',
].join('\n')

const FACTS = [
  'ESTABLISHED FACTS (source-verified, DRBD 9.3.2, HEAD a46cbd9, /tmp/drbdsrc/drbd):',
  '- ARM (no quorum guard): drbd_state.c:3096-3099 sets create_new_uuid when lost_contact_to_peer_data() AND',
  '  role[NEW]==R_PRIMARY AND !UNREGISTERED AND drbd_data_accessible(device,NEW) (=local disk D_UP_TO_DATE).',
  '  have_quorum is NOT consulted. Glue: :3134-3136 set __NEW_CUR_UUID if (create_new_uuid && !susp_uuid[OLD]).',
  '- EXECUTE (no quorum guard): drbd_state.c:4466-4471 fires drbd_uuid_new_current(device,false) on the',
  '  (!susp_uuid[OLD] && susp_uuid[NEW] && NEW_CUR_UUID) edge; ALSO :4295-4305 on peer disk D_UP_TO_DATE->D_FAILED.',
  '- GUARDED counterparts (the asymmetry): the write route drbd_sender.c:3443 IS gated',
  '  `if (device->have_quorum[NOW] && drbd_data_accessible(device, NOW))`; the disconnect route',
  '  drbd_receiver.c:9884-9888 IS gated `!test_bit(PRIMARY_LOST_QUORUM, &device->flags)` with the explicit comment',
  '  (9868-9872) "But when we lost quorum we are going to finish those requests with error, therefore do not create',
  '  the new UUID immediately!". have_quorum[NEW] / PRIMARY_LOST_QUORUM ARE in scope at the unguarded sites',
  '  (PRIMARY_LOST_QUORUM is set at drbd_state.c:2885-2886; have_quorum[OLD]/[NEW] used at :4463 three lines above',
  '  the unguarded execute) but are not used there.',
  '- PRECEDENT: ChangeLog 9.2.18 (included in 9.3.2) lists "Fix when a diskless primary creates a new current UUID,',
  '  fixing possible silent data divergence later." => LINBIT already fixed the DISKLESS-primary variant of this',
  '  exact class; the DISKFUL frozen-Primary variant remains unguarded. Verify this framing precisely.',
  '- on-no-quorum default is suspend-io (susp_quorum set, data path frozen -> zero user writes proven). The other',
  '  value is io-error (sets cached_err_io, not susp_quorum).',
  '- DRBD has NO autonomous self-outdate under hard partition (all four __outdate_myself sites are 2PC/connectivity-',
  '  gated). Quorum loss only sets susp_io/err_io, never D_OUTDATED.',
  'REPRODUCER (4-node, quorum all + suspend-io + auto-promote no): isolate the Primary from SOME peers while it',
  'KEEPS >=1 (2-of-4) -> suspended:quorum, ZERO writes, but kernel logs "new current UUID: <hex> weak: <mask>".',
  'Isolating it from ALL peers at once does NOT rotate (no surviving 2PC partner).',
].join('\n')

const SOURCES = [
  'SOURCES -- quote actual text with file:line or URL:',
  '- DRBD source: /tmp/drbdsrc/drbd and /home/tommy/projects/drbdsrc_clone/drbd (9.3.2, HEAD a46cbd9). Grep/Read',
  '  drbd_state.c, drbd_main.c, drbd_receiver.c, drbd_sender.c, drbd_nl.c, drbd_req.c, drbd-headers/linux/*.h, ChangeLog.',
  '- drbd.conf-9.0 / drbdsetup-9.0 manpages (manpages.debian.org) -- EVERY net/disk/resource option + defaults.',
  '- DRBD 9 User Guide (linbit.com/drbd-user-guide/drbd-guide-9_0-en/): quorum, fencing, generation identifiers.',
  '- LINBIT bug channels: github.com/LINBIT/drbd/issues (search existing issues for "new current UUID", "quorum",',
  '  "split brain", "data divergence"), drbd-user mailing list (mail-archive.com/drbd-user@lists.linbit.com),',
  '  the drbd-user-guide / CONTRIBUTING / SUBMITTING docs. Find LINBIT\'s PREFERRED report channel + required fields.',
  '- Use WebSearch/WebFetch; ToolSearch ("select:WebFetch,WebSearch") if a tool is not loaded.',
].join('\n')

const FINDINGS = {
  type: 'object',
  additionalProperties: false,
  properties: {
    area: { type: 'string' },
    summary: { type: 'string' },
    race_free_prevention: { type: 'string', description: 'Does THIS area surface a race-free, stock-DRBD way to PREVENT the rotation? Describe it precisely with source proof, or state "none" and why.' },
    rules: { type: 'array', items: { type: 'object', additionalProperties: false, properties: {
      rule: { type: 'string' },
      evidence: { type: 'string' },
      source: { type: 'string' },
      confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
    }, required: ['rule', 'evidence', 'source', 'confidence'] } },
    open: { type: 'array', items: { type: 'string' } },
  },
  required: ['area', 'summary', 'race_free_prevention', 'rules', 'open'],
}

const AREAS = [
  { key: 'io-error-mode', q: 'Does on-no-quorum=io-error (instead of suspend-io) PREVENT the rotation, race-free? Trace precisely: under io-error, susp_quorum is NOT set (drbd_state.c:2473-2474) and cached_err_io IS set (:896-897). Does the EXECUTE at drbd_state.c:4466 (the !susp_uuid[OLD] && susp_uuid[NEW] edge) still fire if the node is never suspended? Is susp_uuid ever set under io-error? Does the :4295 path (peer disk -> D_FAILED) fire on a CLEAN disconnect (peer -> D_UNKNOWN, not D_FAILED)? CONCLUSION NEEDED: does io-error cleanly avoid the bump, and if so at what cost (guest IO errors vs freeze)? Is io-error a viable race-free prevention for a VM-hosting appliance, or does it just trade one problem for guest-disk-error chaos?' },
  { key: 'exhaustive-knob-sweep', q: 'EXHAUSTIVELY enumerate EVERY drbd.conf/drbdsetup option (net, disk, options/resource), every module parameter (modinfo drbd / module_param in the source), and every sysfs/proc control, and for each determine whether it can make the ARM (drbd_state.c:3096-3099) or EXECUTE (:4466/:4295) NOT fire for a quorum-lost Primary, WITHOUT a timing race. Specifically check: quorum-minimum-redundancy, on-no-quorum, on-suspended-primary-outdated, fencing, the after-sb-* family, rr-conflict, al-extents, --disable-*, any "outdate"/"uuid" related knob. Is there ANY option whose documented or coded effect disarms the autonomous rotation? Produce a definitive yes/no: does a race-free config prevention exist in stock 9.3.2?' },
  { key: 'force-guarded-route', q: 'The DISCONNECT route (drbd_receiver.c:9865/9884-9888) and the WRITE route (drbd_sender.c:3443) are BOTH quorum-guarded and would NOT rotate a quorum-lost Primary. Scenario B instead takes the UNGUARDED state-change route (:4466). WHY? Is there a config/timing/operational way to make the peer-loss be processed through the GUARDED disconnect route instead of the unguarded susp_uuid-edge route? E.g., does an explicit `drbdadm disconnect` (vs a silent link drop) route through the guarded path? Does the order in which peers are lost matter? Is there a way to ensure the quorum-lost Primary only ever reaches a guarded route? Trace which event sequence lands on :4466 vs :9886 and whether we can steer it.' },
  { key: 'is-it-a-bug', q: 'ADVERSARIAL: is the unguarded rotation a genuine BUG (incomplete guard / oversight) or INTENTIONAL? Argue BOTH sides from evidence. FOR-bug: the explicit guard + comment on the disconnect route ("do not create the new UUID immediately!"), the in-scope-but-unused have_quorum/PRIMARY_LOST_QUORUM at the unguarded sites, and the 9.2.18 diskless fix (same class, already patched). AGAINST-bug / intended: is there any scenario where a quorum-lost Primary SHOULD rotate (e.g. to mark itself diverged so it always loses on heal)? Check comments, the rotate_current_into_bitmap semantics, the "weak" marking, and any design doc/mailing-list rationale. Deliver a confidence-rated verdict: is this a bug LINBIT would accept, and what is the single strongest piece of evidence?' },
  { key: 'diskless-precedent', q: 'Nail the 9.2.18 precedent precisely. The ChangeLog says "Fix when a diskless primary creates a new current UUID, fixing possible silent data divergence later." Find the ACTUAL fix: which function/commit, what guard was added, and to which path. Compare it to the diskful frozen-Primary path (drbd_state.c:3096-3099 arm / :4466 execute). Is the diskful path the SAME root cause left unfixed, or genuinely different? This is the load-bearing argument of the bug report -- "you fixed the diskless variant in 9.2.18; here is the diskful variant with the same root cause, still unguarded." Get the exact diskless-fix code (drbd_main.c diskless_primary_needs_uuid_bump / the get_ldev_if_state(D_UP_TO_DATE) branch around :5273-5303) and articulate the parallel rigorously.' },
  { key: 'minimal-kernel-fix', q: 'Specify the EXACT minimal patch LINBIT would most likely accept. Candidate: add `&& device->have_quorum[NEW]` (or `&& !test_bit(PRIMARY_LOST_QUORUM, &device->flags)`) to the ARM at drbd_state.c:3098-3099 and/or the EXECUTE at :4466-4471 and :4295-4305, mirroring the existing guards at drbd_sender.c:3443 and drbd_receiver.c:9886. Determine: (1) which site is the RIGHT one to guard (arm vs execute vs both) so that legitimate bumps are preserved -- Primary that KEEPS quorum losing a Secondary (must still bump), Secondary->Primary promotion (:3131, must still bump), the write route (already guarded). (2) Does guarding break any legitimate case? (3) Write the actual diff hunk(s). (4) Confirm the guard variable is valid at that point (have_quorum[NEW] populated, PRIMARY_LOST_QUORUM set by then at :2885-2886). Provide a clean, minimal, review-ready patch.' },
  { key: 'linbit-report-format', q: 'How does LINBIT prefer to RECEIVE bug reports for the DRBD kernel module? Determine the correct CHANNEL (GitHub LINBIT/drbd issues vs drbd-user mailing list vs support portal) for a non-security correctness bug, and the EXACT fields/structure they expect (version strings: how to get them -- drbdadm --version, modinfo drbd, cat /proc/drbd; kernel/distro; resource config; reproducer; expected vs actual; dmesg; drbdsetup status; drbdadm show-gi / dump-md). Search github.com/LINBIT/drbd/issues for the bug-report template and for any EXISTING issue already covering "new current uuid" on a quorum-lost/frozen primary (so we do not duplicate). Summarize a few well-received DRBD kernel bug reports as a style guide. Output a concrete template we will fill.' },
]

phase('Research')
const research = (await parallel(AREAS.map(a => () => agent(
  MISSION + '\n\nAREA: ' + a.key + '\n\nQUESTIONS:\n' + a.q + '\n\n' + FACTS + '\n\n' + SOURCES,
  { label: 'p:' + a.key, phase: 'Research', schema: FINDINGS, agentType: 'general-purpose' }
)))).filter(Boolean)

phase('Synthesize')
const SYNTH = {
  type: 'object',
  additionalProperties: false,
  properties: {
    race_free_config_prevention_exists: { type: 'string', enum: ['yes', 'no', 'partial-with-tradeoff'], description: 'Decisive: is there a stock, race-free config/option that prevents the rotation?' },
    best_race_free_option: { type: 'string', description: 'If yes/partial, the exact option + its cost/tradeoff. If no, "NONE -- kernel fix required for race-free prevention" + why every candidate fails.' },
    io_error_verdict: { type: 'string', description: 'Does on-no-quorum=io-error prevent the rotation, and is it viable for a VM appliance?' },
    is_confirmed_bug: { type: 'string', enum: ['yes-high-confidence', 'yes-likely', 'ambiguous', 'no-intended'], description: 'Is this a genuine DRBD bug?' },
    strongest_bug_evidence: { type: 'string', description: 'The single strongest piece of evidence it is a bug.' },
    minimal_fix: { type: 'string', description: 'The exact minimal patch (which site(s), what guard, the diff hunk), and confirmation it preserves legitimate bumps.' },
    report_channel: { type: 'string', description: 'Where/how to file (GitHub issue vs mailing list) and whether a duplicate already exists.' },
    recommended_path: { type: 'string', description: 'The recommendation for Bedrock: race-free config if one exists, else ship the bundled-module patch + file upstream. Be decisive.' },
    load_bearing_claims: { type: 'array', items: { type: 'string' } },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
  required: ['race_free_config_prevention_exists', 'best_race_free_option', 'io_error_verdict', 'is_confirmed_bug', 'strongest_bug_evidence', 'minimal_fix', 'report_channel', 'recommended_path', 'load_bearing_claims', 'confidence'],
}
const synth = await agent(
  'Synthesize a decisive answer: is there a STOCK RACE-FREE way to prevent the quorum-lost-Primary UUID rotation, or is a kernel fix the only race-free option? Confirm whether it is a bug. Specify the minimal fix.\n\n' + MISSION + '\n\n' + FACTS + '\n\nDOSSIERS:\n' + JSON.stringify(research, null, 1),
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
  'Verify this DRBD claim against the kernel source (/tmp/drbdsrc/drbd) and official docs/manpages. Try HARD to REFUTE. Quote the exact line/section.\n\nCLAIM: ' + c + '\n\n' + SOURCES,
  { label: 'verify:' + (i + 1), phase: 'Verify', schema: VERDICT, agentType: 'general-purpose' }
)))).filter(Boolean)

phase('Finalize')
const FINAL = {
  type: 'object',
  additionalProperties: false,
  properties: {
    race_free_config_prevention_exists: { type: 'string', enum: ['yes', 'no', 'partial-with-tradeoff'] },
    recommended_path: { type: 'string', description: 'Decisive recommendation for Bedrock, incorporating verification.' },
    is_confirmed_bug: { type: 'string', enum: ['yes-high-confidence', 'yes-likely', 'ambiguous', 'no-intended'] },
    minimal_fix_diff: { type: 'string', description: 'The exact, review-ready patch hunk(s) with file:line context.' },
    bug_report_markdown: { type: 'string', description: 'The COMPLETE, ready-to-edit LINBIT bug-report draft in their preferred format/channel: title, environment/version fields (with the commands to capture them), summary, root-cause with exact source citations, the guarded-vs-unguarded asymmetry, the 9.2.18 diskless precedent, a minimal reproducer (the 4-node A/B test), expected vs actual with the kernel-log line, impact, and a suggested-fix section with the diff. Leave clearly-marked placeholders for the live values we still need to capture (drbdadm --version, modinfo, kernel, show-gi dumps).' },
    placeholders_to_capture: { type: 'array', items: { type: 'string' }, description: 'The exact commands to run on a sim to fill the report placeholders.' },
    confidence: { type: 'string', enum: ['high', 'medium', 'low'] },
  },
  required: ['race_free_config_prevention_exists', 'recommended_path', 'is_confirmed_bug', 'minimal_fix_diff', 'bug_report_markdown', 'placeholders_to_capture', 'confidence'],
}
const final = await agent(
  'Produce the FINAL decision + a complete LINBIT bug-report draft. Incorporate verification (drop/adjust REFUTED claims). Be decisive on whether a race-free config prevention exists; if not, say the kernel fix is the only race-free option and we ship it via the bundled module + file upstream. The bug report must be precise, source-cited, and in LINBIT\'s preferred format, with placeholders for live values.\n\nSYNTHESIS:\n' + JSON.stringify(synth, null, 1) + '\n\nVERIFICATION:\n' + JSON.stringify(verdicts, null, 1) + '\n\n' + FACTS,
  { label: 'finalize', phase: 'Finalize', schema: FINAL }
)

return { final, verdicts, synth_prevention: synth.race_free_config_prevention_exists, synth_isbug: synth.is_confirmed_bug }
