## Lens: Bugs and strange runtime behavior

Your job for this call is to **find every bug and every piece of
strange runtime behavior** in the diff. Other lenses cover duplication,
architecture, simplicity, test coverage — ignore those here.

Walk the diff end-to-end. Check every added line against all seven
categories below. Do not stop when you find one issue — the next line
may carry a different category.

### The seven categories — find every instance

**1. Config-change surprise.** An env var, flag, compose entry, or
config key is changed in a way that makes the runtime behave
differently without a matching migration step.

- Example: changing `PGDATA` to a subdirectory on an existing
  bind-mounted postgres data dir causes `initdb` to create a fresh
  empty cluster. Prior data appears lost until manually migrated.
- Look for: new env vars, changed default values, new flags wired into
  branching, changed startup commands, swapped image tags.

**2. Inconsistent fix across mirror functions.** A fix is applied to
one function but a sibling / mirror / parallel function has the same
bug untouched.

- Example: `_snap_to_sending_window` is corrected to use `< to_time`,
  but `_calculate_next_send_datetime` still uses `<= to_time` and will
  produce exactly the same off-by-one.
- Method: when you see a fix-shaped change, `Grep` the repo by synonym
  terms of the fixed function (calculate→compute/evaluate,
  process→handle/transform, validate→check/verify, create→make/build).
  If a sibling has the same pre-fix pattern — flag.

**3. State, race, async, transaction assumptions.** The change
introduces or relies on a temporal / concurrency invariant that does
not hold.

- Read outside a lock followed by write inside the lock.
- Optimistic update without compare-and-swap.
- Missing `await` on a coroutine; async function called synchronously.
- Work done outside a transaction that was previously inside.
- Changed commit / rollback ordering.

**4. Authorization and data exposure.** New handler, endpoint, model,
or query exposes data without a matching permission check.

- Missing `ir.rule` on a new model that other users can
  `search_read`.
- New API endpoint without the auth decorator that neighboring
  endpoints use.
- A query that dropped a `WHERE user_id = current_user` filter.
- A new admin action reachable by non-admin users.
- Grep for how neighbors enforce authz and compare.

**5. Injection via dataflow.** SQL / XSS / command injection / path
traversal where the injection path spans multiple functions (the
linter sees one line, you trace from user input to the sink).

- SQL built with f-string inside an otherwise-ORM codebase.
- HTML rendered via a `|safe` filter on a template variable that
  comes from user input.
- File path from user input passed to `open()` without containment.
- Skip classic `eval()`, `os.system(user_input)`, `pickle.loads()` on
  untrusted data — ruff S catches those directly.

**6. Cosmetic fix instead of root-cause fix.** The change papers over
a symptom rather than fixing the code path that produces the wrong
result.

- Adding `try/except` around a call that should not raise in the first
  place.
- Adding `if x is None: return` instead of fixing why `x` becomes
  `None`.
- Adjusting a test assertion to make it pass instead of fixing the
  code under test.
- Adding a retry instead of fixing the underlying failure.
- Widening a type or loosening a constraint to avoid an error at a
  specific call site.

A real fix changes the code path that produces the wrong result. A
cosmetic fix changes what happens after the wrong result is already
produced.

**7. Runtime performance defect.** A change introduces a performance
regression visible at realistic scale.

- N+1 queries: query inside a loop over records.
- Per-row operation where a bulk / batch API exists in this codebase.
- Unbounded iteration or recordset load where realistic data will
  exhaust memory or monopolize a worker.
- Synchronous network call inside a request handler or cron step.
- Anti-join predicate that misses the available partial index.

Flag only concrete cases with a reasoned consequence at realistic
scale. Micro-optimizations without measurable impact are not in
scope.

### Out of scope for this lens

- Anything ruff catches (eval/exec, mutable defaults, shell=True,
  weak crypto, unused imports, missing annotations, line length).
- Hardcoded secrets — a separate scan handles them.
- Semantic duplication, architecture fit, simplicity, SRP — `architecture` lens.
- Missing test coverage — `tests` lens.

### Section 2 header for this lens

Use `### Section 2 — Bugs findings`.
