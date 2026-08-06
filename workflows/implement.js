export const meta = {
  name: 'sdd-implement-engine',
  description: 'Autonomously implement an approved SDD spec: code, test, review, fix, then land commits through the AI commit gate and push — until the task is fully delivered.',
  whenToUse: 'Invoked by the /implement-wf lead command after setup. Not run directly by a human.',
  phases: [
    { title: 'Code', detail: 'parallel coders implement the spec from the Work breakdown' },
    { title: 'Test', detail: 'one tester writes tests; bug-fix loop with owning coders' },
    { title: 'Review', detail: 'role reviewers (code/test/spec/security/+ui) in parallel, schema-enforced' },
    { title: 'Fix', detail: 'route MUST FIX/CRITICAL to coders/tester, re-review until clean' },
    { title: 'Pre-review', detail: 'review all planned commit groups in parallel; fix until clean (≥2 groups)' },
    { title: 'Land', detail: 'finalize spec, land logical commits through the gate, push' },
    { title: 'Verify', detail: 'audit every landed commit was pre-reviewed (integrity cross-check)' },
    { title: 'Accept', detail: 'high-level check the spec is fully delivered; remediate gaps in-run' },
  ],
}

// ---------------------------------------------------------------------------
// Args (from the /implement-wf lead):
//   specPath        absolute path to the spec inside the worktree ({dir}/4-in-progress/...,
//                   where {dir} is the board of the applicable .tasks.toml — the repo root's
//                   or a client's, e.g. clients/<name>/tasks/)
//   worktreePath    directory all agents work in
//   baseBranch      branch to diff against for reviews (dev or current)
//   branchName      task/{ID}-{slug} when auto_branch; null otherwise
//   taskId          e.g. HCC-010 — used to prefix commit messages
//   coders          [{ name, scope, files:[...] }]  (the Architect's Work breakdown)
//   reviewPromptPath  '.claude/review_prompt.md' if present, else null
//   autoBranch      bool — whether a dedicated branch/worktree was created (controls push)
//   seededConcerns  [string] — issues the lead could not reconcile pre-launch
// ---------------------------------------------------------------------------

// The Workflow harness delivers the `args` global as a JSON STRING, not an
// object — so parse it. (Robust to both: object elsewhere, string here.)
const a = (typeof args === 'string' ? (args ? JSON.parse(args) : {}) : (args || {}))
const CODERS = a.coders || []
const WORKTREE = a.worktreePath
const SPEC = a.specPath
// Task-board root, derived from the spec path: a repo may carry several SDD
// roots (e.g. clients/<name>/tasks/), so the stage directories are never
// assumed to be a top-level `tasks/`. The lead always hands over a spec that
// already sits in `{dir}/4-in-progress/` (commands/implement-wf.md, Phase 1),
// so a path without that marker is a caller bug — it returns null and the
// fail-fast guard below turns it into a loud error rather than a garbage
// directory embedded in agent instructions.
const deriveTasksDir = (specPath) => {
  const at = typeof specPath === 'string' ? specPath.lastIndexOf('/4-in-progress/') : -1
  return at === -1 ? null : specPath.slice(0, at)
}
const TASKS_DIR = deriveTasksDir(SPEC)
const BASE = a.baseBranch
const TASK = a.taskId || 'TASK'
const REVIEW_PROMPT = a.reviewPromptPath || null
const knownConcerns = [].concat(a.seededConcerns || [])

// Fail fast on misconfigured args rather than silently running coderless and
// crashing minutes later on `CODERS[0].name` during fix-routing.
if (!WORKTREE || !SPEC || !CODERS.length || !TASKS_DIR) {
  throw new Error(
    `sdd-implement-engine: bad args — WORKTREE=${WORKTREE}, SPEC=${SPEC}, ` +
    `TASKS_DIR=${TASKS_DIR}, CODERS=${CODERS.length}, typeof args=${typeof args}. ` +
    `Expected a JSON object/string with worktreePath, a non-empty coders[], and a ` +
    `specPath under {dir}/4-in-progress/ (the board of the applicable .tasks.toml).`,
  )
}

// How many consecutive rounds the SAME finding may persist before it is
// retired to Known Concerns. This is NOT a global round cap — productive rounds
// (different findings each round) are unbounded; only an individual finding that
// won't die is retired, so the loop provably terminates. (User-endorsed escape.)
// Keyed by content fingerprint, not by agent-assigned id, because re-review
// agents do not reliably preserve ids across rounds.
const STALL_ROUNDS = 3

// SDD-003 — JS-held long-wait bounds. The workflow loop (not any single agent)
// holds each 15-60 min gate wait by re-spawning a fresh one-check poll agent per
// cycle. Two bounds keep it finite AND under the 1000-agent lifetime cap:
//   POLL_CEILING   — per-wait cycle cap. 24 cycles x ~270s ≈ 1.8h covers a
//                    60-min-and-then-some single wait.
//   MAX_POLL_AGENTS — module-level GLOBAL poll budget across ALL waits in the
//                    run. 700 deliberately reserves ~300 non-poll agents under
//                    the 1000 cap (code/test/review/fix + pre-review rounds +
//                    land/verify/accept/push + retries empirically ~210-300).
// On exhaustion of EITHER bound the wait ends with a recorded Known Concern and
// the run degrades to its existing stall path — it NEVER throw-aborts the run.
// (The 700/300 split is re-confirmed against the live agent counter on the D7d
// field run — the only place the real non-poll count is observable.)
const POLL_CEILING = 24
const MAX_POLL_AGENTS = 700
let pollAgentsSpawned = 0 // module-level global poll counter (incremented per poll spawn)

// Sentinel awaitDetachedJob returns to its caller on bound exhaustion. It is a
// recorded-error signal, NOT a throw — the caller degrades that one wait and
// continues (a pre-review wait leaves its pending group(s) to the live gate; a
// committer wait records the group as not-landed and moves on).
const POLL_EXHAUSTED = Symbol('POLL_EXHAUSTED')

// SDD-003 (MF-C) — backstop for the committer's `needRelaunch` re-commit path.
// EVERY in-gate fix (manifest-fill / secret / WARNING / coverage) returns
// needRelaunch and re-runs the full JS-held triple; a gate that keeps rejecting
// the fix would otherwise spin toward the 1000-agent cap. Cap the CONSECUTIVE
// needRelaunch rounds (a productive blockingFindings round resets the counter, so
// the unbounded-but-productive path is preserved). Set comfortably ABOVE the
// documented worst case — Key Constraints #1 / AC-5 Scenario B / the TMS-102
// provenance all cite "10 or more" gate rounds for ONE commit, so a cap of ~20
// admits the real 10-cycle commit while still stopping a true runaway. DEVIATION
// from the reviewer's example "STALL_ROUNDS*2"=6, which would kill legitimate
// 10-cycle commits (AC-5).
const MAX_NEEDRELAUNCH_ROUNDS = 20

const FRONTEND_RE = /\.(x?html?|css|s[ac]ss|less|jsx?|tsx?|vue|svelte|qweb|mako|jinja2)$/i
const TEST_PATH_RE = /(^|\/)tests?\/|(^|\/)test_|_test\.|\.test\.|\.spec\./i

// True when `p` is a test file or test-package registration. The dedicated
// Tester owns every test file; a Coder writes production code only. Single
// source of truth for "is this a test path", used to strip test paths from a
// coder's owned-files list (Phase 1a) and to route test review findings to the
// Tester (Phase 3). The `(^|\/)test_` branch covers the `test_*.py` convention
// (and `tests/__init__.py` via the `tests?/` branch); the boundary anchor keeps
// production names that merely contain "test" (e.g. contest_helpers.py) out.
function isTestPath(p) {
  return TEST_PATH_RE.test(p || '')
}

// A Coder's owned-files list with every test path removed. Phase 1a feeds the
// result to the Coder as "Files you own (production only)" so the Coder never
// authors its own tests — the dedicated Tester does, preserving the
// independent-Tester check. Extracted (not inline) so it is unit-testable.
function prodFilesOnly(files) {
  return (files || []).filter(f => !isTestPath(f))
}

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

// Pre-review: what the reviewer agent returns after running pre_review.py.
// contentKey is the group's base-independent content key (sha256 of its
// changed files' final blob shas); `approved` means the review verdict would
// let the gate pass. blockers feed routeFixes().
const PREREVIEW_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['groups', 'wholediff', 'allClean'],
  properties: {
    groups: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['index', 'contentKey', 'verdict', 'approved'],
        properties: {
          index: { type: 'integer' },
          contentKey: { type: 'string' },
          verdict: { type: 'string' },
          approved: { type: 'boolean' },
          blockers: { type: 'array', items: FINDING },
        },
      },
    },
    wholediff: {
      type: 'object',
      additionalProperties: false,
      required: ['verdict'],
      properties: {
        verdict: { type: 'string' },
        blockers: { type: 'array', items: FINDING },
      },
    },
    allClean: { type: 'boolean' },
  },
}

