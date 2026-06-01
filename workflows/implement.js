export const meta = {
  name: 'sdd-implement-engine',
  description: 'Autonomously implement an approved SDD spec: code, test, review, fix, then land commits through the AI commit gate and push — until the task is fully delivered.',
  whenToUse: 'Invoked by the /implement-wf lead command after setup. Not run directly by a human.',
  phases: [
    { title: 'Code', detail: 'parallel coders implement the spec from the Work breakdown' },
    { title: 'Test', detail: 'one tester writes tests; bug-fix loop with owning coders' },
    { title: 'Review', detail: 'role reviewers (code/test/spec/security/+ui) in parallel, schema-enforced' },
    { title: 'Fix', detail: 'route MUST FIX/CRITICAL to coders/tester, re-review until clean' },
    { title: 'Land', detail: 'finalize spec, land logical commits through the gate, push' },
  ],
}

// ---------------------------------------------------------------------------
// Args (from the /implement-wf lead):
//   specPath        absolute path to the spec inside the worktree (tasks/4-in-progress/...)
//   worktreePath    directory all agents work in
//   baseBranch      branch to diff against for reviews (dev or current)
//   branchName      task/{ID}-{slug} when auto_branch; null otherwise
//   taskId          e.g. HCC-010 — used to prefix commit messages
//   coders          [{ name, scope, files:[...] }]  (the Architect's Work breakdown)
//   reviewPromptPath  '.claude/review_prompt.md' if present, else null
//   autoBranch      bool — whether a dedicated branch/worktree was created (controls push)
//   seededConcerns  [string] — issues the lead could not reconcile pre-launch
// ---------------------------------------------------------------------------

const a = args || {}
const CODERS = a.coders || []
const WORKTREE = a.worktreePath
const SPEC = a.specPath
const BASE = a.baseBranch
const TASK = a.taskId || 'TASK'
const REVIEW_PROMPT = a.reviewPromptPath || null
const knownConcerns = [].concat(a.seededConcerns || [])

// How many consecutive rounds the SAME finding may persist before it is
// retired to Known Concerns. This is NOT a global round cap — productive rounds
// (different findings each round) are unbounded; only an individual finding that
// won't die is retired, so the loop provably terminates. (User-endorsed escape.)
// Keyed by content fingerprint, not by agent-assigned id, because re-review
// agents do not reliably preserve ids across rounds.
const STALL_ROUNDS = 3

const FRONTEND_RE = /\.(x?html?|css|s[ac]ss|less|jsx?|tsx?|vue|svelte|qweb|mako|jinja2)$/i
const TEST_PATH_RE = /(^|\/)tests?\/|_test\.|\.test\.|\.spec\./i

// Stable content fingerprint for a finding — survives id renumbering across
// rounds, so set-shrink / persistence detection is reliable.
function fpKey(f) {
  const desc = (f.description || '').toLowerCase().replace(/\s+/g, ' ').trim().slice(0, 100)
  return `${f.file || ''}|${desc}`
}

// ---------------------------------------------------------------------------
// Schemas — agent({schema}) forces the shape and auto-retries malformed output.
// This is what replaces the manual "reject reports without a DEPTH block" prose.
// ---------------------------------------------------------------------------

const CHANGED_FILE = {
  type: 'object',
  additionalProperties: false,
  required: ['path', 'summary'],
  properties: {
    path: { type: 'string' },
    summary: { type: 'string', description: 'what changed in this file' },
  },
}

const CODER_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['changedFiles', 'implementationSummary'],
  properties: {
    changedFiles: { type: 'array', items: CHANGED_FILE },
    implementationSummary: { type: 'string' },
    knownConcerns: { type: 'array', items: { type: 'string' }, description: 'issues spotted outside scope' },
  },
}

