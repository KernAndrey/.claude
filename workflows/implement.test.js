// SDD-003 — framework-free control-flow test of the JS-held long-wait loop in
// workflows/implement.js (Testing Strategy layer b). Built on Node's built-in
// runner + assert ONLY — no new dependency, no package.json. Run with:
//   node --test workflows/implement.test.js
//
// SEAM (Seam B). implement.js is a top-level-await Workflow script: it has
// `export const meta` (ESM) AND a tail `return` (CJS-wrapper-only), so it loads
// via NEITHER require nor import — only the Workflow runtime's AsyncFunction
// wrap. This test re-creates that wrap: it reads the source, strips the `export `
// prefix, wraps the body in an AsyncFunction with the runtime globals stubbed and
// a `__IMPL_TEST__` flag set. implement.js bails early (before the first real
// agent() call) returning its hoisted helpers + bounds, so this exercises the
// REAL loop logic and stubs ONLY the agent spawner + the job-readiness signal
// (per the spec's mock boundary). Deterministic, idempotent, no real agents / no
// real review / no git.
//
// It does NOT (and CANNOT) prove the headline timing behavior (AC-1/2/4) — that
// is field-measured on a real >=30-min-gate run. It proves the control flow:
// null re-spawn (AC-3), the per-wait ceiling and the global-budget degrade
// (AC-7), NEEDS_MANIFEST re-entry without a double-launch (AC-6), and
// done-detection.

'use strict'

const test = require('node:test')
const assert = require('node:assert')
const fs = require('node:fs')
const path = require('node:path')

const ENGINE_PATH = path.join(__dirname, 'implement.js')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor

// Load the engine's helpers exactly as the Workflow runtime would: wrap the
// (export-stripped) source body in an AsyncFunction, inject stub globals and the
// test flag, and capture the early-bail return value. A fresh load per call
// gives each test an isolated module state (its own spawnAgent + poll counter).
async function loadEngine() {
  const src = fs.readFileSync(ENGINE_PATH, 'utf8').replace(/^export /m, '')
  // Minimal valid `args` so the early
  // `if (!WORKTREE || !SPEC || !CODERS.length || !TASKS_DIR)` guard passes; the
  // real engine body is never reached (we bail before it). specPath mirrors what
  // the lead really passes — a spec already moved into `{dir}/4-in-progress/`.
  const argsJson = JSON.stringify({
    worktreePath: '/tmp/sdd003-test',
    specPath: '/tmp/sdd003-test/tasks/4-in-progress/SDD-003-control-flow.md',
    baseBranch: 'dev',
    taskId: 'SDD-003',
    coders: [{ name: 'coder-1', scope: 'all', files: ['x'] }],
  })
  // Globals the engine references at top level: args (line ~32), and the
  // runtime-injected agent/phase/parallel/log. agent is stubbed to throw so a
  // bug that fails to bail (and reaches a real agent call) is loud, not silent.
  const fn = new AsyncFunction(
    'args', 'agent', 'phase', 'parallel', 'log', '__IMPL_TEST__',
    src,
  )
  const mod = await fn(
    argsJson,
    () => { throw new Error('real agent() must never be called in the control-flow test') },
    () => {},      // phase
    async () => [], // parallel
    () => {},      // log
    true,          // __IMPL_TEST__ — triggers the early bail
  )
  assert.ok(mod && typeof mod.awaitDetachedJob === 'function', 'engine must export awaitDetachedJob under __IMPL_TEST__')
  return mod
}

// A scripted spawner: each call returns the next entry from `script`. An entry of
// `null` simulates a terminal API error (agent() returns null). Records every
// call so a test can assert spawn count / no-double-fire.
function scriptedSpawner(script) {
  const calls = []
  const spawn = async (prompt, opts) => {
    calls.push({ prompt, opts })
    const i = calls.length - 1
    return i < script.length ? script[i] : { done: false }
  }
  spawn.calls = calls
  return spawn
}

// AC-3 — a null poll result re-spawns a fresh poll and the loop continues; the
// run does not abort. The job becomes ready only after the null cycle.
test('test_null_poll_respawns', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  // cycle 1: not ready; cycle 2: NULL (terminal service error); cycle 3: done.
  const spawn = scriptedSpawner([{ done: false }, null, { done: true }])
  mod.setSpawnAgent(spawn)

  const result = await mod.awaitDetachedJob('/tmp/out.json', {
    label: 'prereview:round1', phase: 'Pre-review', doneTest: 'test -e /tmp/out.json && echo READY || echo NOT_READY',
  })

  assert.strictEqual(result, true, 'wait resolves to done despite the null cycle')
  assert.strictEqual(spawn.calls.length, 3, 'the null cycle re-spawned a fresh poll (3 polls: not-ready, null, done)')
  // The null cycle still counts as a poll spawn against the budget.
  assert.strictEqual(mod.getPollCounter(), 3, 'all three poll spawns counted')
  // Every poll uses the minimal { done } schema and a per-cycle label.
  assert.deepStrictEqual(spawn.calls[0].opts.schema.required, ['done'])
  assert.strictEqual(spawn.calls[2].opts.label, 'prereview:round1:poll3')
})

// AC-7 (per-wait ceiling) — a job that never becomes ready stops at POLL_CEILING
// with the POLL_EXHAUSTED recorded-error sentinel (NOT an indefinite loop, NOT a
// throw-abort). The number of polls equals exactly POLL_CEILING.
test('test_ceiling_records_error', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  const spawn = scriptedSpawner([]) // every call defaults to { done: false }
  mod.setSpawnAgent(spawn)

  let threw = false
  let result
  try {
    result = await mod.awaitDetachedJob('/tmp/out.json', {
      label: 'prereview:round1', phase: 'Pre-review', doneTest: 'echo NOT_READY',
    })
  } catch (e) {
    threw = true
  }

  assert.strictEqual(threw, false, 'ceiling exhaustion does NOT throw-abort the workflow')
  assert.strictEqual(result, mod.POLL_EXHAUSTED, 'returns the POLL_EXHAUSTED recorded-error sentinel')
  assert.strictEqual(spawn.calls.length, mod.POLL_CEILING, 'spawned exactly POLL_CEILING poll agents, then stopped')
})

