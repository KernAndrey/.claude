# Known false-positive patterns

This is a catalog of mutmut survived-mutant patterns that are almost always **not** real test gaps. Recognize them, silence them appropriately, move on.

The cost of NOT having this knowledge is that you write fake tests for noise mutants, score climbs but tests become brittle and pin down implementation details. This is the opposite of what mutation testing should do.

## Pattern 1: Version strings

```python
__version__ = '1.2.3'
```

**Mutmut produces:** `__version__ = 'XX1.2.3XX'` (string literal mutation).

**Why it's noise:** there's no test that should assert version == '1.2.3'. Such a test would just be a duplicate of the source.

**Fix:** mutmut treats triple-quoted strings as documentation and skips them. Convert to:
```python
__version__ = """1.2.3"""
```

Or pragma it:
```python
__version__ = '1.2.3'  # pragma: no mutate
```

## Pattern 2: Indent and formatting constants

```python
INDENT_STEP = 4
TAB_WIDTH = 4
```

**Mutmut produces:** `INDENT_STEP = 5` (integer +1 mutation).

**Why it's noise:** these are conventions, not behaviour. The output formatted with 4 vs 5 spaces is "different" but not "wrong". Writing a test that asserts indent == 4 just couples your tests to the implementation choice.

**Fix:** pragma the line.

## Pattern 3: `break` → `return` in early-exit loops

⚠️ mutmut 3.5 does **not** swap `break` ↔ `continue`. Its `_keyword_mapping` is
`break → return`, `continue → break` (plus `is`↔`is not`, `in`↔`not in`). These are
asymmetric and **usually NOT equivalent** — treat a survivor here as a real gap
until proven otherwise, the opposite of the old break↔continue advice.

```python
for item in items:
    if matches(item):
        result = item
        break        # mutant: return  -> returns None, skipping the code below
return result
```

**Why `break` → `return` usually IS a real gap:** the mutated function returns early — normally `None` — skipping everything after the loop. If your tests still pass, they never assert the value the function returns on the match path. That is worth a test.

**Why `continue` → `break` usually IS a real gap:** it stops the loop at the first item that would have been skipped, so any later item is never processed. Tests pass only if no test has a skipped item followed by a meaningful one.

**When either IS equivalent (rare):**
- `break` → `return` where the loop is the last statement and the function's return value is genuinely unused.
- `continue` → `break` where the skip condition can only ever be true for a trailing run of items.

**Fix:** write the test. Pragma only after verifying equivalence:
```python
break  # pragma: no mutate
```

## Pattern 4: Logging and telemetry

```python
logger.info("Processing started")
metrics.increment("processed_count")
```

**Mutmut produces:** string mutation of the message (`"XX...XX"`, case swap) and `operator_arg_removal` — each argument replaced by `None`, or dropped. It does **not** remove the statement: mutmut 3.5 has no statement-removal operator.

**Why it's noise:** the message text and its arguments carry no behaviour. Telemetry calls usually have no observable test impact unless tests specifically assert on metric values.

⚠️ One exception worth checking before dismissing: `arg_to_None` on a *logging* call is noise, but the same operator on a real call (`create(vals)` → `create(None)`) is a genuine gap. Classify by the call, not by the operator alone.

**Fix options:**
- Report them as a separate "message/logging noise" bucket and leave the code alone — this is the right default.
- `# pragma: no mutate` on a line that keeps coming back in triage.

⚠️ There is **no** `pre_mutation` hook / `mutmut_config.py` / `context.skip` in mutmut 3 — that API was removed. You cannot skip by line prefix or by operator. `do_not_mutate` exists but matches **file paths** only:
```toml
[tool.mutmut]
do_not_mutate = ["*/migrations/*", "*/settings.py"]
```

## Pattern 4b: ORM/descriptor type coercion (Odoo, Django, SQLAlchemy, pydantic)

The single biggest source of equivalent mutants in ORM code: **the field coerces the mutated value back to the original**. `operator_name` (`False`→`True`) and `arg_to_None` fire constantly on field assignments, and the ORM quietly normalises them.

Verified against Odoo 19 (`convert_to_cache`) — check the equivalent table for your ORM before triaging, **do not guess**:

| Field type | `False` → | `True` → | `None` → | Mutant equivalent? |
|---|---|---|---|---|
| `Many2one` | `None` | `None` | `None` | **YES** — `{'user_id': False}` → `{'user_id': True}` changes nothing |
| `Integer` (`int(value or 0)`) | `0` | `1` | `0` | `None` **YES**; `True` no (`0`→`1`) |
| `Char` | `None` | `'True'` | `None` | **NO** — a real gap, the field ends up the string `"True"` |
| `Boolean` | `False` | `True` | `False` | `True` no; `None` **YES** |