// SDD-003 — what the pre-review HANDLE agent returns each iteration of the
// NEEDS_MANIFEST sub-loop. FLAT object (NO top-level oneOf/anyOf/allOf): the
// Anthropic API rejects a top-level oneOf in an agent({schema}) tool input_schema
// ("400 input_schema does not support oneOf, allOf, or anyOf at the top level" —
// adding type:'object' does NOT help; proven by probe + the D7c smoke). So the two
// logical branches are merged into ONE flat shape with ALL fields optional, and
// the JS does the discrimination (it already branches on the field VALUES):
//   branch A — { needAwait:true }: the handle authored the manifests AND itself
//              relaunched `pre_review.py --pending` detached; the JS re-enters the
//              wait with NO new launch. (runPreReviewWait checks `needAwait===true`.)
//   branch B — the full PREREVIEW body { groups, wholediff, allClean }, consumed by
//              the unchanged round-loop body. (runPreReviewWait validates groups[] +
//              wholediff are present before returning it, so a malformed handle
//              degrades cleanly instead of crashing the round loop on pr.groups.)
const HANDLE_PREREVIEW_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    needAwait: { type: 'boolean' },
    groups: PREREVIEW_SCHEMA.properties.groups,
    wholediff: PREREVIEW_SCHEMA.properties.wholediff,
    allClean: PREREVIEW_SCHEMA.properties.allClean,
  },
}

// Land-audit: what the verifier agent returns from `pre_review.py --verify-range`.
const VERIFY_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['commits'],
  properties: {
    commits: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['sha', 'contentKey'],
        properties: { sha: { type: 'string' }, contentKey: { type: 'string' } },
      },
    },
  },
}

// Plan-validation: what the validator agent returns from
// `pre_review.py --validate-plan` (fix A — deterministic file-disjointness).
const PLAN_VALIDATION_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['valid', 'overlaps', 'uncovered', 'normalizedPlan'],
  properties: {
    valid: { type: 'boolean', description: 'true iff no path is in >=2 groups' },
    overlaps: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['path', 'groups'],
        properties: { path: { type: 'string' }, groups: { type: 'array', items: { type: 'integer' } } },
      },
    },
    uncovered: { type: 'array', items: { type: 'string' }, description: 'changed files in no group (advisory)' },
    normalizedPlan: COMMIT_PLAN_SCHEMA,
  },
}

// Acceptance: the final high-level "is the spec actually, fully delivered?"
// verdict. Low-level quality was the reviewers' job; this is the whole-feature
// view — read the spec intent + ACs + the real code + run the tests.
const ACCEPT_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['complete', 'testsPass', 'gaps', 'rationale'],
  properties: {
    complete: { type: 'boolean', description: 'true ONLY if every AC and the intended feature are genuinely implemented end-to-end' },
    testsPass: { type: 'boolean' },
    gaps: { type: 'array', items: FINDING, description: 'concrete, actionable shortfalls vs the spec' },
    rationale: { type: 'string' },
  },
}

