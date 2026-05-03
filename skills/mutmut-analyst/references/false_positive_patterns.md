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

## Pattern 3: break ↔ continue in early-exit loops

```python
for item in items:
    if matches(item):
        result = item
        break  # mutant: continue
return result
```

**Why it's often equivalent:** if `matches` is true once, both `break` and `continue` produce the correct result — `continue` just iterates needlessly through remaining items. Behaviour identical, performance worse.

**When it's NOT equivalent:**
- If the loop has side effects in remaining iterations (e.g., counter, accumulation).
- If subsequent iterations would also match and overwrite `result`.

**Fix:** pragma if you've verified equivalence:
```python
break  # pragma: no mutate
```

## Pattern 4: Logging and telemetry

```python
logger.info("Processing started")
metrics.increment("processed_count")
```

**Mutmut produces:** statement removal (SBR) or string mutation.

**Why it's noise:** behaviour shouldn't change if logging is removed. Telemetry calls usually don't have observable test impact unless tests specifically assert on metric values.

**Fix options:**
- Pragma the line.
- Use `pre_mutation` hook in `mutmut_config.py`:
  ```python
  def pre_mutation(context):
      line = context.current_source_line.strip()
      if line.startswith(('log.', 'logger.', 'logging.', 'metrics.', 'tracer.', 'print(')):
          context.skip = True
  ```

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
3. Is it pattern 3 (break↔continue)? → analyze equivalence, pragma if equivalent.
4. Is it pattern 7 (config declaration)? → exclude or pragma.
5. Otherwise → assume real test gap, write the test.

Don't pragma aggressively — every pragma is a tested-behavior assertion. If you pragma 50% of survived mutants, your mutation gate becomes decorative.
