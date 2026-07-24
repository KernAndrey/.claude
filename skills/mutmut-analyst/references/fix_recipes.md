# Fix recipes — operator-specific test templates

Each surviving mutant has an operator type that determines what kind of test will kill it. Generic "add more tests" advice doesn't work — a test for an `>` ↔ `>=` mutant is fundamentally different from a test for an `and` ↔ `or` mutant.

Use this catalog when triaging.

## Recipe: ROR (`>`, `<`, `>=`, `<=`)

**Mutation pattern:** `if x > N` ↔ `if x >= N`, etc.

**Why your tests miss it:** they test cases on either side of the boundary but not at the boundary.

**Recipe:** add a test where the variable equals the boundary value exactly.

```python
# Original
def calculate_late_fee(days_late):
    if days_late > 30:
        return 50
    return 0

# Existing test (insufficient — doesn't kill `>` → `>=`)
def test_late_fee_after_30_days():
    assert calculate_late_fee(45) == 50

def test_no_fee_under_30_days():
    assert calculate_late_fee(20) == 0

# Add this — kills the mutant
def test_no_fee_at_exact_threshold():
    """Late fee starts AFTER 30 days, not AT 30 days."""
    assert calculate_late_fee(30) == 0
```

The boundary value (`30` here) is critical. The original returns `0`, the mutant returns `50`. Tests with values like `29` or `31` don't distinguish them.

## Recipe: ROR (`==`, `!=`)

**Mutation pattern:** `if x == y` ↔ `if x != y`.

**Recipe:** test with **both** equal and non-equal cases, asserting different outcomes for each.

```python
def is_admin(role):
    return role == "admin"

# Insufficient — doesn't kill the mutant if test_passes_for_match is the only test
def test_admin_role():
    assert is_admin("admin") is True

# Add — different outcome for non-admin
def test_non_admin_role():
    assert is_admin("user") is False
```

If you only test `is_admin("admin") is True`, the mutant `role != "admin"` makes the function return `False` for "admin" — which would actually FAIL your test. Wait, that means it's killed. Right — single equality tests often DO kill ROR `==`/`!=` mutants. Where you need extra care: when the function has many branches and your test only exercises one path.

## Recipe: AOR (arithmetic operators)

**Mutation pattern:** `+` ↔ `-`, `*` ↔ `/`, etc.

**Why your tests miss it:** weak assertions like `assert result is not None` or test inputs where the operation result happens to be the same regardless of operator.