const CODER_FIX_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['fixed', 'apiOrBehaviorChanged', 'unresolved'],
  properties: {
    fixed: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['file', 'what'], properties: { file: { type: 'string' }, what: { type: 'string' } } } },
    apiOrBehaviorChanged: { type: 'boolean', description: 'true if a fix changed an API or behavior the Tester must re-check' },
    apiChangeNote: { type: 'string' },
    // For findings the coder judges cannot be cleanly resolved (spurious or
    // genuinely ambiguous): record them here with a rationale; the loop will
    // move them to Known Concerns instead of re-routing forever.
    unresolved: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['findingId', 'rationale'], properties: { findingId: { type: 'string' }, rationale: { type: 'string' } } } },
  },
}

const TESTER_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['testFiles', 'testCount', 'results', 'productionBug'],
  properties: {
    testFiles: { type: 'array', items: { type: 'string' } },
    testCount: { type: 'integer' },
    results: { type: 'string', enum: ['all-passing', 'failing'] },
    coverage: { type: 'string', description: 'which ACs are covered / gaps' },
    // null when tests pass; otherwise the single most important production bug found.
    productionBug: {
      type: ['object', 'null'],
      additionalProperties: false,
      required: ['file', 'description'],
      properties: {
        file: { type: 'string' },
        method: { type: 'string' },
        expected: { type: 'string' },
        actual: { type: 'string' },
        test: { type: 'string' },
        description: { type: 'string' },
      },
    },
  },
}

const FINDING = {
  type: 'object',
  additionalProperties: false,
  required: ['id', 'severity', 'description'],
  properties: {
    id: { type: 'string', description: 'short stable id, e.g. f-1' },
    severity: { type: 'string', enum: ['CRITICAL', 'MUST_FIX', 'MAJOR', 'CONCERN', 'MINOR', 'NIT'] },
    file: { type: 'string' },
    line: { type: ['integer', 'null'] },
    description: { type: 'string' },
    suggestedFix: { type: 'string' },
  },
}

const REVIEWER_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['reviewer', 'verdict', 'depth', 'findings', 'summary'],
  properties: {
    reviewer: { type: 'string' },
    verdict: { type: 'string', enum: ['CLEAN', 'SECURE', 'COMPLIANT', 'HAS_FINDINGS'] },
    // depth is REQUIRED and must be non-empty — this structurally enforces the
    // DEPTH block. A shallow review cannot validate.
    depth: {
      type: 'array',
      minItems: 1,
      items: { type: 'object', additionalProperties: false, required: ['label', 'count'], properties: { label: { type: 'string' }, count: { type: 'integer' } } },
    },
    findings: { type: 'array', items: FINDING },
    summary: { type: 'string' },
  },
}

const RE_REVIEW_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['verdict', 'resolved', 'outstanding', 'newIssues'],
  properties: {
    verdict: { type: 'string', enum: ['PASS', 'FINDINGS'] },
    resolved: { type: 'array', items: { type: 'string' }, description: 'ids of previous findings now fixed' },
    outstanding: { type: 'array', items: FINDING, description: 'previous findings still unfixed' },
    newIssues: { type: 'array', items: FINDING, description: 'regressions introduced by the fixes' },
  },
}

const COMMIT_PLAN_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['groups'],
  properties: {
    groups: {
      type: 'array',
      minItems: 1,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['message', 'files'],
        properties: {
          message: { type: 'string', description: 'conventional commit subject, prefixed with the task id' },
          files: { type: 'array', items: { type: 'string' }, minItems: 1 },
          note: { type: 'string' },
        },
      },
    },
  },
}

const COMMIT_RESULT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['landed', 'verdict', 'blockingFindings', 'warnings', 'overridesAdded', 'knownConcerns'],
  properties: {
    landed: { type: 'boolean' },
    verdict: { type: 'string', enum: ['CLEAN', 'BLOCKED', 'ERROR'] },
    shaSummary: { type: 'string', description: 'short hash + subject if landed' },
    // Gate findings the committer could not resolve on its own and is handing
    // back to the loop to route to the owning coder/tester.
    blockingFindings: { type: 'array', items: FINDING },
    warnings: { type: 'array', items: { type: 'string' } },
    // Scoped overrides the committer added to clear a recurring false-positive.
    overridesAdded: { type: 'array', items: { type: 'object', additionalProperties: false, required: ['where', 'reason'], properties: { where: { type: 'string' }, reason: { type: 'string' } } } },
    knownConcerns: { type: 'array', items: { type: 'string' } },
  },
}