// AC-7 (global budget) — DISTINCT from the per-wait ceiling: when the module-level
// MAX_POLL_AGENTS budget is already (nearly) exhausted, a wait that would exceed it
// mid-poll ends via the SAME recorded-error degrade, even though POLL_CEILING for
// THIS wait was not reached. Primes the global counter to MAX_POLL_AGENTS-1 so the
// wait can take exactly one more poll before the budget bites.
test('test_global_budget_records_error', async () => {
  const mod = await loadEngine()
  // Prime the global counter to one below the budget.
  mod.setPollCounter(mod.MAX_POLL_AGENTS - 1)
  const spawn = scriptedSpawner([]) // never done
  mod.setSpawnAgent(spawn)

  const result = await mod.awaitDetachedJob('/tmp/out.json', {
    label: 'land:commit:1:1', phase: 'Land', doneTest: 'echo NOT_READY',
  })

  assert.strictEqual(result, mod.POLL_EXHAUSTED, 'global-budget exhaustion returns the recorded-error sentinel, not a throw')
  // Exactly ONE more poll fired (the last unit of budget), then the budget bit on
  // the next cycle — well under POLL_CEILING, proving this is the global path.
  assert.strictEqual(spawn.calls.length, 1, 'only the remaining single budget unit was spent before degrading')
  assert.strictEqual(mod.getPollCounter(), mod.MAX_POLL_AGENTS, 'global poll counter stopped exactly at the budget')
  assert.ok(spawn.calls.length < mod.POLL_CEILING, 'this is the global-budget path, NOT the per-wait ceiling')
})

// done-detection — a complete result on a given cycle is recognized as done and
// awaitDetachedJob resolves to true (which is what triggers the collect step at
// the call site). This asserts the boolean resolution only, not collect firing.
test('test_awaitDetachedJob_done_resolves_to_true', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  // not ready twice, then ready.
  const spawn = scriptedSpawner([{ done: false }, { done: false }, { done: true }])
  mod.setSpawnAgent(spawn)

  const result = await mod.awaitDetachedJob('/tmp/out.json', {
    label: 'prereview:round1', phase: 'Pre-review', doneTest: 'test -e /tmp/out.json && echo READY || echo NOT_READY',
  })

  assert.strictEqual(result, true, 'a complete result is recognized as done')
  assert.strictEqual(spawn.calls.length, 3, 'polled until the ready cycle and stopped (no extra polls after done)')
})

// AC-6 — a NEEDS_MANIFEST round re-enters the wait loop and converges: the handle
// authors manifests + relaunches --pending ITSELF (branch A {needAwait:true}), so
// the JS re-enters `await` with NO new launch; once no group is NEEDS_MANIFEST the
// handle returns branch B and the loop exits. This models the canonical MIXED round
// (group 0 = NEEDS_MANIFEST, groups 1,2 already approved in the full --reset review):
// branch B must return the FULL three-group set, NOT just the relaunched subset.
// Asserts: launch fired EXACTLY ONCE (the handle relaunch is NOT double-fired by the
// JS — Risk 6), the sub-loop ran once, and the full set survives the relaunch.
//
// NOTE: the full-set MERGE itself (the handle reading+merging+writing the on-disk
// accumulator) is agent disk-I/O OUTSIDE this stub boundary — it is validated in the
// field (D7c/D7d), not unit-proven here. This test models a CORRECT handle and proves
// the JS control flow propagates whatever full set the handle returns; it does not (and
// cannot) prove the handle's merge logic. The stub returns the full set directly.
test('test_needs_manifest_reentry', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()

  let launchCalls = 0
  const launch = async () => { launchCalls++; return { done: true } }

  // Two readiness checks (one per await), each ready on the first poll, so the
  // poll spawner returns done immediately each cycle.
  const pollSpawn = scriptedSpawner([{ done: true }, { done: true }])
  mod.setSpawnAgent(pollSpawn)

  let handleCalls = 0
  const handle = async () => {
    handleCalls++
    // First handle: branch A (group 0 needs a manifest → authored + relaunched
    // --pending 0 by the handle itself; groups 1,2 already merged into the accum).
    if (handleCalls === 1) return { needAwait: true }
    // Second handle: branch B — the converged FULL set (all three groups), as a
    // correct accumulator-backed handle would return after merging the --pending 0
    // result over the prior full-set state.
    return {
      groups: [
        { index: 0, contentKey: 'k0', verdict: 'OK', approved: true },
        { index: 1, contentKey: 'k1', verdict: 'OK', approved: true },
        { index: 2, contentKey: 'k2', verdict: 'OK', approved: true },
      ],
      wholediff: { verdict: 'OK' },
      allClean: true,
    }
  }

  const pr = await mod.runPreReviewWait({
    launch, handle,
    outFile: '/tmp/prereview-out.json',
    doneTest: 'test -e /tmp/prereview-out.json && echo READY || echo NOT_READY',
    label: 'prereview:round1', phaseName: 'Pre-review',
  })

  assert.strictEqual(launchCalls, 1, 'launch fired EXACTLY once — the handle relaunch is NOT double-fired by the JS (Risk 6)')
  assert.strictEqual(handleCalls, 2, 'the NEEDS_MANIFEST sub-loop ran once (branch A), then converged (branch B)')
  assert.ok(pr && pr.allClean === true, 'returns the converged branch-B full-set result')
  assert.strictEqual(pr.groups.length, 3, 'the FULL three-group set survives the within-round --pending relaunch (groups 1,2 not dropped)')
  assert.deepStrictEqual(pr.groups.map(g => g.index), [0, 1, 2], 'all original group indices present after convergence')
})

