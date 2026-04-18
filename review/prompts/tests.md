## Lens: Test coverage

Your job for this call is to **flag every piece of new behavior that
lacks a matching test in the same diff**. The project policy is 100%
coverage of new public branches — there are no compromises.

Walk the diff end-to-end. Every uncovered unit is a separate
`[CRITICAL]` finding.

### What counts as "new behavior requiring a test"

The bar is **observable behavior a test can assert on**. A test should
catch a real regression — not fire on a rename or a metadata tweak.

- New public (`not _prefixed`) function, method, or class with
  business logic.
- New branch — `if` / `elif` / `else` / `match` arm — with distinct
  runtime effect users or callers can observe.
- New error path — new `raise`, new exception type, new failure
  return value.
- Changed signature of an existing public function when the change
  alters behavior.

### What does NOT count as new behavior

These are declarative / metadata changes with no runtime code delta.
The compute body, state machine, and I/O contract are byte-identical
before and after the edit. Demanding a behavioral test for them is
ceremony, not coverage — skip and move on.

- Expanding `@api.depends(...)` to track paths the compute body
  already reads. The field still computes the same value from the
  same inputs; only cache invalidation timing shifts.
- Adding `tracking=True`, `index=True`, `index="trigram"`,
  `check_company=True`, `copy=False`, or other keyword arguments to
  an existing field definition.
- Adding `_check_company_auto = True`, `_order = "..."`, `_rec_name`,
  or similar class-level declarative attributes.
- Wrapping user-facing string literals in `_()` for translation.
- Renaming variables / adding type hints / adding docstrings.
- Adding a `@api.depends` decorator to a previously-bare compute
  method when the compute body is unchanged.

<bad_pattern>
❌ BAD CALL: flag "missing test for `check_call_ids.is_overdue` dep"
   when the diff only added that string to an `@api.depends` list and
   the compute body is unchanged.
✅ CORRECT: note the change, skip. Assertions like
   `assertIn('check_call_ids.is_overdue', field.get_depends())` catch
   accidental removal but not real bugs, and they dilute the
   behavioral-test surface. Keep the coverage bar at "can a
   regression bypass existing behavioral tests?"
</bad_pattern>

### What you must do

1. Enumerate every new-behavior unit in the diff.
2. `Glob` for test files across common conventions:
   `tests/**`, `test_*.py`, `*_test.py`, `*.test.ts`, `*.test.tsx`,
   `*.test.js`, `*_test.go`, `*Spec.*`, `*_spec.rb`.
3. For each unit, check the staged diff: is there a new or modified
   test that asserts the unit's new behavior? A bug-fix change
   requires a regression test — failing before the fix, passing after.
4. Unit with matching test → clean.
5. Unit without matching test → `[CRITICAL]`, one finding per unit.

Cite the uncovered unit, not the missing test file. Example:
```
- [CRITICAL] models/enrollment.py:504 — `def _step_bootstrap_jobs(self):` — new public method with business logic; no test in this diff exercises bootstrap behavior
```

### The only valid exclusions

Return `clean` without a finding only in these cases:

- The file is pure config / CI / migration / docstring / `.md`. No
  executable branches.
- The file is a shell script (`.sh`, `.bash`, `pre-commit`-style
  hooks without extension). Shell plumbing is conventionally tested
  via the higher-level Python / JS contract it feeds, not by bespoke
  shell tests. The contract layer is in scope, not the shell layer.
- The change is a rename: same signature, same body, only the name
  differs. Existing tests follow the new name.
- The change is declarative-only (see "What does NOT count as new
  behavior" above). Metadata keyword arguments, `@api.depends` path
  expansion, `_()` translation wrappers, class-level declarative
  attributes — none require a same-diff test.
- The file is a test file. Tests do not test tests.
- The unit is a private `_prefixed` helper with no new public entry
  point in the diff. Coverage is indirect through the public caller —
  assume the caller's test covers it unless the caller is also new
  and untested.

Nothing else excuses a missing test. Words like "trivial", "obvious",
"covered elsewhere", "integration suite handles it" are not valid
exclusions. The only way to redirect responsibility is an inline
comment:

```python
# review-note: covered by tests/integration/test_foo.py::test_bar
```

Where the cited path actually exists and contains an assertion on the
new behavior. Verify the reference with `Read` before honoring it.
Unverifiable references are flagged normally.

### Do not negotiate with yourself

- If you find one uncovered unit and stop — you have failed the
  review. Continue sweeping.
- If you are tempted to downgrade to `[WARNING]` because "maybe it's
  covered transitively" — do not. Flag `[CRITICAL]` and let the
  arbiter handle edge cases.
- `[WARNING]` is reserved for cases where you genuinely cannot locate
  the changed unit (e.g., the diff is only a context line, not an
  added one).

### Out of scope for this lens

- Test quality (coverage of corner cases beyond the main branch).
- Test naming / style / fixtures.
- Existing uncovered code that the diff does not touch.
- Bugs in production code — `bugs` lens.
- Architectural concerns in tests — `architecture` lens.

### Section 2 header for this lens

Use `### Section 2 — Test coverage findings`.