const SCRIBE_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['done', 'implementationSummary', 'manualReviewSteps'],
  properties: {
    done: { type: 'boolean' },
    implementationSummary: { type: 'string' },
    manualReviewSteps: { type: 'array', items: { type: 'string' }, minItems: 1 },
  },
}

// ---------------------------------------------------------------------------
// Shared prompt preamble — every agent runs inside the workflow, so there is
// NO lead to SendMessage. Override the agent files' communication convention.
// ---------------------------------------------------------------------------

function preamble(role) {
  return [
    `You are the ${role} running inside an AUTOMATED workflow.`,
    `There is NO lead agent to message — do NOT use SendMessage or wait for replies.`,
    `Your ONLY output channel is the StructuredOutput tool: return your result there.`,
    `Working directory: ${WORKTREE} — all work happens here, nowhere else.`,
    `Spec file: ${SPEC} (read it first; it is your source of truth).`,
  ].join('\n')
}

function reviewRulesLine() {
  return REVIEW_PROMPT
    ? `Project review rules: read ${REVIEW_PROMPT} and apply its severity overrides / intentional-design notes.`
    : `Project review rules: none.`
}

// Map a changed file back to the coder that owns it (Work breakdown).
// Fileless findings (e.g. Spec-Auditor "In Scope item not implemented") fall
// back to the first coder — never to the Tester, which writes no prod code.
function ownerOf(file) {
  if (!file) return CODERS[0]
  for (const c of CODERS) {
    if ((c.files || []).some(f => file === f || file.endsWith('/' + f) || f.endsWith('/' + file))) return c
  }
  return CODERS[0] // fall back to first coder if ownership is unclear
}

const BLOCKING = new Set(['CRITICAL', 'MUST_FIX'])
function blockingFindings(report) {
  return (report.findings || []).filter(f => BLOCKING.has(f.severity))
}

// ===========================================================================
// PHASE 1a — Code
// ===========================================================================
phase('Code')
log(`Spawning ${CODERS.length} coder(s) from the Work breakdown.`)

const coderResults = (await parallel(
  CODERS.map((c, i) => () =>
    agent(
      [
        preamble('Coder'),
        `Read your role instructions: ~/.claude/agents/coder.md`,
        `Your scope (Work breakdown → ${c.name}): ${c.scope}`,
        `Files you own: ${(c.files || []).join(', ')}`,
        `Do NOT touch any other files — they belong to other coders.`,
        `Implement your scope fully. Return changedFiles + implementationSummary.`,
      ].join('\n'),
      { agentType: 'Coder', label: `code:${c.name}`, phase: 'Code', schema: CODER_SCHEMA },
    ),
  ),
)).filter(Boolean)

let changedFiles = []
for (const r of coderResults) {
  changedFiles = changedFiles.concat((r.changedFiles || []).map(f => f.path))
  knownConcerns.push(...(r.knownConcerns || []))
}
changedFiles = Array.from(new Set(changedFiles))
log(`Coders done. ${changedFiles.length} changed file(s).`)

// ===========================================================================
// PHASE 1b — Test  (tester + unbounded bug-fix loop)
// ===========================================================================
phase('Test')

function testerPrompt(extra) {
  return [
    preamble('Tester'),
    `Read your role instructions: ~/.claude/agents/tester.md`,
    `Coding is complete. Changed files: ${changedFiles.join(', ')}`,
    `Write tests for the implementation and run them.`,
    `If you find a PRODUCTION bug, set productionBug (do not work around it). Otherwise productionBug = null.`,
    extra || '',
  ].join('\n')
}