// AC-6 (handle null-safety) — a null handle return re-spawns the handle and the
// loop continues; the handle's own already-running guard makes the re-spawn safe
// (no double-launch). The JS must NOT relaunch on a null handle.
test('test_null_handle_respawns_without_relaunch', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()

  let launchCalls = 0
  const launch = async () => { launchCalls++; return { done: true } }
  mod.setSpawnAgent(scriptedSpawner([{ done: true }]))

  let handleCalls = 0
  const handle = async () => {
    handleCalls++
    if (handleCalls === 1) return null // terminal service error on the handle
    return { groups: [], wholediff: { verdict: 'OK' }, allClean: true }
  }

  const pr = await mod.runPreReviewWait({
    launch, handle,
    outFile: '/tmp/prereview-out.json',
    doneTest: 'echo READY',
    label: 'prereview:round1', phaseName: 'Pre-review',
  })

  assert.strictEqual(launchCalls, 1, 'a null handle does NOT trigger a JS relaunch (only the handle re-spawns)')
  assert.strictEqual(handleCalls, 2, 'the null handle was re-spawned once, then succeeded')
  assert.ok(pr && pr.allClean === true)
})

// AC-3 (pre-review launch null-safety) — a null pre-review LAUNCH return (terminal
// service error) re-spawns the launch, NOT just the poll/handle; the launch's own
// pgrep guard makes the re-spawn idempotent. Without this, a null launch would mean
// the detached job never started and the wait would burn the full ceiling before
// degrading. Mirrors test_committer_triple_null_safe for the launch site.
test('test_null_prereview_launch_respawns', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  mod.setSpawnAgent(scriptedSpawner([{ done: true }])) // wait resolves on first poll

  let launchCalls = 0
  const launch = async () => {
    launchCalls++
    if (launchCalls === 1) return null // terminal service error on launch → re-spawn
    return { done: true }
  }
  const handle = async () => ({ groups: [], wholediff: { verdict: 'OK' }, allClean: true })

  const pr = await mod.runPreReviewWait({
    launch, handle,
    outFile: '/tmp/prereview-out.json',
    doneTest: 'echo READY',
    label: 'prereview:round1', phaseName: 'Pre-review',
  })

  assert.strictEqual(launchCalls, 2, 'a null pre-review launch was re-spawned (null-safety on the launch site, not just poll/handle)')
  assert.ok(pr && pr.allClean === true, 'the wait proceeds normally after the launch re-spawn')
})

// AC-5 (committer triple, every attempt JS-held) — a null launch re-spawns (the
// idempotency guard makes it safe), the wait is JS-held, and a null collect
// re-spawns. The full triple resolves to the collect's COMMIT_RESULT.
test('test_committer_triple_null_safe', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  mod.setSpawnAgent(scriptedSpawner([{ done: true }])) // wait resolves on first poll

  let launchCalls = 0
  const launch = async () => {
    launchCalls++
    if (launchCalls === 1) return null // terminal service error on launch → re-spawn
    return { started: true, outFile: '/bg/commit-out.txt', doneTest: 'echo READY' }
  }
  let collectCalls = 0
  const collect = async (outFile) => {
    collectCalls++
    assert.strictEqual(outFile, '/bg/commit-out.txt', 'collect reads the launch-RETURNED bg path')
    if (collectCalls === 1) return null // terminal service error on collect → re-spawn
    return { landed: true, verdict: 'CLEAN', shaSummary: 'abc1234 SDD-003: x', blockingFindings: [], warnings: [], overridesAdded: [], knownConcerns: [] }
  }

  const cr = await mod.landGroupViaGate({ launch, collect, label: 'land:commit:1:1', phaseName: 'Land' })

  assert.strictEqual(launchCalls, 2, 'null launch re-spawned (idempotency guard makes it safe)')
  assert.strictEqual(collectCalls, 2, 'null collect re-spawned')
  assert.strictEqual(cr.landed, true, 'the triple resolves to the collect COMMIT_RESULT')
})

// AC-5 (idempotency short-circuit) — when the launch detects the group already
// landed, it short-circuits and the wait + collect are skipped entirely.
test('test_committer_already_landed_shortcircuit', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  // If the poll spawner is ever called, that means we wrongly entered the wait.
  mod.setSpawnAgent(async () => { throw new Error('must not poll when launch short-circuits as alreadyLanded') })

  const alreadyResult = { landed: true, verdict: 'CLEAN', shaSummary: 'def5678 SDD-003: y', blockingFindings: [], warnings: [], overridesAdded: [], knownConcerns: [] }
  let collectCalls = 0
  const launch = async () => ({ alreadyLanded: true, result: alreadyResult })
  const collect = async () => { collectCalls++; return alreadyResult }

  const cr = await mod.landGroupViaGate({ launch, collect, label: 'land:commit:1:1', phaseName: 'Land' })

  assert.strictEqual(cr, alreadyResult, 'returns the already-landed result without entering the wait')
  assert.strictEqual(collectCalls, 0, 'collect is skipped on the idempotency short-circuit')
  // The launch itself is ONE budget-charged spawn (spawnWithBudget, MF-B), but the
  // short-circuit skips the wait entirely → NO poll agents fire (the throwing poll
  // stub above would have thrown if a poll ran). The counter reflects exactly the
  // one launch attempt, not any poll.
  assert.strictEqual(mod.getPollCounter(), 1, 'only the single launch spawn was charged; no poll agents spawned (the wait was skipped)')
})

