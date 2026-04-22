## Lens: Test coverage

Your job for this call is to **flag every piece of new behavior that
lacks a matching test in the same diff**. The project policy is 100%
coverage of new public branches — but "new public branch" is a
narrower set than "every line starting with `+`". The list under
"What does NOT count as new behavior" is the precise boundary; apply
it literally.

Walk the diff end-to-end. Every uncovered unit that falls outside the
exclusion list is a `[CRITICAL]` finding; borderline cases (see
per-category notes) are `[WARNING]`.

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

<critical>
If the only way to reach the branch from a test is to monkeypatch the
stdlib/framework call it guards, a static library argument it forwards,
or a UI-view attribute it declares, the test asserts against the
monkeypatch — not against your code. **Skip.** The coverage bar is
"could a regression here change what a caller observes about your
code"; a different stub return value does not count.
</critical>

The six classes below share one shape: the runtime contract is
byte-identical before and after the edit, OR the branch forwards a
framework/stdlib behavior the project did not author. Demanding a
behavioral test for them is ceremony, not coverage — skip and move on.

**1. Declarative / metadata changes**

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
- Unconditional literal assignments (`"subtitle": "Tracking"`). No
  branching → only a manual rename can regress it. Metadata, not
  behavior.
- Defensive branches guarded by a declared model-layer invariant
  (`required=True`, `@api.constrains`, NOT NULL). The guarded arm is
  unreachable. Cite the invariant `file:line`, verify with `Read`,
  then surface via architecture lens, not tests.
- Sibling absence under one guard. `if X:` suppressing N fields is
  one behavioral unit — emit one finding on the guard, not N on
  each leaf.

**2. Stdlib / framework-delegated error handlers** *(category A)*

All FOUR conditions must hold — miss any and the branch stays
`[CRITICAL]`:

- `try` body contains exactly one stdlib/framework call
  (`Path.read_text`, `Path.mkdir`, `Path.unlink`, `os.replace`,
  `shutil.copy2`, `input()`, `json.loads`,
  `base64.urlsafe_b64decode`, `subprocess.run`, HTTP-client
  `raise_for_status`, Odoo ORM browse, SQLAlchemy session ops), or a
  trivially composed chain of them (`iterdir()` + sort, `read_text()`
  + `json.loads`).
- `except` body is one of: `return None` / `return` / `pass` /
  `continue` / `die(msg)` / `_logger.<level>(msg)` followed by one of
  the previous. **No state write**, no user-owned compensation, no
  raise of a DIFFERENT exception type.
- Exception class is one that is **hard to induce via a test
  fixture** (requires environmental or stdin simulation, not just a
  crafted input): `OSError`, `FileNotFoundError`, `PermissionError`,
  `EOFError`, `KeyboardInterrupt`, and library-client error types
  (`HttpError` ONLY when the caller re-raises the same type).

  **Does NOT apply** to exception classes that are naturally
  fixture-testable: `json.JSONDecodeError` (feed bad JSON),
  `UnicodeDecodeError` (feed bad bytes), `binascii.Error` (feed bad
  base64), `ValueError`, `KeyError`, `AttributeError` — a test
  exercises these by supplying a crafted input, not by monkeypatching
  the stdlib call. Branches catching these stay `[CRITICAL]` when
  uncovered.
- **Transitive coverage exists.** The enclosing function is
  `Grep`-pable in the diff's new/modified test files. If no
  same-diff test exercises the enclosing function at all, the `try`
  body itself is uncovered — flag the function, not the handler.

When conditions 1-3 hold but condition 4 is borderline (enclosing
function is reached only through a deeper caller several layers up),
emit `[WARNING]`, not silence.

**3. Log-only observer branches** *(category B)*

Applies to `if` / `elif` / `else` branches whose body is one or more
`_logger.{debug,info,warning,error,exception}` calls and nothing
else — no `return`, no state write, no raise, no response-code
change, no counter update, no metric emit. "The log line didn't
appear" is not a regression a reasonable test asserts.

**Does NOT apply to `except` blocks.** An `except` body suppresses
an exception; removing it lets the exception propagate, which IS a
caller-observable change. Even if the body reads like
`except E: _logger.warning(e)`, the branch is a failure-tolerance
decision — test it (or route it through category A if it forwards an
stdlib error).

**Does NOT apply when the branch causes fall-through to a
state-writing path** with a defaulted value (e.g. an early `if x is
None: _logger.warning(...)` that lets `x=None` flow into a later
`self.write({..., 'field': x})`). The test target is the downstream
write, not the log.

When the log message itself is an audit trail operators depend on,
emit `[WARNING]` instead of silence.

**4. Idempotent bootstrap wrappers** *(category C)*

A function whose whole body is one idempotent stdlib setup call
(`Path.mkdir(exist_ok=True)`, `Path.touch`,
`os.makedirs(exist_ok=True)`, `logging.getLogger(__name__)`,
idempotent `Connection.ping()`), optionally followed by a
category-A handler. No branching of its own. Any test that touches
a caller exercises it transitively.

**5. Declarative view/XML conditionals** *(category D)*

Single-attribute conditional visibility / decoration in Odoo XML
views (`invisible="..."`, `readonly="..."`, `required="..."`,
`decoration-*`, `attrs="..."`), Jinja `{% if %}` wrapping
show/hide, or equivalent template conditionals. The condition is
one boolean expression on a record field; the body changes DOM
rendering, not model state. The project's ORM test harness does
not render views.