// Like COMMIT_PLAN_SCHEMA but allows zero groups (the remediation delta may be
// empty if a fix changed nothing on disk).
const DELTA_PLAN_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  required: ['groups'],
  properties: {
    groups: {
      type: 'array',
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['message', 'files'],
        properties: {
          message: { type: 'string' },
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
    // SDD-003 (D9) — relaunch signal. When the collect agent makes an in-gate
    // LOCAL fix (manifest-fill / secret removal / WARNING fix) it returns
    // {landed:false, needRelaunch:true} WITHOUT git-committing in its own turn;
    // the while(!landed) loop re-runs the FULL launch->await->collect triple so
    // the re-commit's 15-60 min gate wait is ALSO JS-held, not just the first.
    needRelaunch: { type: 'boolean' },
  },
}

// SDD-003 (MF-A) — what the committer LAUNCH agent returns. FLAT object (NO
// top-level oneOf/anyOf/allOf): the Anthropic API rejects a top-level oneOf in an
// agent({schema}) tool input_schema ("400 ... does not support oneOf, allOf, or
// anyOf at the top level"; proven by probe + the D7c smoke, where land:launch
// 400'd SPAWN_RETRIES times and the engine landed NO commit). The two logical
// branches are merged into ONE flat shape with ALL fields optional; the JS does
// the discrimination on the field VALUES (landGroupViaGate):
//   branch A — idempotency short-circuit: { alreadyLanded:true, result }.
//   branch B — job launched detached: { started:true, outFile, doneTest }.
// Because the schema no longer REQUIRES per-branch fields, landGroupViaGate guards
// a malformed launch (neither a valid alreadyLanded nor a valid started shape) and
// degrades to POLL_EXHAUSTED rather than dereferencing undefined.
const LAND_LAUNCH_SCHEMA = {
  type: 'object',
  additionalProperties: false,
  properties: {
    alreadyLanded: { type: 'boolean' },
    result: COMMIT_RESULT_SCHEMA,
    started: { type: 'boolean' },
    outFile: { type: 'string' },
    doneTest: { type: 'string' },
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
// SDD-003 — JS-held long-wait (launch -> JS-await -> collect)
// ---------------------------------------------------------------------------
// The two long-wait points (parallel pre-review + the AI commit gate) take
// 15-60 min. A single agent CANNOT hold that wait: the harness auto-backgrounds
// any foreground command past ~10 min, a workflow agent({schema}) gets no
// background re-wake, and the StructuredOutput Stop-hook fails it the moment it
// ends a turn without a result. So the JS LOOP holds every wait by re-spawning a
// FRESH one-check poll agent per cycle; no single agent's turn spans the job.
// ===========================================================================

// POLL_PROTOCOL survives (TMS-102 / D2) ONLY as the short-sleep guidance INSIDE
// each per-cycle poll agent's prompt. Its old "poll back-to-back in one turn"
// loop role is now the JS loop's job — so a poll agent does EXACTLY ONE sleep +
// ONE readiness check and then RETURNS, never looping in-turn.
const POLL_PROTOCOL = [
  `HOW TO DO ONE readiness check for a long (15-60 min) background job WITHOUT being failed by the Stop-hook:`,
  `- Issue EXACTLY ONE Bash call with the Bash tool timeout set to 300000 ms: run \`sleep 270\` and then, in the SAME call, the one readiness test below.`,
  `- Do NOT loop in-turn: NEVER issue a second sleep, NEVER use a single \`while …; do sleep …; done\` loop or any command longer than ~5 min (the harness backgrounds it and you will not be re-woken). The JS workflow loop will re-spawn a fresh poll agent for the next cycle.`,
  `- Then RETURN immediately with { done: true } if the result is ready or { done: false } if not — do NOT wait for it to finish, do NOT read or interpret the result here (a later collect agent does that).`,
].join('\n')

// Indirection seam: production runs route every SDD-003 spawn through `agent`
// (the runtime-injected global); the control-flow test overrides `spawnAgent`
// with a stub so it exercises the REAL loop logic without spawning agents.
let spawnAgent = (typeof agent !== 'undefined' ? agent : undefined)

// awaitDetachedJob — the JS loop that HOLDS one long wait. It spawns one fresh,
// null-safe, one-check poll agent per cycle and returns when a poll reports done.
//   outFile  — the readiness target. For pre-review this is the JS-owned <outFile>
//              ("done" = target exists, written by an atomic mv); for the committer
//              this is the launch-RETURNED bg output path ("done" = a terminal gate
//              result is present). The launch step owns the freshness rm -f; this
//              helper only reads readiness.
//   opts     — { label, phase, doneTest } where doneTest is the literal Bash the
//              poll runs to decide readiness (it must echo READY when done).
// Returns true when the job is done; returns POLL_EXHAUSTED (a sentinel, NOT a
// throw) when POLL_CEILING or the MAX_POLL_AGENTS global budget is hit, so the
// caller degrades that wait to a recorded Known Concern and continues.
async function awaitDetachedJob(outFile, opts) {
  const { label, phase: phaseName, doneTest } = opts
  for (let cycle = 1; cycle <= POLL_CEILING; cycle++) {
    if (pollAgentsSpawned >= MAX_POLL_AGENTS) {
      // Global poll budget exhausted across ALL waits — degrade, do not abort.
      return POLL_EXHAUSTED
    }
    pollAgentsSpawned++
    const p = await spawnAgent(
      [
        `You are a one-check POLL agent inside an automated workflow. Your ONLY job is a single readiness check on a long detached job, then RETURN.`,
        POLL_PROTOCOL,
        `The readiness check (run it verbatim after the sleep, in the same Bash call): ${doneTest}`,
        `If it prints READY return { done: true }; otherwise return { done: false }. Return the minimal schema { done }.`,
      ].join('\n'),
      { agentType: 'general-purpose', label: `${label}:poll${cycle}`, phase: phaseName, schema: { type: 'object', additionalProperties: false, required: ['done'], properties: { done: { type: 'boolean' } } } },
    )
    // Null-safety: a null poll return is a terminal service error for THIS cycle.
    // It means "still waiting, re-poll" — never read .done off it, never count it
    // as done, never abort. Spawn a fresh poll on the next iteration.
    if (!p) continue
    if (p.done === true) return true
  }
  // Per-wait POLL_CEILING reached without "done".
  return POLL_EXHAUSTED
}

// Default per-site retry cap for a BOUNDED null-retry of a non-poll spawn
// (launch / handle / collect). A null return is a terminal service error for
// that one call; a TRANSIENT null clears within a couple of retries, but a
// SUSTAINED null (a real outage) must NOT spin forever toward the 1000-agent cap.
const SPAWN_RETRIES = 5

// spawnWithBudget — call a spawn fn, and on a null return (terminal service
// error) re-spawn up to maxRetries times, charging each attempt against the
// module-level MAX_POLL_AGENTS budget (so a sustained outage degrades EARLY, not
// at the 1000-agent cap). Returns the first truthy result, or the POLL_EXHAUSTED
// sentinel when the per-site retries OR the global budget are exhausted — the
// caller MUST check `=== POLL_EXHAUSTED` before dereferencing the result. This is
// what makes the four null-retry sites bounded by construction (Risk 3, AC-7);
// it never throws and never spins unbounded.
async function spawnWithBudget(fn, maxRetries = SPAWN_RETRIES) {
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    if (pollAgentsSpawned >= MAX_POLL_AGENTS) return POLL_EXHAUSTED
    pollAgentsSpawned++
    const r = await fn()
    if (r) return r
    // null → terminal service error this attempt; re-spawn (bounded).
  }
  return POLL_EXHAUSTED
}

// runPreReviewWait — the pre-review launch-once + NEEDS_MANIFEST handle sub-loop,
// extracted so it is unit-testable with injected collaborators (the inline
// round-loop body at the call site stays byte-for-byte unchanged; it just calls
// this to obtain `pr`). Contract (Integration points 1-4b):
//   launch ONCE before the loop; then do { await; handle } while (handle.needAwait).
//   The handle agent (branch A) authors manifests AND relaunches `--pending`
//   ITSELF, so the JS re-enters `await` with NO new launch (no double-launch).
//   A null handle return re-spawns the handle (handle carries its own already-
//   running guard, so a re-spawn never double-launches the review).
//   `accumulated` is the JS-owned full-set merge so the handle computes allClean
//   over the FULL group set, not the partial --pending subset.
// Returns a branch-B-shaped value (PREREVIEW_SCHEMA body) OR POLL_EXHAUSTED.
async function runPreReviewWait(deps) {
  const { launch, handle, outFile, doneTest, label, phaseName } = deps
  // launch ONCE — the handle relaunches --pending internally thereafter. Null-safe
  // AND bounded: a null launch return is a terminal service error → re-spawn up to
  // SPAWN_RETRIES (the launch's pgrep guard makes a re-spawn idempotent: if the
  // prior call DID start the job before erroring, the re-spawn finds it running
  // and no-ops). pre_review.py is read-only against a private index, so a restart
  // is safe (Approach / Assumptions). A SUSTAINED null degrades via POLL_EXHAUSTED.
  const lr = await spawnWithBudget(launch)
  if (lr === POLL_EXHAUSTED) return POLL_EXHAUSTED
  while (true) {
    const done = await awaitDetachedJob(outFile, { label, phase: phaseName, doneTest })
    if (done === POLL_EXHAUSTED) return POLL_EXHAUSTED
    // Null-safety + bound: a null handle return → re-spawn the handle (it is
    // guarded against an already-running --pending, so a re-spawn cannot
    // double-launch); a sustained null degrades via POLL_EXHAUSTED.
    const h = await spawnWithBudget(handle)
    if (h === POLL_EXHAUSTED) return POLL_EXHAUSTED
    if (h.needAwait === true) continue // branch A — re-enter await, no new launch
    // branch B — the full PREREVIEW body. HANDLE_PREREVIEW_SCHEMA is FLAT (no
    // top-level oneOf — the API forbids it), so it no longer REQUIRES groups[] +
    // wholediff. Guard them here: a malformed handle (not needAwait AND not a valid
    // PREREVIEW body) degrades to POLL_EXHAUSTED rather than letting the round-loop
    // body crash on pr.groups.
    if (!Array.isArray(h.groups) || !h.wholediff) return POLL_EXHAUSTED
    return h
  }
}

// landGroupViaGate — the committer launch -> JS-await -> collect triple for ONE
// commit attempt, extracted so it is unit-testable. EVERY attempt (the first AND
// every re-commit after an in-gate fix) goes through this triple, so every gate
// wait is JS-held (D9). Returns the COMMIT_RESULT the caller's while(!landed)
// loop branches on (landed / needRelaunch / blockingFindings / else), OR
// POLL_EXHAUSTED when the wait hits a bound.
//   launch  — stages + preflight + idempotency-guard + starts `git commit` via
//             run_in_background:true; RETURNS { outFile } (the harness-assigned
//             bg output path) or a short-circuit { alreadyLanded:true, result }.
//   collect — reads the bg output file and returns COMMIT_RESULT_SCHEMA; for an
//             exit-1 in-gate-fixable sub-case it does ONLY the local fix and
//             returns { landed:false, needRelaunch:true } (no git commit in-turn).
async function landGroupViaGate(deps) {
  const { launch, collect, label, phaseName } = deps
  // null launch → bounded re-spawn (idempotency guard makes it safe); a sustained
  // null degrades via POLL_EXHAUSTED. MUST check the sentinel BEFORE dereferencing.
  const lr = await spawnWithBudget(launch)
  if (lr === POLL_EXHAUSTED) return POLL_EXHAUSTED
  // idempotency short-circuit: the group's commit ALREADY landed (the only case
  // that may claim landed — there is no in-flight false-success guard; see the
  // committer launch prompt for why).
  // LAND_LAUNCH_SCHEMA is FLAT (no top-level oneOf — the API forbids it), so it no
  // longer REQUIRES per-branch fields; the JS does the discrimination here. An
  // alreadyLanded with no result degrades cleanly.
  if (lr.alreadyLanded) return lr.result || POLL_EXHAUSTED
  // A valid "started" branch carries both outFile and doneTest; a malformed launch
  // (neither a valid alreadyLanded nor a valid started shape) degrades to
  // POLL_EXHAUSTED rather than calling awaitDetachedJob(undefined,{doneTest:undefined}).
  if (!lr.outFile || !lr.doneTest) return POLL_EXHAUSTED
  const done = await awaitDetachedJob(lr.outFile, { label, phase: phaseName, doneTest: lr.doneTest })
  if (done === POLL_EXHAUSTED) return POLL_EXHAUSTED
  // null collect → bounded re-spawn; a sustained null degrades via POLL_EXHAUSTED.
  const cr = await spawnWithBudget(() => collect(lr.outFile))
  if (cr === POLL_EXHAUSTED) return POLL_EXHAUSTED
  return cr
}

// landGroupLoop — drives ONE commit group's while(!landed) loop, extracted from
// landGroups so the multi-cycle control flow (the needRelaunch re-entry + the
// MF-B budget degrade + the MF-C stall backstop + the blockingFindings backstop)
// is unit-testable. Collaborators are injected so the test can stub them:
//   viaGate(gateRound) → COMMIT_RESULT | POLL_EXHAUSTED  (the full launch->await->collect triple)
//   routeFixes(findings)  — routes blocking gate findings to the owning coders
//   deps: { knownConcerns, commitLedger, log, g, gi }
// Every long wait inside it is JS-held (viaGate holds the gate wait); it NEVER
// spins unbounded — POLL_EXHAUSTED, the needRelaunch stall cap, the
// blockingFindings STALL_ROUNDS backstop, and the verdict-ERROR path all break.
async function landGroupLoop(viaGate, routeFixes, deps) {
  const { knownConcerns, commitLedger, log, g, gi } = deps
  let landed = false
  let gateRound = 0
  let relaunchStreak = 0 // consecutive needRelaunch rounds (reset by a productive blockingFindings round)
  const gatePersist = new Map() // fpKey -> consecutive gate rounds the finding stayed open
  while (!landed) {
    gateRound++
    const cr = await viaGate(gateRound)
    // MF-B / Bound exhaustion (POLL_CEILING / MAX_POLL_AGENTS / sustained null) —
    // degrade, do NOT abort: record this group as not-landed and move on.
    if (cr === POLL_EXHAUSTED) {
      knownConcerns.push(`commit gate: job did not finish (poll/spawn budget exhausted) for group "${g.message}"; left uncommitted.`)
      break
    }
    knownConcerns.push(...(cr.knownConcerns || []))
    if (cr.landed) {
      commitLedger.push({ message: g.message, shaSummary: cr.shaSummary, warnings: cr.warnings, overridesAdded: cr.overridesAdded })
      landed = true
    } else if (cr.needRelaunch) {
      // The collect agent applied a LOCAL in-gate fix (manifest-fill / secret
      // removal / WARNING fix / coverage) and did NOT commit — re-run the FULL
      // JS-held triple so the re-commit's gate wait is JS-held too (D9).
      // MF-C backstop: a gate that keeps rejecting the fix must not spin toward
      // the 1000-agent cap. Cap CONSECUTIVE needRelaunch rounds (a productive
      // blockingFindings round resets the streak), set above the documented
      // 10-cycle worst case (AC-5) so legitimate long commits still land.
      relaunchStreak++
      if (relaunchStreak > MAX_NEEDRELAUNCH_ROUNDS) {
        knownConcerns.push(`Commit group "${g.message}" did not converge after ${MAX_NEEDRELAUNCH_ROUNDS} consecutive in-gate-fix relaunch rounds; left uncommitted.`)
        break
      }
      log(`Commit group ${gi + 1} gate round ${gateRound}: in-gate fix applied → relaunching the full commit cycle (relaunch streak ${relaunchStreak}/${MAX_NEEDRELAUNCH_ROUNDS}).`)
      continue
    } else if (cr.blockingFindings && cr.blockingFindings.length) {
      relaunchStreak = 0 // a productive (routed) round resets the needRelaunch streak
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

// preReviewDegrade — the pre-review main-loop's POLL_EXHAUSTED degrade, extracted
// so it is unit-testable (symmetric with landGroupLoop's tested degrade). This is
// SDD-003's OWN added check, which sits ABOVE the frozen :789-805 convergence body
// (approved-set build, allClean, routeFixes, pending recompute, round cap) — so
// extracting it touches none of that load-bearing logic. Returns true (and records
// a Known Concern) when the wait exhausted, so the caller `break`s the round loop
// and leaves the pending group(s) to the live commit gate; false otherwise.
function preReviewDegrade(pr, round, pendingLen, knownConcerns) {
  if (pr === POLL_EXHAUSTED) {
    knownConcerns.push(`pre-review: job did not finish within the poll ceiling (round ${round}); ${pendingLen} group(s) will go through the live commit gate instead of the fast-path.`)
    return true
  }
  return false
}

// SDD-003 test seam (Seam B): the engine file is a top-level-await script with
// runtime-injected globals (agent/phase/parallel/log/args) and a tail `return`,
// so it loads via neither require nor import — only the Workflow runtime's
// AsyncFunction wrap. The control-flow test re-creates that wrap and sets
// __IMPL_TEST__, bailing HERE (before the first real agent() call) with the
// hoisted helpers + bounds exposed, so it exercises the REAL loop logic without
// running the engine. Guarded with typeof so production (no such binding) never
// ReferenceErrors and never bails.
if (typeof __IMPL_TEST__ !== 'undefined' && __IMPL_TEST__) {
  return {
    isTestPath,
    prodFilesOnly,
    deriveTasksDir,
    awaitDetachedJob,
    spawnWithBudget,
    runPreReviewWait,
    preReviewDegrade,
    landGroupViaGate,
    landGroupLoop,
    POLL_CEILING,
    MAX_POLL_AGENTS,
    SPAWN_RETRIES,
    MAX_NEEDRELAUNCH_ROUNDS,
    POLL_EXHAUSTED,
    HANDLE_PREREVIEW_SCHEMA,
    COMMIT_RESULT_SCHEMA,
    LAND_LAUNCH_SCHEMA,
    // Every agent-facing tool input_schema — the regression test iterates this to
    // assert each is API-valid (top-level type:'object', no top-level oneOf/anyOf/
    // allOf, which the Anthropic API rejects). Add new agent schemas here.
    allSchemas: {
      CODER_SCHEMA, CODER_FIX_SCHEMA, TESTER_SCHEMA, REVIEWER_SCHEMA, RE_REVIEW_SCHEMA,
      COMMIT_PLAN_SCHEMA, PREREVIEW_SCHEMA, HANDLE_PREREVIEW_SCHEMA, VERIFY_SCHEMA,
      PLAN_VALIDATION_SCHEMA, ACCEPT_SCHEMA, DELTA_PLAN_SCHEMA, COMMIT_RESULT_SCHEMA,
      LAND_LAUNCH_SCHEMA, SCRIBE_SCHEMA,
    },
    setSpawnAgent: (fn) => { spawnAgent = fn },
    resetPollCounter: () => { pollAgentsSpawned = 0 },
    setPollCounter: (n) => { pollAgentsSpawned = n },
    getPollCounter: () => pollAgentsSpawned,
  }
}

// ===========================================================================
// PHASE 1a — Code
// ===========================================================================
phase('Code')
log(`Spawning ${CODERS.length} coder(s) from the Work breakdown.`)

const coderResults = (await parallel(
  CODERS.map((c, i) => () => {
    // Defense in depth: a Coder writes PRODUCTION code only. The dedicated Tester
    // (next phase) writes ALL tests. Strip any test paths the Work breakdown
    // assigned to this coder so the "Files you own" list never tells a coder to
    // author its own tests — that would defeat the independent-Tester check.
    const prodFiles = prodFilesOnly(c.files)
    return agent(
      [
        preamble('Coder'),
        `Read your role instructions: ~/.claude/agents/coder.md`,
        `Your scope (Work breakdown → ${c.name}): ${c.scope}`,
        `Files you own (production only): ${prodFiles.join(', ')}`,
        `Do NOT touch any other files — they belong to other coders.`,
        `Implement production code only. A dedicated Tester writes ALL tests in the next phase, so do NOT create or edit any test file (tests/…, *_test.*, *.spec.*, test_*).`,
        `Implement your scope fully. Return changedFiles + implementationSummary.`,
      ].join('\n'),
      { agentType: 'Coder', label: `code:${c.name}`, phase: 'Code', schema: CODER_SCHEMA },
    )
  }),
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
    if (f.file && isTestPath(f.file)) testFindings.push(f)
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
// frontmatter (status: review), and moves the spec to {dir}/5-review/.
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
    `Then MOVE the spec file from ${TASKS_DIR}/4-in-progress/ to ${TASKS_DIR}/5-review/ (git mv).`,
    `Do this with Bash inside ${WORKTREE}. Return done=true, the implementationSummary text, and the manualReviewSteps you wrote.`,
  ].join('\n'),
  { agentType: 'general-purpose', label: 'land:scribe', phase: 'Land', schema: SCRIBE_SCHEMA },
)

// 4.2 — Plan the FEWEST cohesive commits (one per logical unit), file-disjoint.
// SDD-002: size is a SOFT target overridden by cohesion — a coupled feature
// stays ONE commit even when big; pre-review chunk-reviews it (the gate already
// does). Disjointness stays a HARD rule: it's what keeps each group's isolated
// diff content-stable (fast-path) AND base-stable (committing one doesn't shift
// another). validatePlanGroups enforces disjointness deterministically (LLM-free).
function planPrompt(extra) {
  return [
    preamble('Commit planner'),
    `Plan the FEWEST coherent commits — ONE per logical unit: a writer + its consumers + THAT unit's tests, together in one group. A cohesive feature is ONE commit even if large; do NOT split it just to stay small. Keep genuinely independent changes (unrelated features, config/infra, the finalized spec docs) as their own commits so bisect still works — cohesion, never maximum size, decides the boundary.`,
    `FILE-DISJOINT (hard rule): NO file may appear in more than one group — never split a file's changes across groups.`,
    `Shared files (e.g. tests/__init__.py, __manifest__.py, a shared compose/config file) are owned WHOLE by exactly ONE group — typically the integration/registration group, committed last — never split line-by-line across groups.`,
    `Size (~400 added production lines; tests/docs/config don't count) is a SOFT target, overridden by cohesion: a coupled unit over the limit stays one commit (pre-review chunk-reviews it). Never "all code" then "all tests"; never a tests-only commit.`,
    `Conventional commit subjects, each prefixed with "${TASK}": e.g. "feat(${TASK}): ...".`,
    `Inspect the working tree with Bash (git status --porcelain, git diff --stat) inside ${WORKTREE} to enumerate every changed/new file, including the moved spec.`,
    `Return groups[] of { message, files[] } covering EVERY changed file exactly once.`,
    extra || '',
  ].join('\n')
}

function overlapFixLines(overlaps) {
  return [
    `Your previous plan put these files in MULTIPLE groups. Re-plan so EVERY file is in exactly ONE group (merge or move as needed):`,
    ...overlaps.map(o => `- ${o.path} (currently in groups ${o.groups.join(', ')})`),
  ].join('\n')
}

// Deterministic file-disjointness gate (fix A). Runs the LLM-free
// pre_review.py --validate-plan; on an overlap it re-prompts the planner ONCE
// with the concrete overlaps, then falls back to the validator's deterministic
// disjoint merge. `uncovered` is advisory (surfaced as a concern, never blocks).
// Returns the validated, file-disjoint groups. A <2-group plan can't overlap →
// returned as-is with no agent call.
async function validatePlanGroups(groups, tag, replan) {
  if (groups.length < 2) return groups
  const runValidate = async (grps, attempt) => {
    const planJson = JSON.stringify({ groups: grps.map(g => ({ message: g.message, files: g.files })) })
    return agent(
      [
        preamble('Plan validator'),
        `Validate a commit plan for file-disjointness. This is an LLM-FREE check — run the script and return its JSON verbatim. Do NOT commit or edit anything.`,
        `1. Write this JSON verbatim to ${WORKTREE}/.review/plan-${tag}.json (mkdir -p the dir first):`,
        planJson,
        `2. From ${WORKTREE}, run exactly:`,
        `   python3 ~/.claude/review/pre_review.py --validate-plan ${WORKTREE}/.review/plan-${tag}.json --repo-root ${WORKTREE}`,
        `3. It emits JSON { valid, overlaps:[{path,groups:[idx]}], uncovered:[path], normalized_plan:{groups:[{message,files}]} }.`,
        `Return PLAN_VALIDATION_SCHEMA: { valid, overlaps, uncovered, normalizedPlan: normalized_plan } verbatim from that JSON.`,
      ].join('\n'),
      { agentType: 'general-purpose', label: `${tag}:validate${attempt > 1 ? ':retry' : ''}`, phase: 'Land', schema: PLAN_VALIDATION_SCHEMA },
    )
  }
  const noteUncovered = (v) => { if (v.uncovered.length) knownConcerns.push(`Commit plan (${tag}) leaves ${v.uncovered.length} changed file(s) uncovered (advisory): ${v.uncovered.join(', ')}`) }

  const v1 = await runValidate(groups, 1)
  noteUncovered(v1)
  if (v1.valid) return groups

  log(`Plan (${tag}) has ${v1.overlaps.length} file overlap(s) across groups → re-prompting planner once.`)
  const fixed = await replan(v1.overlaps)
  const v2 = await runValidate(fixed, 2)
  noteUncovered(v2)
  if (v2.valid) return fixed

  knownConcerns.push(`Commit plan (${tag}) could not be made file-disjoint by the planner; used the deterministic merged plan (${v2.normalizedPlan.groups.length} group(s)).`)
  return v2.normalizedPlan.groups
}

const initialPlan = await agent(
  planPrompt(''),
  { agentType: 'general-purpose', label: 'land:plan', phase: 'Land', schema: COMMIT_PLAN_SCHEMA },
)
const plan = {
  groups: await validatePlanGroups(initialPlan.groups, 'land', async (overlaps) => {
    const r = await agent(
      planPrompt(overlapFixLines(overlaps)),
      { agentType: 'general-purpose', label: 'land:replan', phase: 'Land', schema: COMMIT_PLAN_SCHEMA },
    )
    return r.groups
  }),
}

// 4.2.5 — Parallel pre-review (fires for 2+ commits). This is the load-bearing
// SDD-002 win: each commit's review→fix convergence loop (10-15 rounds, hours
// each at the gate) runs HERE for all commits AT ONCE, so a 3-commit feature
// converges in ~one commit's wall-clock instead of three sequential ones. Big
// cohesive groups are chunk-reviewed in parallel (per-group manifest authored by
// this agent, like the committer fills the gate's). Clean groups get an on-disk
// approval marker; the committer's `git commit` then fast-paths past the slow LLM
// review for that content — reviewed exactly once. `approved` is the workflow's
// TRUSTED set, built from the reviewer agent's return (not disk) and later
// cross-checked by an independent verifier — so the committer (no-knowledge of
// the marker format) cannot smuggle an unreviewed change past the audit.
const PREREVIEW_MIN_GROUPS = 2 // skip only for a single commit (nothing to parallelize)
const PREREVIEW_MAX_ROUNDS = 12 // must cover real convergence (10-15 cycles); else commits fall to the sequential gate and the parallel win is lost. Leftovers still just hit the live gate.
const approved = new Set() // content keys the workflow trusts as CLEAN
let lastPreReviewGroups = [] // final per-group records → audit exclusion (commits pre-review left for the gate)
let preReviewRan = false
if (plan.groups.length >= PREREVIEW_MIN_GROUPS) {
  preReviewRan = true
  phase('Pre-review')
  const planJson = JSON.stringify({ groups: plan.groups.map(g => ({ message: g.message, files: g.files })) })
  let pending = plan.groups.map((_g, i) => i)
  // SDD-003 (Integration point 4b) — FULL-SET MERGE. `pre_review.py --pending
  // <subset>` reviews ONLY the requested indices and reports all_clean over THAT
  // subset, so a relaunch's output file does not carry the earlier-approved
  // groups. The spec's literal mechanism (a JS-owned Map fed to the handle as
  // priorState) cannot work for the within-round manifest case: branch A returns
  // only { needAwait } (locked schema), so the JS never observes the first FULL
  // review before a manifest relaunch shrinks the file to the subset. DEVIATION:
  // the full-set state is instead carried by a handle-maintained on-disk
  // accumulator (see preReviewAccum below) which the handle reads+merges+writes
  // each call and whose lifecycle is tied to --reset, exactly like pre_review.py's
  // own clear_approvals. The branch-B handle returns the full set from it.
  // SDD-003 (Integration points 1-3) — JS-owned readiness target. The launch/
  // relaunch shell writes the job to <outFile>.part and atomically `mv`s it to
  // <outFile> ONLY on a complete result, so "done" = "the target exists" and a
  // poll can never read a partial result (Risk 4). The path is STABLE across all
  // launches/relaunches, so EVERY launch/relaunch MUST `rm -f` it first (Risk 6).
  const preReviewOut = `${WORKTREE}/.review/prereview-out.json`
  const preReviewDoneTest = `test -e "${preReviewOut}" && echo READY || echo NOT_READY`
  // The handle-maintained full-set accumulator (see the DEVIATION note above).
  // The handle reads+merges+writes it each call; only the --reset launch clears
  // it. The branch-B handle returns the full set from it, so the unchanged body
  // below sees every group.
  const preReviewAccum = `${WORKTREE}/.review/prereview-accum.json`
  for (let round = 1; round <= PREREVIEW_MAX_ROUNDS && pending.length; round++) {
    const reset = round === 1
    const pendingArg = reset ? '--reset' : `--pending ${pending.join(',')}`
    // launch ONCE per outer round — the handle relaunches --pending itself.
    const launchPreReview = () => spawnAgent(
      [
        preamble('Pre-reviewer (launch)'),
        `LAUNCH the parallel pre-review of the planned commit groups DETACHED, then RETURN immediately. Do NOT wait for it to finish, do NOT commit anything, do NOT read the result (a later agent collects it). The JS workflow loop holds the wait.`,
        `0. IDEMPOTENCY GUARD (run FIRST — makes a re-spawn after a transient error safe): if a pre-review is already running, do NOT start a second one. Run \`pgrep -f "pre_review.py.*--repo-root ${WORKTREE}"\`; if it finds a process, the job is already launched — return done=true WITHOUT starting another (and skip the freshness rm, which would race the running job's output).`,
        `1. Write this JSON verbatim to "${WORKTREE}/.review/prereview-plan.json" (mkdir -p the dir first):`,
        planJson,
        `2. FRESHNESS (mandatory, ONLY when step 0 found nothing running): the readiness target is reused across rounds, so a stale file would falsely report "done". Run \`rm -f "${preReviewOut}" "${preReviewOut}.part"\` BEFORE starting the job.${reset ? ` This is the FULL --reset round, so ALSO clear the full-set accumulator: \`rm -f "${preReviewAccum}"\` (it is rebuilt from this full review; later --pending rounds must NOT clear it).` : ''}`,
        `3. From ${WORKTREE}, start the review DETACHED writing to a .part file and atomically renaming on completion (so "done" = the target exists, never a partial read). ALL paths below are double-quoted because the worktree path may contain spaces. Run EXACTLY:`,
        `   ( python3 ~/.claude/review/pre_review.py --plan "${WORKTREE}/.review/prereview-plan.json" --repo-root "${WORKTREE}" ${pendingArg} > "${preReviewOut}.part" 2> "${WORKTREE}/.review/prereview-err.log" && mv "${preReviewOut}.part" "${preReviewOut}" ) &`,
        `   It reviews each group in parallel against a private git index and writes an approval marker for every CLEAN group. The script itself is NOT modified; the shell redirect produces the atomic file.`,
        `Return done=true once you have STARTED the detached job (it is launched, not finished).`,
      ].join('\n'),
      { agentType: 'general-purpose', label: `prereview:launch:round${round}`, phase: 'Pre-review', schema: { type: 'object', additionalProperties: false, required: ['done'], properties: { done: { type: 'boolean' } } } },
    )
    // handle — runs after each "done": parse the FINAL JSON, merge into the
    // full-set view, and EITHER author manifests + relaunch --pending detached
    // (branch A {needAwait:true}, NO new launch by the JS) OR return the full
    // PREREVIEW_SCHEMA body (branch B). Carries its own already-running guard so a
    // null-handle re-spawn cannot double-launch (Integration point 4a).
    const handlePreReview = () => spawnAgent(
      [
        preamble('Pre-reviewer (handle)'),
        `The detached pre-review just finished. READ its result, MERGE it into the full-set accumulator, then EITHER (A) author manifests + relaunch the pending groups detached, OR (B) return the merged full-set result. This does NOT commit anything.`,
        `1. Read the FINAL run's JSON from ${preReviewOut} (use ${WORKTREE}/.review/prereview-err.log if it is empty): { groups:[{index,message,content_key,verdict,too_big,approved,blockers}], wholediff:{verdict,blockers}, all_clean }. Each "blockers" is a list of reviewer lines for BLOCK verdicts, like "[F1] [CRITICAL] path:line — description".`,
        `2. FULL-SET ACCUMULATOR (this run reviewed only the subset ${pendingArg}; a --pending run's output does NOT carry the earlier-reviewed groups, so the file alone is incomplete). The full-set state lives at ${preReviewAccum}, keyed by group index:`,
        `   a. Read ${preReviewAccum} if it exists (a JSON object { "<index>": {index,contentKey,verdict,approved,blockers}, ... }); treat a missing/empty file as {}.`,
        `   b. MERGE this run's groups OVER it (current run wins for the indices it reviewed; indices it did NOT touch keep their last-known record). For each reviewed group store {index, contentKey:content_key, verdict, approved, blockers:(parsed from its blocker lines)}.`,
        `   c. Write the merged object BACK to ${preReviewAccum}. This accumulator is your single source of truth for the FULL group set — it survives the subset relaunches within this round.`,
        `3. BRANCH A — MANIFESTS for big groups: if ANY group's verdict is "NEEDS_MANIFEST" it is a big cohesive commit that must be CHUNK-reviewed. The script wrote a scaffold at ${WORKTREE}/.review/prereview/group-<index>/manifest.yaml. For EACH such group OPEN that file and fill in 'chunks:' — split the group's files into <=6 chunks, each <=400 added PRODUCTION lines (tests/docs/config don't count), every changed file in exactly ONE chunk. Each chunk MUST be a mapping with two keys: 'id' (a slug matching [a-z][a-z0-9_]* — lowercase letters/digits/underscores, starts with a letter; the key is literally 'id', NOT 'name') and 'files' (a non-empty list where each entry is '- path: <repo-relative path>' plus 'line_ranges:' set to either the string 'all' for a whole file OR a LIST of [start, end] pairs like [[1, 250]] to take part of an oversized file — a bare string like "1-250" is INVALID). Whole files use 'all'; split a single oversized file across chunks with disjoint pair-lists. Do NOT edit 'diff_hash'. (If a group truly cannot fit in 6 chunks, leave its manifest's chunks empty so it is reviewed once at the commit gate, and do NOT keep relaunching it.)`,
        `   AFTER authoring manifests (and AFTER writing the accumulator in step 2c), RELAUNCH the review DETACHED for exactly the NEEDS_MANIFEST indices, with the SAME freshness + atomic-rename discipline (ALL paths double-quoted — the worktree path may contain spaces) — but do NOT clear the accumulator (only --reset clears it): FIRST guard against an already-running pending review — if \`pgrep -f "pre_review.py.*--repo-root ${WORKTREE}.*--pending"\` finds one, do NOT start a second (just return {needAwait:true}). This pattern is SCOPED to THIS worktree (it matches --repo-root ${WORKTREE} AND --pending, in the relaunch command's arg order) so a concurrent /implement-wf run in ANOTHER worktree never makes you skip your own relaunch. Otherwise run \`rm -f "${preReviewOut}" "${preReviewOut}.part"\` then \`( python3 ~/.claude/review/pre_review.py --plan "${WORKTREE}/.review/prereview-plan.json" --repo-root "${WORKTREE}" --pending <NEEDS_MANIFEST indices> > "${preReviewOut}.part" 2> "${WORKTREE}/.review/prereview-err.log" && mv "${preReviewOut}.part" "${preReviewOut}" ) &\`. Then return EXACTLY { needAwait: true } — the JS re-enters the wait with NO new launch.`,
        `4. BRANCH B — if NO group is NEEDS_MANIFEST: return the FULL set from the merged accumulator (NOT just this run's subset). For each accumulated group return {index, contentKey, verdict, approved} with its blockers[] (each {id, severity:"CRITICAL", file, description}). Parse wholediff.blockers from this run's file the same way. Return groups[], wholediff{verdict,blockers}, allClean (= true iff EVERY group in the accumulated full set is approved AND wholediff is OK/SKIP).`,
        `Return HANDLE_PREREVIEW_SCHEMA: EITHER { needAwait: true } (branch A) OR the full body { groups[], wholediff{verdict,blockers}, allClean } (branch B).`,
      ].join('\n'),
      { agentType: 'general-purpose', label: `prereview:handle:round${round}`, phase: 'Pre-review', schema: HANDLE_PREREVIEW_SCHEMA },
    )
    const pr = await runPreReviewWait({
      launch: launchPreReview,
      handle: handlePreReview,
      outFile: preReviewOut,
      doneTest: preReviewDoneTest,
      label: `prereview:round${round}`,
      phaseName: 'Pre-review',
    })
    // Bound exhaustion (POLL_CEILING / MAX_POLL_AGENTS) — degrade, do NOT abort:
    // record the wait as a Known Concern and leave the pending groups to the live
    // commit gate (the existing degrade pattern), then stop the pre-review loop.
    // review-note: preReviewDegrade is unit-tested (implement.test.js::test_prereview_mainloop_exhausted_degrades, which asserts the Known-Concern record + the break signal). This one-line break-on-exhaustion stays INLINE because extracting the frozen :789-805 convergence body (approved-set build, allClean test, routeFixes, pending recompute, audit-exclusion) to unit-test a single branch would force a byte-for-byte change to that load-bearing region — disproportionate and riskier than the branch itself; the integration is field-validated (D7c/d).
    if (preReviewDegrade(pr, round, pending.length, knownConcerns)) break
    // pr.groups is the branch-B FULL set (from the handle's accumulator), so the
    // unchanged body below sees every group — approved set, allClean test,
    // routeFixes, pending recompute, and the audit-exclusion list all operate on
    // the complete plan, not a --pending subset.
    lastPreReviewGroups = pr.groups
    for (const gr of pr.groups) if (gr.approved && gr.contentKey) approved.add(gr.contentKey)
    const wholediffClean = pr.wholediff.verdict === 'OK' || pr.wholediff.verdict === 'SKIP'
    if (pr.allClean || (pr.groups.every(g => g.approved) && wholediffClean)) {
      log(`Pre-review clean after ${round} round(s); ${approved.size} group(s) approved.`)
      break
    }
    const blockers = [].concat(...pr.groups.map(g => g.blockers || [])).concat(pr.wholediff.blockers || [])
    if (blockers.length) {
      log(`Pre-review round ${round}: ${blockers.length} blocking finding(s) → routing fixes.`)
      await routeFixes(blockers)
    }
    // A whole-diff fix may touch any group → re-review all; otherwise just the unapproved ones.
    pending = wholediffClean ? pr.groups.filter(g => !g.approved).map(g => g.index) : plan.groups.map((_g, i) => i)
    if (round === PREREVIEW_MAX_ROUNDS && pending.length) {
      knownConcerns.push(`Pre-review did not fully converge after ${PREREVIEW_MAX_ROUNDS} rounds; ${pending.length} group(s) will go through the live commit gate instead of the fast-path.`)
    }
  }
  phase('Land')
}

// 4.3 — Land each group sequentially through the commit gate, with a fix loop
// bounded on every path (STALL_ROUNDS on blockingFindings; MAX_NEEDRELAUNCH_ROUNDS
// on needRelaunch). The committer agent runs ALONE (never alongside others).
const commitLedger = []
// Land a set of logical commit groups sequentially through the gate. Extracted
// into a function so the acceptance-remediation loop (4.5) can re-land the
// delta it produces. `fastpathEnabled` gates the SDD_REVIEW_FASTPATH hint.
async function landGroups(groups, fastpathEnabled) {
 for (let gi = 0; gi < groups.length; gi++) {
  const g = groups[gi]
  // SDD-003 (D9, Integration points 3 & 5) — EVERY commit attempt (the first AND
  // every re-commit after an in-gate fix) goes through the full JS-held
  // launch -> awaitDetachedJob -> collect triple, so every gate wait is JS-held,
  // not just the first. The launch RETURNS the harness-assigned bg output path
  // (run_in_background); a null launch is safe to re-spawn because of the
  // idempotency guard. The collect, for an in-gate-fixable exit-1 sub-case, does
  // ONLY the local fix and returns needRelaunch (no in-turn git commit), so the
  // loop re-runs the WHOLE triple. The loop control + its MF-B/MF-C backstops
  // live in landGroupLoop (unit-tested); viaGate is the per-cycle triple.
  const viaGate = (gateRound) => {
    const landLaunch = () => spawnAgent(
      [
        preamble('Committer (launch)'),
        `LAUNCH a commit for ONE logical group, then RETURN immediately — do NOT wait for the AI review gate (the JS workflow loop holds that wait), do NOT read the result (a later collect agent does).`,
        // IDEMPOTENCY: only an ALREADY-LANDED commit may claim landed. There is
        // deliberately NO "in-flight commit" guard: returning landed for an
        // in-flight (not-yet-finished) commit is a FALSE success — the gate may
        // still block/crash, but landGroupLoop would record it landed and advance,
        // silently dropping the commit. Instead we rely on git's own serialization:
        // a null-launch re-spawn that starts a second `git commit` while the first
        // is mid-gate fails fast ("index.lock exists"), and a later retry's
        // already-landed check (case a) picks up the first commit once its gate
        // completes and returns the REAL landed result. So the rare
        // null-re-spawn-mid-commit case converges via git's lock + the already-landed
        // check, with NO false landed:true and NO silent drop — strictly safer.
        `IDEMPOTENCY GUARD (run FIRST — makes a re-spawn safe, never double-commits), scoped to THIS worktree (paths double-quoted — the worktree path may contain spaces):`,
        `  ALREADY-LANDED check — run \`git -C "${WORKTREE}" log --oneline ${BASE}..HEAD\` and look for a commit whose subject is EXACTLY this group's conventional subject (it is ALREADY prefixed with the task id "${TASK}" — match it verbatim, do NOT add another prefix):`,
        `      ${g.message}`,
        `If that subject is ALREADY present (the commit really landed), do NOT start another — return { alreadyLanded:true, result:{ landed:true, verdict:"CLEAN", shaSummary:"<the existing short hash + subject>", blockingFindings:[], warnings:[], overridesAdded:[], knownConcerns:[] } }.`,
        `If a \`git commit\` for this group is still RUNNING (e.g. \`git commit\` fails fast with "index.lock exists"), do NOT fabricate a landed result — just proceed to start the commit normally; git's own lock serializes it and a later already-landed check will pick up the real result.`,
        `Otherwise: follow the commit skill procedure (~/.claude/skills/commit/SKILL.md): security scan, Phase 3.5 coverage+assert preflight (a fresh coverage.xml is required), stash-guard, then launch \`git commit\` via the Bash tool's run_in_background:true (the commit-skill way — NEVER a shell \`&\`, which detaches the commit and leaves HEAD unchanged). The AI review gate takes 15-25 min and runs in the BACKGROUND.`,
        ...(fastpathEnabled
          ? [`This run pre-reviewed the commits in parallel. Prefix the git commit with the env var SDD_REVIEW_FASTPATH=1 (i.e. \`SDD_REVIEW_FASTPATH=1 git commit ...\`): a commit whose changes already passed pre-review then skips the redundant LLM review automatically — the deterministic gates (coverage/assert/secrets) still run. If the commit was not pre-approved the full review just runs as normal; never try to force a skip any other way.`]
          : []),
        `Commit ONLY this logical group:`,
        `  message: ${g.message}`,
        `  files: ${g.files.join(', ')}`,
        `Work inside ${WORKTREE}. Stage only these files (never -A / .).`,
        `When you launch the commit via run_in_background:true the Bash tool assigns an OUTPUT-FILE path for the background process. RETURN that exact path as outFile, and a doneTest = a single Bash one-liner that prints READY when the gate has produced a TERMINAL result (a commit SHA, or "Review BLOCKED"/"### Upheld findings", or "manifest auto-scaffolded", or a preflight exit-2/3 marker) in that output file, else NOT_READY. DOUBLE-QUOTE the output-file path (it may contain spaces) — e.g. \`grep -qE '<short-sha>|Review BLOCKED|### Upheld findings|manifest auto-scaffolded|preflight (failed|crashed)' "<outFile>" && echo READY || echo NOT_READY\`.`,
        `Return { started:true, outFile, doneTest }. (If you took the idempotency short-circuit, return { alreadyLanded:true, result } instead.)`,
      ].join('\n'),
      { agentType: 'general-purpose', label: `land:launch:${gi + 1}:${gateRound}`, phase: 'Land', schema: LAND_LAUNCH_SCHEMA },
    )
    const landCollect = (outFile) => spawnAgent(
      [
        preamble('Committer (collect)'),
        `The background commit's AI review gate has finished. READ its result from the background output file "${outFile}" (double-quote it when you cat/grep it — the path may contain spaces) and return the structured outcome. Do NOT issue a fresh \`git commit\` in your own turn — if a re-commit is needed you signal it with needRelaunch and the JS re-runs the whole launch->wait->collect cycle.`,
        ``,
        `READING THE GATE RESULT — the git commit exit code (in "${outFile}") is authoritative:`,
        `- exit 0  → commit landed. Return landed=true with shaSummary (\`git -C "${WORKTREE}" log -1 --oneline\`).`,
        `- exit 2  → coverage/assert preflight failed (usually a missing/stale coverage.xml or an untested new line / weak assertion). Regenerate coverage / add the missing test or assertion ON DISK, then return { landed:false, needRelaunch:true } so the JS re-commits. This is yours to fix.`,
        `- exit 3  → preflight gate crashed. Return landed=false, verdict ERROR, with the crash text in knownConcerns (NO needRelaunch).`,
        `- exit 1  → BLOCK. Read the hook output to tell which kind:`,
        `    • "manifest auto-scaffolded" → fill .review/manifest.yaml (group files <=300 prod lines/chunk, <=12 chunks) ON DISK, then return { landed:false, needRelaunch:true }.`,
        `    • "Review BLOCKED" / "Chunked review BLOCKED" → parse the "### Upheld findings (blocking)" section. Each blocking line looks like "[F1] [CRITICAL] path:line — description". Those CRITICALs are what blocks the commit. The "### Warnings:" section lists advisory "[WARNING] ..." lines.`,
        ``,
        `AUTONOMY RULES (no human is available) for an exit-1 review BLOCK — apply ONLY the LOCAL on-disk fix, then signal needRelaunch (NEVER commit in your own turn):`,
        `- gitleaks/semgrep secret hit: it is your own generated code — remove the secret ON DISK (use an env var or an obviously-fake placeholder), then return { landed:false, needRelaunch:true }.`,
        `- WARNINGs: fix the ones you can within this group's files ON DISK, then return { landed:false, needRelaunch:true } (advisory, but the directive says fix-in-one-pass).`,
        `- An UPHELD CRITICAL you have already tried to fix once and judge to be a false positive: add a SCOPED override (a note in .claude/review_prompt.md, or a rule-id in .semgrep-exclude-rules) ON DISK AND record the override + your reasoning in knownConcerns, then return { landed:false, needRelaunch:true } so the re-commit lands.`,
        `- An UPHELD CRITICAL in another coder's production logic that you CANNOT fix from this group's files: return landed=false with it in blockingFindings — set { id, severity:"CRITICAL", file (the path:line from the finding), description } and NO needRelaunch, so the loop routes it to the owner.`,
        ``,
        `Return COMMIT_RESULT: landed, verdict (CLEAN/BLOCKED/ERROR), shaSummary, blockingFindings (each with id+file+description), warnings, overridesAdded, knownConcerns, and needRelaunch when (and only when) you applied a local fix and want the JS to re-commit.`,
      ].join('\n'),
      { agentType: 'general-purpose', label: `land:collect:${gi + 1}:${gateRound}`, phase: 'Land', schema: COMMIT_RESULT_SCHEMA },
    )
    return landGroupViaGate({ launch: landLaunch, collect: landCollect, label: `land:commit:${gi + 1}:${gateRound}`, phaseName: 'Land' })
  }
  await landGroupLoop(viaGate, routeFixes, { knownConcerns, commitLedger, log, g, gi })
 }
}

// Land the initial plan (fast-path enabled iff we pre-reviewed).
await landGroups(plan.groups, preReviewRan)

// 4.3.5 — Land-audit (integrity backstop for the fast-path). A SEPARATE
// verifier agent (not the committer) re-derives every landed commit's
// canonical diff_hash straight from git history and we cross-check it against
// the trusted `approved` set built during pre-review. Any landed commit whose
// hash was NOT pre-approved means the fast-path may have skipped review for an
// unreviewed diff → force a full review of that commit and record it loudly.
// The committer never produced this evidence, so it cannot fake the audit.
if (preReviewRan && commitLedger.length) {
  phase('Verify')
  const vr = await agent(
    [
      preamble('Verifier'),
      `Audit which commits just landed, for an integrity check. Do NOT commit or change anything.`,
      `From ${WORKTREE}, run exactly:`,
      `   python3 ~/.claude/review/pre_review.py --verify-range ${BASE} --repo-root ${WORKTREE}`,
      `It emits JSON { commits:[{sha, content_key}] } — the canonical content key of each ${BASE}..HEAD commit, computed by the same code the gate uses.`,
      `Return VERIFY_SCHEMA: commits[] of {sha, contentKey:content_key}, verbatim from that JSON.`,
    ].join('\n'),
    { agentType: 'general-purpose', label: 'verify:land', phase: 'Verify', schema: VERIFY_SCHEMA },
  )
  // Exclude commits pre-review intentionally left for the gate (big or blocked
  // groups it did NOT approve): those already went through the FULL gate review
  // once, so the audit must not force a redundant THIRD review — the exact
  // double-review SDD-002 targets. Keyed on content identity: if committed content
  // DRIFTED from what pre-review saw, the key won't match and the audit still
  // (correctly) flags it. KNOWN LIMITATION: a group BLOCKED in pre-review and fixed
  // LATE (after its final pre-review round) lands with a content key != its stale
  // pre-review key, so it is not excluded and gets one redundant audit review —
  // wasteful, not unsafe (no bad code lands). The clean fix is a real "did it
  // fast-path?" signal rather than "was this content pre-approved?"; out of scope.
  const expectedGateReviewed = new Set(
    lastPreReviewGroups.filter(g => !g.approved && g.contentKey).map(g => g.contentKey),
  )
  const unapproved = vr.commits.filter(c => !approved.has(c.contentKey) && !expectedGateReviewed.has(c.contentKey))
  if (unapproved.length) {
    for (const c of unapproved) {
      knownConcerns.push(`INTEGRITY: landed commit ${c.sha.slice(0, 9)} was not in the pre-reviewed approved set — its review provenance is unverified.`)
    }
    log(`Land-audit: ${unapproved.length} commit(s) not pre-approved → forcing a full review.`)
    // Force a full review of the unapproved commits and fold findings into concerns (autonomous: surface, don't halt).
    const audit = await agent(
      [
        preamble('Code-Reviewer'),
        `Integrity backstop: the following landed commits were NOT confirmed as pre-reviewed. Review each commit's full diff now and report any real defects.`,
        ...unapproved.map(c => `- commit ${c.sha}: inspect with "git show ${c.sha}" inside ${WORKTREE}`),
        `Base for context: ${BASE}. Read your role instructions: ~/.claude/agents/code-reviewer.md.`,
        `Return findings[] (id, severity, file, line, description) — empty if all clean.`,
      ].join('\n'),
      { agentType: 'Code-Reviewer', label: 'verify:audit-review', phase: 'Verify', schema: { type: 'object', additionalProperties: false, required: ['findings'], properties: { findings: { type: 'array', items: FINDING } } } },
    )
    for (const f of audit.findings || []) {
      knownConcerns.push(`Post-Land audit finding (${f.severity}) ${f.file || '(no file)'}:${f.line || ''} — ${f.description}`)
    }
  } else {
    log(`Land-audit: all ${vr.commits.length} landed commit(s) were pre-reviewed. ✓`)
  }
}

// 4.5 — Acceptance gate + in-run remediation. A final, HIGH-LEVEL check that
// the spec is actually, fully delivered (low-level quality was the reviewers'
// job). If not, fix the gaps and land the delta through the gate, then
// re-check — so the workflow finishes the job rather than shipping a partial
// implementation. NOT round-capped: every remediation delta lands through the
// full commit gate anyway, so productive rounds run unbounded. Termination is
// fingerprint-based (same as the review fix loop): a gap that survives
// STALL_ROUNDS consecutive rounds is retired to Known Concerns; the 1000-agent
// workflow ceiling is the ultimate backstop.
phase('Accept')

// Land whatever changes a remediation round left uncommitted, through the gate.
async function landDelta(tag) {
  const deltaPrompt = (extra) => [
    preamble('Commit planner'),
    `Remediation landing. Inspect the working tree in ${WORKTREE} (git status --porcelain, git diff --stat) for changes left UNCOMMITTED by the latest fixes.`,
    `Plan logical commits for ONLY those uncommitted changes — same rules as before: <=300 prod lines/group, conventional subjects prefixed "${TASK}".`,
    `VERTICAL SLICES, FILE-DISJOINT (hard rule): no file may appear in more than one group; shared files owned WHOLE by one group, never split.`,
    `If the working tree is clean (nothing to commit), return groups: []. Otherwise cover every uncommitted file exactly once.`,
    extra || '',
  ].join('\n')
  const dplan = await agent(
    deltaPrompt(''),
    { agentType: 'general-purpose', label: `accept:${tag}:plan`, phase: 'Accept', schema: DELTA_PLAN_SCHEMA },
  )
  if (!dplan.groups.length) return 0
  // Fix A also applies to the remediation delta — enforce disjointness here too.
  const groups = await validatePlanGroups(dplan.groups, `accept-${tag}`, async (overlaps) => {
    const r = await agent(
      deltaPrompt(overlapFixLines(overlaps)),
      { agentType: 'general-purpose', label: `accept:${tag}:replan`, phase: 'Accept', schema: DELTA_PLAN_SCHEMA },
    )
    return r.groups
  })
  if (groups.length) await landGroups(groups, false) // full gate on the (small) delta
  return groups.length
}

let accepted = false
const specGlob = `${TASKS_DIR}/5-review/ (the Scribe moved it there; if not found, check ${TASKS_DIR}/4-in-progress/)`
const acceptPersist = new Map() // gap fpKey -> consecutive rounds still open
const acceptGivenUp = new Set() // gap fpKeys retired to Known Concerns
let acceptRound = 0
while (true) {
  acceptRound++
  const acc = await agent(
    [
      preamble('Acceptance auditor'),
      `FINAL high-level acceptance check — decide whether the spec is ACTUALLY, FULLY implemented, not merely whether files exist. Low-level code quality was already reviewed; you take the whole-feature view.`,
      `1. Read the spec in full from ${specGlob}. Internalize the intent, every Acceptance Criterion, the Behavior/Examples, and the Work breakdown.`,
      `2. READ THE REAL CODE for this change in ${WORKTREE}: "git diff ${BASE}..HEAD --stat", then open and read the actual implementations (not just diffs). Judge end-to-end: does this deliver every AC and the intended feature?`,
      `3. Run the test suite and confirm it genuinely passes. Confirm every Work-breakdown file is created/changed and there are NO leftover TODO / stub / NotImplemented / placeholder.`,
      `Return complete=true ONLY if the feature is genuinely, fully done. Otherwise complete=false with gaps[] — each a concrete, actionable finding {severity, file, line, description, suggestedFix} — plus testsPass and a one-paragraph rationale.`,
    ].join('\n'),
    { agentType: 'general-purpose', label: `accept:round${acceptRound}`, phase: 'Accept', schema: ACCEPT_SCHEMA },
  )
  if (acc.complete && acc.testsPass) {
    accepted = true
    log(`Acceptance: spec fully implemented${acceptRound > 1 ? ` after ${acceptRound - 1} remediation round(s)` : ''}. ✓`)
    break
  }
  // Build this round's actionable gap set (failing tests fold in as a stable gap).
  let gaps = (acc.gaps || []).filter(g => g && g.description)
  if (!acc.testsPass) gaps.push({ id: 'accept-tests', severity: 'MUST_FIX', description: 'Acceptance: test suite is failing' })
  gaps = gaps.filter(g => !acceptGivenUp.has(fpKey(g)))

  // Fingerprint-based stall detection: retire gaps stuck for STALL_ROUNDS rounds.
  const curKeys = new Set(gaps.map(fpKey))
  for (const k of curKeys) acceptPersist.set(k, (acceptPersist.get(k) || 0) + 1)
  for (const k of Array.from(acceptPersist.keys())) if (!curKeys.has(k)) acceptPersist.delete(k)
  for (const g of gaps.filter(g => (acceptPersist.get(fpKey(g)) || 0) >= STALL_ROUNDS)) {
    acceptGivenUp.add(fpKey(g))
    knownConcerns.push(`Acceptance gap unresolved after ${STALL_ROUNDS} rounds (${g.severity}) ${g.file || '(no file)'}: ${g.description}`)
  }
  const toFix = gaps.filter(g => !acceptGivenUp.has(fpKey(g)))
  if (!toFix.length) {
    if (acc.gaps && acc.gaps.length === 0 && acc.testsPass) knownConcerns.push(`Acceptance reported incomplete but gave no actionable gaps: ${acc.rationale}`)
    log(`Acceptance: no further actionable gaps; stopping (${acceptGivenUp.size} retired to Known Concerns).`)
    break
  }
  log(`Acceptance round ${acceptRound}: ${toFix.length} gap(s)${acc.testsPass ? '' : ', tests failing'} → remediating.`)
  await routeFixes(toFix)
  await landDelta(`r${acceptRound}`)
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
  // Not accepted → INCOMPLETE (acceptance could not confirm full delivery even
  // after remediation). Otherwise concerns downgrade DELIVERED → _WITH_CONCERNS.
  status: !accepted ? 'DELIVERED_INCOMPLETE' : knownConcerns.length ? 'DELIVERED_WITH_CONCERNS' : 'DELIVERED',
  accepted,
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