// Schema sanity — both SDD-003 schemas are FLAT plain objects (NO top-level oneOf:
// the Anthropic API rejects a top-level oneOf in an agent tool input_schema; the
// discrimination moved into the JS, which branches on the field VALUES). Asserts the
// flat shape + the expected optional properties. COMMIT_RESULT_SCHEMA still carries
// the optional needRelaunch (D9). Static-shape checks only.
test('test_schema_shapes', async () => {
  const mod = await loadEngine()

  // HANDLE_PREREVIEW_SCHEMA — flat object, NO oneOf, all fields optional.
  const h = mod.HANDLE_PREREVIEW_SCHEMA
  assert.strictEqual(h.type, 'object', 'HANDLE_PREREVIEW_SCHEMA is a plain object')
  assert.ok(!('oneOf' in h) && !('anyOf' in h) && !('allOf' in h), 'HANDLE_PREREVIEW_SCHEMA has NO top-level oneOf/anyOf/allOf (API-valid)')
  assert.strictEqual(h.additionalProperties, false)
  for (const k of ['needAwait', 'groups', 'wholediff', 'allClean']) {
    assert.ok(k in h.properties, `HANDLE_PREREVIEW_SCHEMA exposes the optional ${k} property`)
  }
  assert.ok(!Array.isArray(h.required) || h.required.length === 0, 'HANDLE_PREREVIEW_SCHEMA requires no field (JS discriminates on values)')

  // COMMIT_RESULT_SCHEMA — optional needRelaunch (D9).
  assert.ok('needRelaunch' in mod.COMMIT_RESULT_SCHEMA.properties, 'COMMIT_RESULT_SCHEMA has the optional needRelaunch (D9)')
  assert.ok(!mod.COMMIT_RESULT_SCHEMA.required.includes('needRelaunch'), 'needRelaunch is OPTIONAL (not required)')

  // LAND_LAUNCH_SCHEMA — flat object, NO oneOf, all branch fields optional.
  const ll = mod.LAND_LAUNCH_SCHEMA
  assert.strictEqual(ll.type, 'object', 'LAND_LAUNCH_SCHEMA is a plain object')
  assert.ok(!('oneOf' in ll) && !('anyOf' in ll) && !('allOf' in ll), 'LAND_LAUNCH_SCHEMA has NO top-level oneOf/anyOf/allOf (API-valid)')
  assert.strictEqual(ll.additionalProperties, false)
  for (const k of ['alreadyLanded', 'result', 'started', 'outFile', 'doneTest']) {
    assert.ok(k in ll.properties, `LAND_LAUNCH_SCHEMA exposes the optional ${k} property`)
  }
  assert.ok(!Array.isArray(ll.required) || ll.required.length === 0, 'LAND_LAUNCH_SCHEMA requires no field (JS discriminates on values)')
})

// REGRESSION (the bug class the D7c smoke caught) — EVERY agent-facing tool
// input_schema must be API-valid: top-level type:'object' and NO top-level
// oneOf/anyOf/allOf (the Anthropic API returns 400 "input_schema does not support
// oneOf, allOf, or anyOf at the top level" otherwise — adding type:'object' does
// NOT help). This iterates the full schema set so a future schema that reintroduces
// a top-level combinator is caught here, before it 400s every agent of that role.
test('test_agent_schemas_are_api_valid', async () => {
  const mod = await loadEngine()
  const schemas = mod.allSchemas
  assert.ok(schemas && Object.keys(schemas).length >= 15, 'allSchemas exposes every agent-facing schema')
  for (const [name, s] of Object.entries(schemas)) {
    assert.strictEqual(s.type, 'object', `${name} must have top-level type:'object' (API requirement)`)
    assert.ok(!('oneOf' in s), `${name} must NOT have a top-level oneOf (API 400s on it)`)
    assert.ok(!('anyOf' in s), `${name} must NOT have a top-level anyOf (API 400s on it)`)
    assert.ok(!('allOf' in s), `${name} must NOT have a top-level allOf (API 400s on it)`)
  }
})

// MF-A — the alreadyLanded idempotency short-circuit returns a VALID COMMIT_RESULT
// (never undefined). landGroupViaGate must short-circuit only when a usable result
// is present, so the caller can safely read cr.knownConcerns etc.
test('test_landlaunch_alreadyLanded_guard', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  // poll must never run: a short-circuit skips the wait entirely.
  mod.setSpawnAgent(async () => { throw new Error('must not poll on the alreadyLanded short-circuit') })

  const landedResult = { landed: true, verdict: 'CLEAN', shaSummary: 'abc1234 SDD-003: x', blockingFindings: [], warnings: [], overridesAdded: [], knownConcerns: [] }
  const launch = async () => ({ alreadyLanded: true, result: landedResult })
  const collect = async () => { throw new Error('must not collect on the short-circuit') }

  const cr = await mod.landGroupViaGate({ launch, collect, label: 'land:commit:1:1', phaseName: 'Land' })
  assert.strictEqual(cr, landedResult, 'returns the carried COMMIT_RESULT, not undefined')
  assert.strictEqual(cr.landed, true)
  assert.ok(Array.isArray(cr.knownConcerns), 'caller can safely read cr.knownConcerns (no undefined crash)')
})

// MF-B — a SUSTAINED null (service outage) on a launch/collect/handle spawn must
// NOT spin: spawnWithBudget caps retries and returns POLL_EXHAUSTED, so the run
// degrades early instead of grinding to the 1000-agent cap. Asserts the bound is
// respected at the launch site, the collect site, AND directly on spawnWithBudget.
test('test_spawn_budget_ceiling', async () => {
  const mod = await loadEngine()

  // (a) launch returns null forever → landGroupViaGate degrades after SPAWN_RETRIES.
  mod.resetPollCounter()
  let launchCalls = 0
  const nullLaunch = async () => { launchCalls++; return null }
  const collectNever = async () => { throw new Error('collect must not be reached when launch never succeeds') }
  const crLaunch = await mod.landGroupViaGate({ launch: nullLaunch, collect: collectNever, label: 'land:commit:1:1', phaseName: 'Land' })
  assert.strictEqual(crLaunch, mod.POLL_EXHAUSTED, 'sustained null launch degrades to POLL_EXHAUSTED (no infinite spin)')
  assert.strictEqual(launchCalls, mod.SPAWN_RETRIES, `launch retried exactly SPAWN_RETRIES (${mod.SPAWN_RETRIES}) times, then stopped`)

  // (b) launch ok, collect returns null forever → degrades after SPAWN_RETRIES.
  mod.resetPollCounter()
  mod.setSpawnAgent(scriptedSpawner([{ done: true }])) // wait resolves immediately
  let collectCalls = 0
  const okLaunch = async () => ({ started: true, outFile: '/bg/o.txt', doneTest: 'echo READY' })
  const nullCollect = async () => { collectCalls++; return null }
  const crCollect = await mod.landGroupViaGate({ launch: okLaunch, collect: nullCollect, label: 'land:commit:1:1', phaseName: 'Land' })
  assert.strictEqual(crCollect, mod.POLL_EXHAUSTED, 'sustained null collect degrades to POLL_EXHAUSTED')
  assert.strictEqual(collectCalls, mod.SPAWN_RETRIES, 'collect retried exactly SPAWN_RETRIES times')

  // (c) spawnWithBudget directly: a null-forever fn stops at maxRetries and counts.
  mod.resetPollCounter()
  let n = 0
  const r = await mod.spawnWithBudget(async () => { n++; return null }, 3)
  assert.strictEqual(r, mod.POLL_EXHAUSTED, 'spawnWithBudget returns POLL_EXHAUSTED on sustained null')
  assert.strictEqual(n, 3, 'respected the maxRetries cap')
  assert.strictEqual(mod.getPollCounter(), 3, 'each retry charged against the global budget')

  // (d) global budget bites mid-retry even below maxRetries.
  mod.setPollCounter(mod.MAX_POLL_AGENTS)
  let m = 0
  const r2 = await mod.spawnWithBudget(async () => { m++; return null }, 5)
  assert.strictEqual(r2, mod.POLL_EXHAUSTED, 'exhausted global budget degrades immediately')
  assert.strictEqual(m, 0, 'no spawn attempted once the global budget is gone')
})