let test = await agent(testerPrompt(''), { agentType: 'Tester', label: 'test:write', phase: 'Test', schema: TESTER_SCHEMA })

let bugRound = 0
while (test.productionBug) {
  bugRound++
  const bug = test.productionBug
  const owner = ownerOf(bug.file)
  log(`Production bug #${bugRound} in ${bug.file} → ${owner.name}.`)
  await agent(
    [
      preamble('Coder'),
      `Read your role instructions: ~/.claude/agents/coder.md`,
      `The Tester found a production bug in a file you own. Fix it.`,
      `File: ${bug.file}  Method: ${bug.method || '(n/a)'}`,
      `Expected: ${bug.expected || '(see description)'}`,
      `Actual: ${bug.actual || '(see description)'}`,
      `Details: ${bug.description}`,
      `Return the fix. Set apiOrBehaviorChanged if the Tester must adjust tests.`,
    ].join('\n'),
    { agentType: 'Coder', label: `bugfix:${owner.name}:${bugRound}`, phase: 'Test', schema: CODER_FIX_SCHEMA },
  )
  test = await agent(testerPrompt('Re-run the affected tests after the latest fix.'), { agentType: 'Tester', label: `test:rerun:${bugRound}`, phase: 'Test', schema: TESTER_SCHEMA })
}
log(`Tests green: ${test.testCount} test(s).`)

// ===========================================================================
// PHASE 2 — Review  (parallel role reviewers, schema-enforced depth)
// ===========================================================================
phase('Review')

const uiNeeded = changedFiles.some(f => FRONTEND_RE.test(f))
const REVIEWERS = [
  { key: 'code', type: 'Code-Reviewer', scope: 'production code quality' },
  { key: 'test', type: 'Test-Reviewer', scope: 'test quality and coverage' },
  { key: 'spec', type: 'Spec-Auditor', scope: 'spec compliance' },
  { key: 'security', type: 'Security-Reviewer', scope: 'security and architecture' },
]
if (uiNeeded) REVIEWERS.push({ key: 'ui', type: 'UI-Reviewer', scope: 'visual verification' })
log(`Spawning ${REVIEWERS.length} reviewer(s)${uiNeeded ? ' (UI included)' : ''}.`)

function reviewerPrompt(r) {
  const lines = [
    preamble(r.type),
    `Read your role instructions: ~/.claude/agents/${r.type.toLowerCase()}.md`,
    `Base branch for the diff: ${BASE}`,
    reviewRulesLine(),
    `Audit ${r.scope}. Return verdict, a non-empty depth list (what you audited + counts), findings, and a summary.`,
    `Give each finding a stable id (f-1, f-2, ...).`,
  ]
  if (r.key === 'ui') {
    lines.push(`Changed files: ${changedFiles.join(', ')}`)
    lines.push(`Identify the affected pages/URLs from the spec and changed files.`)
  }
  return lines.join('\n')
}

// A reviewer that errors is NOT silently dropped — that would let a whole
// dimension (e.g. Security) pass unreviewed. Run, retry once, and if it still
// fails, record the un-reviewed dimension in Known Concerns for manual review.
async function runReviewer(r, attempt) {
  try {
    const rep = await agent(reviewerPrompt(r), { agentType: r.type, label: `review:${r.key}${attempt > 1 ? ':retry' : ''}`, phase: 'Review', schema: REVIEWER_SCHEMA })
    return { r, rep }
  } catch (e) {
    return { r, rep: null, failed: true }
  }
}

let reviewRuns = await parallel(REVIEWERS.map(r => () => runReviewer(r, 1)))
for (let i = 0; i < reviewRuns.length; i++) {
  if (reviewRuns[i] && reviewRuns[i].failed) reviewRuns[i] = await runReviewer(reviewRuns[i].r, 2)
}
let reports = []
for (const item of reviewRuns) {
  if (item && item.rep) reports.push({ r: item.r, rep: item.rep })
  else if (item && item.r) knownConcerns.push(`Reviewer ${item.r.type} did not complete — the "${item.r.scope}" dimension was NOT reviewed. Manual review required.`)
}