So in one real audit, `self.write({"void_date": False, "void_by": False, "void_reason": False, "sent_by": False, "paid_by": False})` produced 4 survivors — and only **one** (`void_reason`, a `Char`) was a genuine test gap. The three `Many2one` ones were equivalent, even though the tests *did* assert those fields were cleared. Reporting all four as gaps would have been wrong.

**How to check fast** — ask the ORM, don't reason about it:
```python
f = env["my.model"]._fields["void_by"]
f.convert_to_cache(True, env["my.model"]), f.convert_to_cache(False, env["my.model"])
# equal => equivalent mutant
```

**Rule:** before filing any `False`→`True` / `arg_to_None` survivor on a field assignment as a gap, resolve the field's type and coercion. Same-result => equivalent, close it.

## Pattern 5: Defensive None checks for impossible cases

```python
def process(item):
    if item is None:  # mutmut: 'is not None'
        raise ValueError("item required")
    return item.process()
```

If `item` is type-annotated as non-None and callers always provide a value, the `if item is None` branch is unreachable. Mutating it doesn't change behaviour because no test path triggers the branch.

**Why it's nuanced:**
- If the function is internal, the check is defensive paranoia → pragma.
- If the function is public API, the check IS the contract → write a test that calls with `None`.

**Decision rule:** does any test, anywhere in the suite, deliberately call this function with `None`? If no — write that test (it's a real gap). If yes but the test asserts on the error — the test would catch the mutation, so it shouldn't survive (something else is going on, dig deeper).

## Pattern 6: Error message strings

```python
raise UserError("Invoice not found")
```

**Mutmut produces:** `raise UserError("XXInvoice not foundXX")` (string mutation).

**Why it's noise:** tests assert that an exception is raised, not that the message is exact. Adding `assert "not found" in str(e)` makes tests brittle.

**Fix:** pragma the string. Or, if you really care about user-facing text, extract messages to a separate module that you exclude from `paths_to_mutate`.

## Pattern 7: Configuration / settings declarations

```python
DEFAULT_TIMEOUT = 30
ALLOWED_HOSTS = ['localhost']
DEBUG = True
```

**Why often noise:** these are deployment / environmental defaults. Tests of behaviour use mocked configs, not these literals. Mutating `30` to `31` doesn't change anything observable.

**Fix:** exclude config files from `paths_to_mutate`:
```toml
[tool.mutmut]
paths_to_mutate = ["src/"]
# excludes are by leaving them out of the include list
```

Or pragma.

## Pattern 8: Auto-generated code

- Django migrations
- protobuf-generated code
- OpenAPI client stubs
- alembic migrations
- Odoo `__manifest__.py`

**Why it's noise:** the code is generated, not authored. Mutations on generated code don't reveal test gaps in your code — they reveal behaviour of the generator.

**Fix:** exclude paths.

## Pattern 9: Constant lookup tables

```python
HTTP_STATUS_NAMES = {
    200: "OK",
    404: "Not Found",
    500: "Server Error",
}
```

**Mutmut produces:** integer +1 mutations on keys, string mutations on values.

**Why it's noise:** these are facts, not logic. A test asserting `HTTP_STATUS_NAMES[200] == "OK"` is just restating the source.

**Fix:** if the table is used as data, the consuming code's tests should kill the mutants. If not — exclude or pragma.

## Pattern 10: Type annotations and class fields

```python
class Config:
    timeout: int = 30
    retries: int = 3
```

**Mutmut may try:** `self.timeout = 31`, `self.retries = None`.

**With type checking enabled** (`type_check_command` in mutmut config), these are filtered automatically as type errors.

**Without type checking:** these are usually equivalent because instances configure these via constructor anyway. Pragma if persistent.

## Pattern 11: Dead code reachability paths

```python
def foo(x):
    if isinstance(x, str):
        return process_str(x)
    elif isinstance(x, int):
        return process_int(x)
    else:
        return None  # only reached for unexpected types
```

If your tests only pass `str` and `int`, the `else` branch is dead from tests' perspective. Mutating it produces a survived mutant — not because tests are weak, but because the code path is unreachable from your test inputs.

**Two valid responses:**
1. Add a test passing an unexpected type (verifying defensive behaviour). Most often correct.
2. Pragma the line if the branch is genuinely unreachable in production.

## Recognition workflow

When triaging a survived mutant, check in order:
1. Is the file in pattern 8 (auto-generated)? → exclude path.
2. Does the line match pattern 1, 2, 4, or 6 (version, indent, log, error msg)? → pragma.
3. Is it pattern 3 (`break`→`return` / `continue`→`break`)? → treat as a real gap; only pragma if you prove equivalence.
4. Is it pattern 7 (config declaration)? → exclude or pragma.
5. Otherwise → assume real test gap, write the test.

Don't pragma aggressively — every pragma is a tested-behavior assertion. If you pragma 50% of survived mutants, your mutation gate becomes decorative.