// MF-B/MF-C control flow — landGroupLoop drives the committer multi-cycle loop:
// the needRelaunch re-entry re-runs the full triple each cycle, then lands.
test('test_committer_needrelaunch_loop', async () => {
  const mod = await loadEngine()
  const knownConcerns = []
  const commitLedger = []
  // cycle 1,2: in-gate fix → needRelaunch; cycle 3: landed.
  let cycle = 0
  const viaGate = async (gateRound) => {
    cycle++
    assert.strictEqual(gateRound, cycle, 'viaGate is called once per cycle with the incrementing gateRound (full triple re-runs)')
    if (cycle < 3) return { landed: false, needRelaunch: true, verdict: 'BLOCKED', blockingFindings: [], warnings: [], overridesAdded: [], knownConcerns: [] }
    return { landed: true, verdict: 'CLEAN', shaSummary: 'abc1234 SDD-003: x', blockingFindings: [], warnings: [], overridesAdded: [], knownConcerns: [] }
  }
  let routeCalls = 0
  const routeFixes = async () => { routeCalls++; return [] }

  await mod.landGroupLoop(viaGate, routeFixes, { knownConcerns, commitLedger, log: () => {}, g: { message: 'feat(SDD-003): x', files: ['a'] }, gi: 0 })

  assert.strictEqual(cycle, 3, 'the full launch->await->collect triple re-ran each needRelaunch cycle, then landed')
  assert.strictEqual(commitLedger.length, 1, 'the commit landed (one ledger entry)')
  assert.strictEqual(routeCalls, 0, 'needRelaunch does NOT route fixes (the collect agent did the local fix)')
})

// MF-C — a gate that NEVER converges on the needRelaunch path must stop at the
// stall cap with a Known Concern, NOT spin toward the 1000-agent cap.
test('test_committer_needrelaunch_stall', async () => {
  const mod = await loadEngine()
  const knownConcerns = []
  const commitLedger = []
  let cycle = 0
  // collect always returns needRelaunch:true — a gate that keeps rejecting the fix.
  const viaGate = async () => { cycle++; return { landed: false, needRelaunch: true, verdict: 'BLOCKED', blockingFindings: [], warnings: [], overridesAdded: [], knownConcerns: [] } }
  const routeFixes = async () => []

  await mod.landGroupLoop(viaGate, routeFixes, { knownConcerns, commitLedger, log: () => {}, g: { message: 'feat(SDD-003): runaway', files: ['a'] }, gi: 0 })

  // Cap is MAX_NEEDRELAUNCH_ROUNDS consecutive relaunches, then break. The loop
  // runs the cap-th relaunch (streak == cap) and breaks on the next (streak > cap),
  // so total cycles = cap + 1 — finite, NOT a runaway.
  assert.strictEqual(cycle, mod.MAX_NEEDRELAUNCH_ROUNDS + 1, `stopped at the needRelaunch cap (${mod.MAX_NEEDRELAUNCH_ROUNDS}), not a runaway`)
  assert.strictEqual(commitLedger.length, 0, 'nothing landed')
  assert.ok(knownConcerns.some(c => /did not converge/.test(c)), 'recorded a Known Concern about non-convergence')
  // The cap is comfortably ABOVE the documented 10-cycle worst case (AC-5).
  assert.ok(mod.MAX_NEEDRELAUNCH_ROUNDS >= 10, 'the cap admits the real >=10-cycle commit (AC-5 Scenario B)')
})

// ---------------------------------------------------------------------------
// POLL_EXHAUSTED forwarding/degrade through the wrappers (C2-C6). The bound is
// tested in isolation on awaitDetachedJob elsewhere; these prove it PROPAGATES
// through runPreReviewWait / landGroupViaGate / landGroupLoop so a hung gate
// degrades-and-continues rather than spinning or aborting.
// ---------------------------------------------------------------------------

// C2 — runPreReviewWait forwards an exhausted LAUNCH (implement.js launch site).
// spawnWithBudget returns the POLL_EXHAUSTED sentinel (it is truthy), so the
// `lr === POLL_EXHAUSTED` guard fires before any await/handle runs.
test('test_prereview_launch_exhausted_forwards', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  // poll must never run — exhaustion happens at the launch, before the wait.
  mod.setSpawnAgent(async () => { throw new Error('must not poll: launch exhausted before the wait') })

  let handleCalls = 0
  const launch = async () => mod.POLL_EXHAUSTED // launch site degrades
  const handle = async () => { handleCalls++; return { groups: [], wholediff: { verdict: 'OK' }, allClean: true } }

  const pr = await mod.runPreReviewWait({
    launch, handle, outFile: '/tmp/o.json', doneTest: 'echo READY',
    label: 'prereview:round1', phaseName: 'Pre-review',
  })

  assert.strictEqual(pr, mod.POLL_EXHAUSTED, 'an exhausted launch is forwarded as POLL_EXHAUSTED')
  assert.strictEqual(handleCalls, 0, 'the handle never runs once the launch degraded')
})