// ===========================================================================
// PHASE 3 — Fix & Verify  (unbounded productive loop; per-finding escape)
// ===========================================================================
phase('Fix')

// Route a batch of blocking findings to the owning coders + tester, then
// re-review. Returns the findings re-review still flags as blocking.
async function routeFixes(findings) {
  // Only true test-path files go to the Tester. Fileless findings (missing
  // implementation, scope creep) and production files go to a coder — the
  // Tester writes no production code and cannot act on them.
  const byCoder = new Map()
  const testFindings = []
  for (const f of findings) {
    if (f.file && TEST_PATH_RE.test(f.file)) testFindings.push(f)
    else {
      const owner = ownerOf(f.file)
      if (!byCoder.has(owner.name)) byCoder.set(owner.name, [])
      byCoder.get(owner.name).push(f)
    }
  }

  // coder fixes (parallel across coders; each coder serial on its own files)
  const fixResults = await parallel(
    Array.from(byCoder.entries()).map(([name, fs]) => () =>
      agent(
        [
          preamble('Coder'),
          `Read your role instructions: ~/.claude/agents/coder.md`,
          `Review findings that need fixing. For each: id, severity, file:line, description, suggested fix.`,
          ...fs.map(f => `- [${f.severity}] (${f.id}) ${f.file || '(no file)'}:${f.line || ''} — ${f.description}${f.suggestedFix ? ' | fix: ' + f.suggestedFix : ''}`),
          `If a finding is genuinely spurious or you cannot cleanly resolve it, list it under "unresolved" with a rationale instead of forcing a change.`,
          `Return fixed[], apiOrBehaviorChanged, and unresolved[].`,
        ].join('\n'),
        { agentType: 'Coder', label: `fix:${name}`, phase: 'Fix', schema: CODER_FIX_SCHEMA },
      ).then(res => ({ name, res })).catch(() => null),
    ),
  )

  // anything a coder voluntarily marked unresolved → Known Concerns
  const droppedIds = new Set()
  let apiChanged = false
  for (const fr of fixResults.filter(Boolean)) {
    if (fr.res.apiOrBehaviorChanged) apiChanged = true
    for (const u of fr.res.unresolved || []) {
      droppedIds.add(u.findingId)
      knownConcerns.push(`Unresolved review finding ${u.findingId}: ${u.rationale}`)
    }
  }

  // tester fixes (test findings + API changes that need test updates)
  if (testFindings.length || apiChanged) {
    test = await agent(
      [
        testerPrompt(apiChanged ? 'A production API/behavior changed during fixes — update the affected tests, then re-run.' : 'Address the test findings below, then re-run all tests.'),
        ...testFindings.map(f => `- [${f.severity}] (${f.id}) ${f.file || ''} — ${f.description}`),
      ].join('\n'),
      { agentType: 'Tester', label: `fix:tester`, phase: 'Fix', schema: TESTER_SCHEMA },
    )
  }

  // re-review only the reviewers that had blocking findings
  const recheck = reports.filter(({ rep }) => blockingFindings(rep).length > 0)
  const rechecked = await parallel(
    recheck.map(({ r, rep }) => () =>
      agent(
        [
          preamble(r.type),
          `Read your role instructions: ~/.claude/agents/${r.type.toLowerCase()}.md`,
          `Base branch for the diff: ${BASE}`,
          reviewRulesLine(),
          `This is a RE-REVIEW after fixes.`,
          `Primary: verify each of your previous blocking findings is resolved:`,
          ...blockingFindings(rep).map(f => `- (${f.id}) [${f.severity}] ${f.file || ''}:${f.line || ''} — ${f.description}`),
          `Secondary (mandatory): re-run your full audit on the modified files — treat new methods, new error paths, and regressions in previously-clean code as in scope.`,
          `Return PASS only if BOTH the primary items are resolved AND the secondary pass finds nothing new.`,
        ].join('\n'),
        { agentType: r.type, label: `reverify:${r.key}`, phase: 'Fix', schema: RE_REVIEW_SCHEMA },
      ).then(rr => ({ r, rr })).catch(() => null),
    ),
  )

  // fold re-review results back into `reports` and collect still-open findings
  let stillOpen = []
  for (const item of rechecked.filter(Boolean)) {
    const open = []
      .concat(item.rr.outstanding || [])
      .concat(item.rr.newIssues || [])
      .filter(f => !droppedIds.has(f.id))
    // refresh this reviewer's findings to the current open set
    const entry = reports.find(e => e.r.key === item.r.key)
    if (entry) entry.rep = { ...entry.rep, findings: open, verdict: open.length ? 'HAS_FINDINGS' : 'CLEAN' }
    stillOpen = stillOpen.concat(open.filter(f => BLOCKING.has(f.severity)))
  }
  return stillOpen
}