**Bad test inputs (don't choose these):**
- `(0, 0)` for AOR — both `0+0` and `0-0` give `0`.
- `(1, 1)` for `*` ↔ `/` — both give 1.
- Identity values that mask operator differences.

**Recipe:** assert the **exact** value, with inputs that distinguish operators.

```python
def calculate_total(price, discount):
    return price - discount

# Insufficient
def test_total_returns_number():
    result = calculate_total(100, 20)
    assert isinstance(result, (int, float))

# Add — exact value with inputs that distinguish + from -
def test_total_subtracts_discount():
    assert calculate_total(100, 20) == 80
    # Note: NOT `(0, 0)` or `(50, 50)` — those don't distinguish + from -
```

## Recipe: LCR (`and` ↔ `or`)

**Mutation pattern:** `if A and B` ↔ `if A or B`.

**Recipe:** truth-table coverage — test the four combinations `(T,T)`, `(T,F)`, `(F,T)`, `(F,F)`. The killing inputs are the ones where `and` and `or` give different results: `(T,F)` and `(F,T)`.

```python
def can_login(user):
    return user.is_active and user.is_verified

# Insufficient — only tests (T,T) and (F,F)
def test_active_verified_can_login():
    user = User(is_active=True, is_verified=True)
    assert can_login(user) is True

def test_inactive_unverified_cannot_login():
    user = User(is_active=False, is_verified=False)
    assert can_login(user) is False

# Add these — distinguishes `and` from `or`
def test_active_unverified_cannot_login():
    user = User(is_active=True, is_verified=False)
    assert can_login(user) is False  # `and` returns False, `or` would return True

def test_inactive_verified_cannot_login():
    user = User(is_active=False, is_verified=True)
    assert can_login(user) is False  # symmetric
```

## Recipe: LCR (`not` removal)

**Mutation pattern:** `if not x` ↔ `if x`.

**Recipe:** assertion that flips outcome for the input.

```python
def is_invalid(value):
    return not value

# Test with falsy input — distinguishes `not value` from `value`
def test_zero_is_invalid():
    assert is_invalid(0) is True  # original

# Test with truthy input
def test_nonzero_is_valid():
    assert is_invalid(1) is False  # original
```

Both tests together kill `not value` ↔ `value` because the asserted result differs.

## Recipe: CRC (`True` ↔ `False`)

**Mutation pattern:** `flag = True` ↔ `flag = False`.

**Recipe:** test that the constant's value is observable in output behaviour.

```python
DEBUG_MODE = True

def process(data):
    if DEBUG_MODE:
        log_extra(data)
    return result(data)

# Bad — doesn't kill DEBUG_MODE = False mutant
def test_process_returns_result():
    assert process(data) == expected

# Good — observable side effect
def test_process_logs_in_debug_mode():
    with capture_logs() as logs:
        process(data)
    assert any("extra" in log for log in logs)
```

If DEBUG_MODE is set as `False`, `log_extra` doesn't run, no logs captured, test fails — mutant killed.

## Recipe: CRC (`None` substitution)

**Mutation pattern:** `default = "value"` ↔ `default = None`.

**Recipe:** test that uses the default and asserts on its specific value.

```python
def greet(name="friend"):
    return f"Hello, {name}!"

# Insufficient
def test_greet_returns_greeting():
    assert greet() is not None

# Good — exact value of default
def test_greet_default_is_friend():
    assert greet() == "Hello, friend!"
```

## Recipe: CRC (numeric `+1`)

**Mutation pattern:** `x = 5` ↔ `x = 6`.

**Recipe:** test that the constant's exact value matters.

If the constant is a magic number with semantic meaning (timeout in seconds, max retries, threshold) — test behaviour at that exact value:

```python
MAX_RETRIES = 3

def fetch_with_retry(url):
    for i in range(MAX_RETRIES):
        try:
            return fetch(url)
        except FailedError:
            continue
    raise GiveUpError()

# Insufficient
def test_fetch_eventually_gives_up():
    with mock.side_effect=FailedError:
        with pytest.raises(GiveUpError):
            fetch_with_retry(url)

# Good — count exact retries
def test_fetch_retries_exactly_3_times():
    mock_fetch = MagicMock(side_effect=[FailedError, FailedError, FailedError, FailedError])
    with patch('module.fetch', mock_fetch):
        with pytest.raises(GiveUpError):
            fetch_with_retry(url)
    assert mock_fetch.call_count == 3  # not 4, not 2 — exactly 3
```

If MAX_RETRIES mutates to 4, `call_count` becomes 4, test fails.

If the constant is genuinely arbitrary (a sentinel ID, a version number, an indent width), pragma it — see `false_positive_patterns.md`.

## Recipe: arg_to_None / arg_dropped (`operator_arg_removal`)

⚠️ **mutmut 3.5 has no statement-removal operator** — it never deletes a line. If you are looking for "SBR", the closest real operator is `operator_arg_removal`, which either replaces one call argument with `None` or (when the call has >1 arg) drops it:

```python
audit_log.record(account, amount)
# mutants: record(None, amount) / record(account, None) / record(amount) / record(account)
```

**Whether it's a real gap depends on the call, not the operator:**

*Noise* — the call is a message or a log; nulling an argument changes nothing observable:
```python
raise UserError(_("Cannot move a deduction."))   # -> UserError(None)
_logger.info("Processing %s", record.name)       # -> _logger.info(None, record.name)
```
Tests assert the exception *type*, which is the right design. Leave them; don't assert exact wording.

*Real gap* — the argument carries behaviour, and the mutant still passes:
```python
def update_balance(account, amount):
    account.balance += amount
    audit_log.record(account, amount)   # -> record(account, None) survives
```

**Insufficient test:**
```python
def test_balance_updated():
    update_balance(acc, 100)
    assert acc.balance == 100
```

**Good test** — observe the side effect, with a value that distinguishes `None`:
```python
def test_audit_logged_on_balance_change():
    update_balance(acc, 100)
    assert acc.balance == 100
    assert audit_log.contains_entry(acc, 100)   # None-amount entry would fail
```

If the side effect is genuinely unobservable (logging, telemetry tests don't care about), leave it. Don't bend tests around unimportant side effects.

## Recipe: string literal mutation

**Mutation pattern:** `"foo"` ↔ `"XXfooXX"`.

**Whether it's a real gap depends on whether the string is data or display:**

**If string is data** (used in comparison, lookup, parsing):
```python
ROLE_ADMIN = "admin"

def is_admin(role):
    return role == ROLE_ADMIN

# Test with the exact string
def test_admin_role_recognized():
    assert is_admin("admin") is True
```

The mutation `ROLE_ADMIN = "XXadminXX"` makes `is_admin("admin")` return `False`, test fails.

**If string is display** (error messages, log messages, UI text):
- Pragma the line.
- Don't write tests asserting exact display text — brittle and meaningless.

## Quick reference table

| Operator | Killing test pattern | Common pitfall |
|----------|----------------------|----------------|
| ROR boundary | Test value == boundary exactly | Testing only values on either side |
| ROR equality | Test both equal and unequal cases | Testing only one case |
| AOR | Exact value assertion, inputs distinguish operators | Identity inputs that mask operator |
| LCR and/or | Truth-table coverage (T,F) and (F,T) | Testing only (T,T) and (F,F) |
| LCR not | Test both truthy and falsy with different expected results | Single-case test |
| CRC boolean | Observe behaviour difference of flag | Testing function output without flag-specific assertions |
| CRC numeric | Exact value matters in observable behaviour | "Returns a number" assertion |
| arg_to_None / arg_dropped | Assert the side effect, with a value a `None` arg could not produce | Test only main return value |
| String literal | Exact string used in data context | None — pragma if display-only |