// C3 — runPreReviewWait forwards an exhausted POLL WAIT (awaitDetachedJob hits
// POLL_CEILING inside the wrapper). Launch succeeds; the poll spawner never
// reports done → 24 cycles → POLL_EXHAUSTED, forwarded before the handle runs.
test('test_prereview_poll_wait_exhausted_forwards', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  const pollSpawn = scriptedSpawner([]) // always { done: false } → drives to POLL_CEILING
  mod.setSpawnAgent(pollSpawn)

  let handleCalls = 0
  const launch = async () => ({ done: true })
  const handle = async () => { handleCalls++; return { groups: [], wholediff: { verdict: 'OK' }, allClean: true } }

  const pr = await mod.runPreReviewWait({
    launch, handle, outFile: '/tmp/o.json', doneTest: 'echo NOT_READY',
    label: 'prereview:round1', phaseName: 'Pre-review',
  })

  assert.strictEqual(pr, mod.POLL_EXHAUSTED, 'an exhausted poll wait is forwarded as POLL_EXHAUSTED')
  assert.strictEqual(pollSpawn.calls.length, mod.POLL_CEILING, 'the wait stopped at POLL_CEILING (no infinite spin)')
  assert.strictEqual(handleCalls, 0, 'the handle never runs once the wait degraded')
})

// C4 — runPreReviewWait forwards an exhausted HANDLE (implement.js handle site).
// Launch + poll succeed; the handle's spawnWithBudget degrades → forwarded.
test('test_prereview_handle_exhausted_forwards', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  mod.setSpawnAgent(scriptedSpawner([{ done: true }])) // wait resolves on the first poll

  const launch = async () => ({ done: true })
  const handle = async () => mod.POLL_EXHAUSTED // handle site degrades

  const pr = await mod.runPreReviewWait({
    launch, handle, outFile: '/tmp/o.json', doneTest: 'echo READY',
    label: 'prereview:round1', phaseName: 'Pre-review',
  })

  assert.strictEqual(pr, mod.POLL_EXHAUSTED, 'an exhausted handle is forwarded as POLL_EXHAUSTED')
})

// C5 — landGroupViaGate forwards an exhausted POLL WAIT (its awaitDetachedJob hits
// POLL_CEILING). Launch returns a valid bg path; the poll never reports done →
// POLL_EXHAUSTED, forwarded before collect runs.
test('test_landgroupviagate_wait_exhausted_forwards', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  const pollSpawn = scriptedSpawner([]) // always { done: false } → POLL_CEILING
  mod.setSpawnAgent(pollSpawn)

  let collectCalls = 0
  const launch = async () => ({ started: true, outFile: '/bg/o.txt', doneTest: 'echo NOT_READY' })
  const collect = async () => { collectCalls++; return { landed: true, verdict: 'CLEAN', shaSummary: 'x', blockingFindings: [], warnings: [], overridesAdded: [], knownConcerns: [] } }

  const cr = await mod.landGroupViaGate({ launch, collect, label: 'land:commit:1:1', phaseName: 'Land' })

  assert.strictEqual(cr, mod.POLL_EXHAUSTED, 'an exhausted committer wait is forwarded as POLL_EXHAUSTED')
  assert.strictEqual(pollSpawn.calls.length, mod.POLL_CEILING, 'the wait stopped at POLL_CEILING')
  assert.strictEqual(collectCalls, 0, 'collect never runs once the wait degraded')
})

// C6 — landGroupLoop degrades on an exhausted viaGate: it pushes a Known Concern
// and BREAKS (does not spin, does not throw, nothing lands).
test('test_landgrouploop_exhausted_degrades', async () => {
  const mod = await loadEngine()
  const knownConcerns = []
  const commitLedger = []
  let cycle = 0
  const viaGate = async () => { cycle++; return mod.POLL_EXHAUSTED }
  const routeFixes = async () => { throw new Error('must not routeFixes on an exhausted gate') }

  await mod.landGroupLoop(viaGate, routeFixes, { knownConcerns, commitLedger, log: () => {}, g: { message: 'feat(SDD-003): x', files: ['a'] }, gi: 0 })

  assert.strictEqual(cycle, 1, 'viaGate ran once, then the loop broke (no spin)')
  assert.strictEqual(commitLedger.length, 0, 'nothing landed')
  assert.ok(knownConcerns.some(c => /budget exhausted/.test(c)), 'recorded a Known Concern about the exhausted gate')
})

// C7 — landGroupLoop resets the needRelaunch streak on a productive blockingFindings
// round, so a legitimate long MIXED run (relaunches interleaved with routed fixes)
// is NOT falsely capped at MAX_NEEDRELAUNCH_ROUNDS. The pattern below has more total
// needRelaunch rounds than the cap, but never more than the cap CONSECUTIVELY.
test('test_landgrouploop_blockingfindings_resets_streak', async () => {
  const mod = await loadEngine()
  const knownConcerns = []
  const commitLedger = []
  let routeCalls = 0
  const routeFixes = async () => { routeCalls++; return [] }

  // Build a script: (cap-1) needRelaunch, then a blockingFindings round (resets),
  // then (cap-1) needRelaunch again, then landed. Total needRelaunch = 2*(cap-1)
  // > cap, but the max CONSECUTIVE streak is cap-1 < cap, so it must NOT cap out.
  const cap = mod.MAX_NEEDRELAUNCH_ROUNDS
  const script = []
  const relaunch = { landed: false, needRelaunch: true, verdict: 'BLOCKED', blockingFindings: [], warnings: [], overridesAdded: [], knownConcerns: [] }
  const blocking = { landed: false, verdict: 'BLOCKED', blockingFindings: [{ id: 'F1', severity: 'CRITICAL', file: 'a.py', description: 'x' }], warnings: [], overridesAdded: [], knownConcerns: [] }
  const land = { landed: true, verdict: 'CLEAN', shaSummary: 'abc1234 SDD-003: x', blockingFindings: [], warnings: [], overridesAdded: [], knownConcerns: [] }
  for (let i = 0; i < cap - 1; i++) script.push(relaunch)
  script.push(blocking)
  for (let i = 0; i < cap - 1; i++) script.push(relaunch)
  script.push(land)

  let cycle = 0
  const viaGate = async () => script[cycle++]

  await mod.landGroupLoop(viaGate, routeFixes, { knownConcerns, commitLedger, log: () => {}, g: { message: 'feat(SDD-003): long mixed', files: ['a'] }, gi: 0 })

  assert.strictEqual(cycle, script.length, 'every scripted round ran — the blockingFindings round RESET the streak so the cap was never hit')
  assert.strictEqual(commitLedger.length, 1, 'the commit landed despite 2*(cap-1) total relaunch rounds')
  assert.strictEqual(routeCalls, 1, 'the single blockingFindings round routed fixes once')
  assert.ok(!knownConcerns.some(c => /did not converge/.test(c)), 'NOT falsely capped — no non-convergence concern recorded')
})