If the view change wires `context="..."` or `button type="object"`
(which reshapes model state on click), it is NOT declarative —
test the action. When the attribute hides a button whose field
name suggests state rather than display (`invisible="not
is_approved"`), emit `[WARNING]` instead of silence.

**6. Outbound-call-argument assertions — cosmetic kwargs only** *(category E)*

A branch whose "new behavior" reduces to "kwargs passed to a
library / framework call include this particular value." Exclusion
applies ONLY when **all four** hold:

- The flagged unit is one kwarg at one call site.
- The kwarg is **purely cosmetic / observability** — formatting
  strings, log labels, description fields, user-facing messages
  (e.g. `description=f'Gmail push ingest for {email_address}'`,
  `log_context={'request_id': req_id}`).
- The kwarg is **none** of the following — these ALWAYS require a
  test:
  - Security / authentication: `audience=`, `issuer=`, `verify=`,
    `cert=`, `algorithms=`, `scope=`, `client_id=`, signing kwargs.
    Regression = fail-open.
  - Idempotency / dedup: `identity_key=`, `dedupe_key=`,
    `request_id=`, `idempotency_key=`. Regression = duplicate
    processing.
  - Routing / channel: `channel=`, `queue=`, `topic=`,
    `routing_key=`, `partition=`. Regression = work on wrong pool.
  - Authorization scope: `sudo=`, `allowed_company_ids=`, `uid=`.
    Regression = widened data visibility.
- No same-diff test covers a downstream observable effect that
  would catch the same regression.

When the kwarg's semantic category is unclear (e.g. a new custom
kwarg) emit `[WARNING]`, not silence.

<bad_pattern>
❌ BAD CALL: flag `except OSError as e: die(f"failed to read {path}: {e}")`
   around `Path.read_text()` as uncovered because no test makes
   `read_text` raise `OSError`.
✅ CORRECT: category A applies — try body is one stdlib call, except
   is a terminal `die`, exception class is stdlib-raised. Confirm the
   enclosing function has a happy-path test in the diff (`Grep` the
   function name in the test files). If yes, skip. If no, flag the
   enclosing function as uncovered — the handler is not the finding.
</bad_pattern>

<bad_pattern>
❌ BAD CALL: flag `<button invisible="not watch_enabled"/>` as
   uncovered new behavior because no test asserts the button is
   hidden for disabled watches.
✅ CORRECT: category D applies — single-attribute conditional
   visibility on a record field; Odoo does not render views in ORM
   tests. Skip. Typos / inverted conditions surface in manual QA,
   not unit tests.
</bad_pattern>

<good_pattern>
✅ STILL FLAG: `if not token: return Response(status=401)` in a webhook.
   HTTP status is user-visible state the caller sees; auth contract
   requires a test. Categories A-E do not exclude this — response-code
   changes are state changes.
✅ STILL FLAG: `id_token.verify_oauth2_token(token, audience=audience)`.
   Category E excludes cosmetic kwargs ONLY; `audience=` is
   security-critical — regression = fail-open.
✅ STILL FLAG: `except UniqueViolation: return Response(status=204)`.
   Project's own idempotency contract, not stdlib plumbing — the
   except body writes a response code and a dedup decision.
✅ STILL FLAG: `except HttpError as e: raise UserError(...)`.
   Category A excludes exception forwarding; this is an exception-type
   CONVERSION — test the new exception type.
✅ STILL FLAG: `except json.JSONDecodeError: die("corrupted profile")`.
   Category A only excludes hard-to-fixture exceptions. Feeding a
   malformed JSON file is a trivial fixture — write the test.
</good_pattern>

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

<bad_pattern>
❌ BAD CALL: flag `if customer_name:` uncovered when `customer` is
   `required=True` (cite `models/shipment.py:206`) and `customer.name`
   is `required=True`. The guarded arm is dead on the public path.
✅ CORRECT: skip. Emit architecture-lens finding to delete the branch.
   Pinning a test on dead code only triggers architecture to ask for
   its removal on the next pass.
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
- The change matches any category in "What does NOT count as new
  behavior" above — declarative / metadata (class 1), stdlib /
  framework-delegated error handlers (category A), log-only observer
  branches (category B), idempotent bootstrap wrappers (category C),
  declarative view/XML conditionals (category D), or cosmetic-only
  outbound kwargs (category E). Each category has a precise shape;
  apply it literally.
- The branch is guarded by a declared model-layer invariant
  (`required=True`, `@api.constrains`, NOT NULL). Cite the invariant
  `file:line`, verify with `Read`. Report as architecture-lens
  finding, not tests.
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

### Severity policy

- All four category conditions hold cleanly → return clean, no
  finding.
- Three of four hold, one is borderline (see per-category "emit
  `[WARNING]`" notes) → emit `[WARNING]`. Advisory; arbiter still
  reviews.
- Any condition fails outright → `[CRITICAL]`.
- If you genuinely cannot locate the changed unit (e.g. the diff is
  only a context line), emit `[WARNING]`.

Do not downgrade a clear `[CRITICAL]` to `[WARNING]` to avoid blocking
a commit — that is the arbiter's call, not yours.

### Out of scope for this lens

- Test quality (coverage of corner cases beyond the main branch).
- Test naming / style / fixtures.
- Existing uncovered code that the diff does not touch.
- Bugs in production code — `bugs` lens.
- Architectural concerns in tests — `architecture` lens.

### Section 2 header for this lens

Use `### Section 2 — Test coverage findings`.