// Termination is fingerprint-based, not id-based: a finding that persists for
// STALL_ROUNDS consecutive rounds is retired to Known Concerns and excluded
// from further routing. Productive rounds (different findings each time) never
// trigger it, so the loop stays unbounded yet provably terminates.
const persist = new Map() // fpKey -> consecutive rounds still open
const givenUp = new Set() // fpKeys retired to Known Concerns
let fixRound = 0
let outstanding = reports.flatMap(({ rep }) => blockingFindings(rep)).filter(f => !givenUp.has(fpKey(f)))
while (outstanding.length) {
  fixRound++
  const curKeys = new Set(outstanding.map(fpKey))
  for (const k of curKeys) persist.set(k, (persist.get(k) || 0) + 1)
  for (const k of Array.from(persist.keys())) if (!curKeys.has(k)) persist.delete(k)
  const retire = outstanding.filter(f => (persist.get(fpKey(f)) || 0) >= STALL_ROUNDS)
  for (const f of retire) {
    givenUp.add(fpKey(f))
    knownConcerns.push(`Unresolved review finding after ${STALL_ROUNDS} rounds (${f.severity}) ${f.file || '(no file)'}: ${f.description}`)
  }
  const toFix = outstanding.filter(f => !givenUp.has(fpKey(f)))
  if (!toFix.length) { log(`Retired ${retire.length} stuck finding(s) to Known Concerns; review loop done.`); break }
  log(`Fix round ${fixRound}: ${toFix.length} blocking finding(s)${retire.length ? `, ${retire.length} retired` : ''}.`)
  const reopened = await routeFixes(toFix)
  outstanding = reopened.filter(f => !givenUp.has(fpKey(f)))
}
log(`Review resolved after ${fixRound} fix round(s).`)

// ===========================================================================
// PHASE 4 — Land  (finalize spec → logical commits through the gate → push)
// ===========================================================================
phase('Land')

// 4.1 — Scribe writes the SDD finalization sections into the spec, sets
// frontmatter (status: review), and moves the spec to tasks/5-review/.
const reviewerSummary = reports.map(({ r, rep }) => `${r.type}: ${rep.verdict} — ${rep.summary}`).join('\n')
const scribe = await agent(
  [
    preamble('Scribe'),
    `You finalize the SDD spec. Use the template ~/.claude/templates/sdd/implementation-sections.md.`,
    `Append these sections to the spec file, filled in:`,
    `- Implementation Summary (what was done + key decisions, from the coders' work).`,
    `- Known Concerns: ${knownConcerns.length ? knownConcerns.map(c => '\n  • ' + c).join('') : 'none'}`,
    `- Auto-Review Results: tests = ${test.testCount} passing; reviewer verdicts:\n${reviewerSummary}`,
    `- Steps for Manual Review: 3-7 concrete "action → expected result" steps.`,
    `Then update frontmatter: status: review, completed/updated to today, branch: ${a.branchName || BASE}.`,
    `Then MOVE the spec file from tasks/4-in-progress/ to tasks/5-review/ (git mv).`,
    `Do this with Bash inside ${WORKTREE}. Return done=true, the implementationSummary text, and the manualReviewSteps you wrote.`,
  ].join('\n'),
  { agentType: 'general-purpose', label: 'land:scribe', phase: 'Land', schema: SCRIBE_SCHEMA },
)