// Adjacent — landGroupViaGate degrades an EXHAUSTED LAUNCH (sustained null/Symbol at
// the launch site) to POLL_EXHAUSTED before touching the wait. Mirrors C2 for the
// committer triple (the launch-exhaustion arm distinct from the wait arm in C5).
test('test_landgroupviagate_launch_exhausted_forwards', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  mod.setSpawnAgent(async () => { throw new Error('must not poll: launch exhausted before the wait') })
  const launch = async () => mod.POLL_EXHAUSTED
  const collect = async () => { throw new Error('must not collect once the launch degraded') }

  const cr = await mod.landGroupViaGate({ launch, collect, label: 'land:commit:1:1', phaseName: 'Land' })
  assert.strictEqual(cr, mod.POLL_EXHAUSTED, 'an exhausted committer launch is forwarded as POLL_EXHAUSTED')
})

// Adjacent — landGroupViaGate's alreadyLanded guard degrades a MALFORMED short-circuit
// (alreadyLanded with no result) to POLL_EXHAUSTED rather than entering the wait with
// an undefined outFile. (Unreachable under LAND_LAUNCH_SCHEMA's oneOf, but the guard
// must degrade cleanly if a malformed launch ever slips through.)
test('test_landgroupviagate_alreadyLanded_no_result_degrades', async () => {
  const mod = await loadEngine()
  mod.resetPollCounter()
  mod.setSpawnAgent(async () => { throw new Error('must not poll: malformed alreadyLanded must degrade, not wait') })
  const launch = async () => ({ alreadyLanded: true }) // no result (malformed)
  const collect = async () => { throw new Error('must not collect on a malformed short-circuit') }

  const cr = await mod.landGroupViaGate({ launch, collect, label: 'land:commit:1:1', phaseName: 'Land' })
  assert.strictEqual(cr, mod.POLL_EXHAUSTED, 'a malformed alreadyLanded (no result) degrades to POLL_EXHAUSTED, never an undefined-outFile wait')
})

// Adjacent — landGroupLoop's blockingFindings STALL backstop: the SAME finding
// persisting STALL_ROUNDS rounds (routed each time, never resolved) stops retrying
// with a Known Concern instead of spinning. Distinct from the needRelaunch cap.
test('test_landgrouploop_blockingfindings_stall', async () => {
  const mod = await loadEngine()
  const knownConcerns = []
  const commitLedger = []
  const F = [{ id: 'F1', severity: 'CRITICAL', file: 'a.py', description: 'x' }]
  let cycle = 0
  // The SAME blocking fingerprint every round → gatePersist climbs to STALL_ROUNDS.
  const viaGate = async () => { cycle++; return { landed: false, verdict: 'BLOCKED', blockingFindings: F, warnings: [], overridesAdded: [], knownConcerns: [] } }
  let routeCalls = 0
  const routeFixes = async () => { routeCalls++; return [] }

  await mod.landGroupLoop(viaGate, routeFixes, { knownConcerns, commitLedger, log: () => {}, g: { message: 'feat(SDD-003): stuck', files: ['a'] }, gi: 0 })

  assert.strictEqual(cycle, 3, 'broke at STALL_ROUNDS (3) rounds of the same finding — not a runaway')
  assert.strictEqual(routeCalls, 2, 'routed fixes on rounds 1-2; round 3 hit the STALL break before routing')
  assert.strictEqual(commitLedger.length, 0, 'nothing landed')
  assert.ok(knownConcerns.some(c => /did not pass the gate after \d+ rounds/.test(c)), 'recorded the STALL Known Concern')
})

// Adjacent — landGroupLoop's verdict-ERROR else branch: a {landed:false} with NO
// needRelaunch and NO blockingFindings (e.g. preflight crash, exit 3) records a
// Known Concern and breaks immediately — it does not retry or route.
test('test_landgrouploop_verdict_error_breaks', async () => {
  const mod = await loadEngine()
  const knownConcerns = []
  const commitLedger = []
  let cycle = 0
  const viaGate = async () => { cycle++; return { landed: false, verdict: 'ERROR', blockingFindings: [], warnings: [], overridesAdded: [], knownConcerns: [] } }
  const routeFixes = async () => { throw new Error('must not route on a verdict-ERROR with no findings') }

  await mod.landGroupLoop(viaGate, routeFixes, { knownConcerns, commitLedger, log: () => {}, g: { message: 'feat(SDD-003): errored', files: ['a'] }, gi: 0 })

  assert.strictEqual(cycle, 1, 'broke on the first verdict-ERROR round (no retry)')
  assert.strictEqual(commitLedger.length, 0, 'nothing landed')
  assert.ok(knownConcerns.some(c => /could not be landed automatically/.test(c)), 'recorded the verdict-ERROR Known Concern')
})

// Note: the committer launch deliberately has NO in-flight-commit guard (an
// earlier version's in-flight branch falsely returned landed:true for a not-yet-
// finished commit → silent drop; removed). Only a genuinely ALREADY-LANDED commit
// returns the short-circuit, covered by test_committer_already_landed_shortcircuit.

// C3 — the pre-review MAIN-LOOP exhaustion degrade (preReviewDegrade), extracted
// symmetric with landGroupLoop's tested degrade. Covers the detection + Known-Concern
// recording (the meaningful part); the bare `break` at the call site stays inline,
// identical to landGroupLoop's pattern already validated by test_landgrouploop_exhausted_degrades.
test('test_prereview_mainloop_exhausted_degrades', async () => {
  const mod = await loadEngine()

  // POLL_EXHAUSTED pr → records a Known Concern naming the wait + round, returns true
  // (caller breaks the round loop, leaving pending groups to the live gate).
  const kc1 = []
  const degraded = mod.preReviewDegrade(mod.POLL_EXHAUSTED, 4, 2, kc1)
  assert.strictEqual(degraded, true, 'an exhausted pre-review wait degrades (returns true → caller breaks)')
  assert.strictEqual(kc1.length, 1, 'recorded exactly one Known Concern')
  assert.ok(/pre-review: job did not finish within the poll ceiling \(round 4\)/.test(kc1[0]), 'the concern names the wait and round')
  assert.ok(/2 group\(s\) will go through the live commit gate/.test(kc1[0]), 'the concern states the degrade (pending groups → live gate)')

  // A normal (non-exhausted) pr → returns false, records nothing (loop continues to
  // the frozen convergence body).
  const kc2 = []
  const normalPr = { groups: [{ index: 0, contentKey: 'k0', verdict: 'OK', approved: true }], wholediff: { verdict: 'OK' }, allClean: true }
  const notDegraded = mod.preReviewDegrade(normalPr, 1, 1, kc2)
  assert.strictEqual(notDegraded, false, 'a normal pre-review result does NOT degrade')
  assert.strictEqual(kc2.length, 0, 'no Known Concern recorded on the normal path')
})

// isTestPath — single source of truth for "is this a test path", used to keep
// test files out of a Coder's owned-files list (Phase 1a) and to route test
// review findings to the Tester (Phase 3). The Coder/Tester separation breaks
// silently if a real test path slips through, so the classifier — especially
// the `test_*` prefix convention the earlier regex missed — is asserted directly.
test('test_isTestPath_classifies_test_and_production_paths', async () => {
  const mod = await loadEngine()

  const TEST_PATHS = [
    'test_orders.py',              // bare test_* prefix (the convention the old regex missed)
    'tms/tests/test_orders.py',    // test_* under a tests/ dir
    'module/test_helpers.py',      // test_* prefix in a subdirectory
    'tests/__init__.py',           // test-package registration
    'app/tests/conftest.py',       // anything under a tests/ dir
    'app/test/legacy_spec.rb',     // singular test/ dir
    'order_test.py',               // *_test.* suffix
    'widget.test.js',              // *.test.* (JS)
    'widget.spec.tsx',             // *.spec.* (TS)
  ]
  for (const p of TEST_PATHS) {
    assert.strictEqual(mod.isTestPath(p), true, `expected ${p} to be classified as a test path`)
  }

  const PROD_PATHS = [
    'models/order.py',             // ordinary production module
    'src/widget.js',
    'contest_helpers.py',          // contains "test" but NOT at a path boundary — must NOT match
    'fastest.py',                  // substring "test", no test_ prefix and no tests/ dir
    '',                            // empty / undefined-ish path is not a test path
  ]
  for (const p of PROD_PATHS) {
    assert.strictEqual(mod.isTestPath(p), false, `expected ${p || '<empty>'} to be classified as a production path`)
  }

  // Guards the `p || ''` fallback against null/undefined inputs.
  assert.strictEqual(mod.isTestPath(undefined), false, 'undefined is not a test path')
  assert.strictEqual(mod.isTestPath(null), false, 'null is not a test path')
})

// prodFilesOnly — the filter Phase 1a applies to a coder's owned-files list so
// the "Files you own (production only)" prompt never tells a Coder to author
// its own tests. Asserted directly (not just via isTestPath) because it is the
// artifact that produces the feature's output: test paths must drop while every
// production path survives in order.
test('test_prodFilesOnly_drops_test_paths_keeps_production', async () => {
  const mod = await loadEngine()

  assert.deepStrictEqual(
    mod.prodFilesOnly(['models/order.py', 'test_order.py', 'tests/__init__.py', 'api/views.py']),
    ['models/order.py', 'api/views.py'],
    'drops every test path and keeps production files in order',
  )
  // A coder list with no test paths is returned unchanged.
  assert.deepStrictEqual(
    mod.prodFilesOnly(['a/models.py', 'a/service.py']),
    ['a/models.py', 'a/service.py'],
    'production-only input is preserved',
  )
  // Missing / empty file list is tolerated (the `c.files || []` guard).
  assert.deepStrictEqual(mod.prodFilesOnly(undefined), [], 'undefined files → empty list')
  assert.deepStrictEqual(mod.prodFilesOnly([]), [], 'empty files → empty list')
})

// deriveTasksDir — the board root the engine embeds verbatim into the Scribe's
// `git mv` instruction and the Accept-phase spec glob. A repo may carry several
// SDD roots (repo root plus per-client ones), so this must follow the spec path
// rather than assume a top-level `tasks/`; an unusable path must return null so
// the arg guard fails loudly instead of handing agents a garbage directory.
test('test_deriveTasksDir_follows_the_spec_path_across_sdd_roots', async () => {
  const mod = await loadEngine()

  assert.strictEqual(
    mod.deriveTasksDir('/wt/tasks/4-in-progress/TMS-042-x.md'),
    '/wt/tasks',
    'repo-root board',
  )
  assert.strictEqual(
    mod.deriveTasksDir('/wt/clients/internal-crm/tasks/4-in-progress/TMSBD-286-x.md'),
    '/wt/clients/internal-crm/tasks',
    'per-client board, not the top-level tasks/',
  )
  // A board directory that itself contains the marker name resolves to the last
  // occurrence — the stage directory the lead actually moved the spec into.
  assert.strictEqual(
    mod.deriveTasksDir('/wt/4-in-progress/tasks/4-in-progress/X.md'),
    '/wt/4-in-progress/tasks',
    'last marker wins',
  )

  for (const bad of ['/wt/tasks/3-ready/X.md', '/wt/spec.md', '', undefined, null]) {
    assert.strictEqual(
      mod.deriveTasksDir(bad),
      null,
      `${bad === '' ? '<empty>' : bad} has no 4-in-progress stage → null`,
    )
  }
})