// 4.2 — Plan logical commits (vertical slices, <=300 prod lines each).
const plan = await agent(
  [
    preamble('Commit planner'),
    `Plan logical commits for the full change set. Group by cohesive unit: each feature chunk together with its tests; config/infra separately; the finalized spec as its own docs commit.`,
    `Each group: <=300 added production lines (tests/docs/config do not count). Vertical slices only — never "all code" then "all tests".`,
    `Conventional commit subjects, each prefixed with "${TASK}": e.g. "feat(${TASK}): ...".`,
    `Inspect the working tree with Bash (git status --porcelain, git diff --stat) inside ${WORKTREE} to enumerate every changed/new file, including the moved spec.`,
    `Return groups[] of { message, files[] } covering EVERY changed file exactly once.`,
  ].join('\n'),
  { agentType: 'general-purpose', label: 'land:plan', phase: 'Land', schema: COMMIT_PLAN_SCHEMA },
)

// 4.3 — Land each group sequentially through the commit gate, with an
// unbounded fix loop. The committer agent runs ALONE (never alongside others).
const commitLedger = []
for (let gi = 0; gi < plan.groups.length; gi++) {
  const g = plan.groups[gi]
  let landed = false
  let gateRound = 0
  const gatePersist = new Map() // fpKey -> consecutive gate rounds the finding stayed open
  while (!landed) {
    gateRound++
    const cr = await agent(
      [
        preamble('Committer'),
        `Follow the commit skill procedure (~/.claude/skills/commit/SKILL.md): security scan, Phase 3.5 coverage+assert preflight (a fresh coverage.xml is required), stash-guard, then commit with the AI review hook in the BACKGROUND (the gate can take up to 20 minutes — poll, do not give up early).`,
        `Commit ONLY this logical group:`,
        `  message: ${g.message}`,
        `  files: ${g.files.join(', ')}`,
        `Work inside ${WORKTREE}. Stage only these files (never -A / .).`,
        ``,
        `READING THE GATE RESULT — the git commit exit code is authoritative:`,
        `- exit 0  → commit landed. Return landed=true with shaSummary ("git log -1 --oneline").`,
        `- exit 2  → coverage/assert preflight failed (usually a missing/stale coverage.xml or an untested new line / weak assertion). Regenerate coverage, add the missing test/assertion, re-commit. This is yours to fix.`,
        `- exit 3  → preflight gate crashed. Return landed=false, verdict ERROR, with the crash text in knownConcerns.`,
        `- exit 1  → BLOCK. Read the hook output to tell which kind:`,
        `    • "manifest auto-scaffolded" → fill .review/manifest.yaml (group files <=300 prod lines/chunk, <=12 chunks) and re-commit.`,
        `    • "Review BLOCKED" / "Chunked review BLOCKED" → parse the "### Upheld findings (blocking)" section. Each blocking line looks like "[F1] [CRITICAL] path:line — description". Those CRITICALs are what blocks the commit. The "### Warnings:" section lists advisory "[WARNING] ..." lines.`,
        ``,
        `AUTONOMY RULES (no human is available) for an exit-1 review BLOCK:`,
        `- gitleaks/semgrep secret hit: it is your own generated code — remove the secret, use an env var or an obviously-fake placeholder, and re-commit.`,
        `- WARNINGs: fix the ones you can within this group's files, then re-commit (they are advisory but the directive says fix-in-one-pass).`,
        `- An UPHELD CRITICAL you have already tried to fix once and judge to be a false positive: add a SCOPED override (a note in .claude/review_prompt.md, or a rule-id in .semgrep-exclude-rules) AND record the override + your reasoning in knownConcerns. Only then will the commit land.`,
        `- An UPHELD CRITICAL in another coder's production logic that you cannot fix from this group's files: return landed=false with it in blockingFindings — set { id, severity:"CRITICAL", file (the path:line from the finding), description } so the loop routes it to the owner.`,
        ``,
        `Return COMMIT_RESULT: landed, verdict (CLEAN/BLOCKED/ERROR), shaSummary, blockingFindings (each with id+file+description), warnings, overridesAdded, knownConcerns.`,
      ].join('\n'),
      { agentType: 'general-purpose', label: `land:commit:${gi + 1}:${gateRound}`, phase: 'Land', schema: COMMIT_RESULT_SCHEMA },
    )
    knownConcerns.push(...(cr.knownConcerns || []))
    if (cr.landed) {
      commitLedger.push({ message: g.message, shaSummary: cr.shaSummary, warnings: cr.warnings, overridesAdded: cr.overridesAdded })
      landed = true
    } else if (cr.blockingFindings && cr.blockingFindings.length) {
      // Backstop: if the SAME gate findings persist for STALL_ROUNDS rounds the
      // committer's own fix+override path is not converging — stop retrying this
      // group, record it, and move on rather than loop toward the 1000-agent cap.
      const keys = new Set(cr.blockingFindings.map(fpKey))
      for (const k of keys) gatePersist.set(k, (gatePersist.get(k) || 0) + 1)
      for (const k of Array.from(gatePersist.keys())) if (!keys.has(k)) gatePersist.delete(k)
      if (cr.blockingFindings.every(f => (gatePersist.get(fpKey(f)) || 0) >= STALL_ROUNDS)) {
        knownConcerns.push(`Commit group "${g.message}" did not pass the gate after ${STALL_ROUNDS} rounds; left uncommitted. Findings: ${cr.blockingFindings.map(f => f.description).join('; ')}`)
        break
      }
      log(`Commit group ${gi + 1} blocked by gate: ${cr.blockingFindings.length} finding(s) → routing.`)
      await routeFixes(cr.blockingFindings)
    } else {
      // verdict ERROR with no actionable findings — record and stop retrying this group
      knownConcerns.push(`Commit group "${g.message}" could not be landed automatically (committer verdict ${cr.verdict}).`)
      break
    }
  }
}

// 4.4 — Push the branch (only when a dedicated branch/worktree was created).
let pushed = false
if (a.autoBranch && a.branchName) {
  const pr = await agent(
    [
      preamble('Pusher'),
      `Push the current branch to origin with standard push (no force): run "git push -u origin ${a.branchName}" with Bash inside ${WORKTREE}.`,
      `Return done=true and a one-line result.`,
    ].join('\n'),
    { agentType: 'general-purpose', label: 'land:push', phase: 'Land', schema: { type: 'object', additionalProperties: false, required: ['done', 'result'], properties: { done: { type: 'boolean' }, result: { type: 'string' } } } },
  )
  pushed = !!pr.done
}

return {
  status: knownConcerns.length ? 'DELIVERED_WITH_CONCERNS' : 'DELIVERED',
  taskId: TASK,
  branch: a.branchName || BASE,
  changedFiles,
  test: { count: test.testCount, results: test.results },
  reviewers: reports.map(({ r, rep }) => ({ reviewer: r.type, verdict: rep.verdict, summary: rep.summary })),
  fixRounds: fixRound,
  bugRounds: bugRound,
  commits: commitLedger,
  pushed,
  knownConcerns,
  implementationSummary: scribe.implementationSummary,
  manualReviewSteps: scribe.manualReviewSteps,
}
