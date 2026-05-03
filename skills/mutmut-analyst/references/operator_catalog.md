# Mutmut operator catalog

Mutmut applies multiple mutation operators. Each has different signal-to-noise characteristics. Use this catalog when triaging — operator alone tells you a lot before you even read the diff.

## Signal-to-noise ranking

| Tier | Operators | When survived means |
|------|-----------|---------------------|
| **HIGH signal** | ROR, AOR, LCR (and↔or, not removal) | Almost always a real test gap. Write a test. |
| **MEDIUM signal** | CRC (boolean swap, None), numeric boundary | Often a gap. Review the surrounding logic to decide if equivalent. |
| **LOW signal** | SBR (statement removal), UOI on declarative code, string literals on non-business strings | Often noise. Pragma or exclude patterns first, test only if clearly behavioural. |
| **OFTEN EQUIVALENT** | break↔continue (when remaining iterations are no-ops), `+1` on version constants, indent-step constants | Usually pragma. Don't write tests for these. |

## Operator details

### ROR — Relational Operator Replacement

Mutates comparison operators: `>` ↔ `>=`, `<` ↔ `<=`, `==` ↔ `!=`.

**Why high signal:** boundary errors and off-by-one are among the most common bug classes in production. When ROR survives, your tests don't cover the boundary value.

**Killing recipe:**
- For `>` / `<`: add a test where the variable equals the boundary value.
- For `==`: add tests for both equal and unequal cases with different inputs.

**Equivalence cases (rare but real):**
- Integer comparisons where the boundary value is provably unreachable due to upstream invariants.
- Floating-point comparisons where epsilon makes `>` and `>=` indistinguishable in practice.

When in doubt: write the boundary test. Equivalence is the exception.

### AOR — Arithmetic Operator Replacement

Mutates `+` ↔ `-`, `*` ↔ `/`, `//`, `**`, `%`.

**Why high signal:** arithmetic bugs in calculations (billing, percentages, totals, ratios) directly affect data integrity. Survived AOR with `assert result is not None`–style tests is the classic case.

**Killing recipe:** assertions on **exact** values, not just type or non-null. Pick test inputs where `+` and `-` produce different results — `(0, 0)` is a bad test case for AOR because both operations give 0.

**Equivalence cases:**
- `x + 0` vs `x - 0` (rare, but mutmut may try this).
- Operations where result happens to equal regardless (e.g., `1 * 1` vs `1 / 1`).

### LCR — Logical Connector Replacement

Mutates `and` ↔ `or`, removes `not`, swaps boolean operations.

**Why high signal:** access controls, validation chains, multi-condition guards. `if user.is_admin and user.is_active` mutated to `or` is a security bug.

**Killing recipe:** truth-table coverage. For `A and B`, test all four combinations: `(T,T)`, `(T,F)`, `(F,T)`, `(F,F)`. The killing case is the one where `and` and `or` give different results — `(T,F)` or `(F,T)`.

**Note:** LCR mutations are often killed accidentally by tests that have specific input values. Don't assume it's killed — sometimes you got lucky.

### CRC — Constant Replacement

Mutates literal constants: `True` ↔ `False`, `None` → value, integer `+1`, string content.

Sub-types vary in signal:

- **Boolean swap in conditional / flag context** → HIGH signal (feature flags, guards)
- **None ↔ value in defensive code** → HIGH signal (None-handling logic)
- **Integer `+1` on business constants** (limits, thresholds, prices) → MEDIUM signal
- **Integer `+1` on version constants, IDs, indent levels** → LOW signal (often pragma)
- **String literal mutations on user-facing strings** → LOW signal unless string is part of business logic (comparison, validation regex)

**Killing recipe (when high signal):**
- Boolean: test the case where the flag's value is observable in output.
- None: test both with and without None input.
- Numeric boundary: test the exact value (see ROR).

### SBR — Statement Block Removal

Removes whole statements. mutmut 3 has reduced scope of this.

**Why often noise:**
- Removing a logging call has no behavioural effect.
- Removing a side-effect-only statement (cache update, metric increment) often goes undetected because tests don't observe these side effects.

**When it's signal:** when the removed statement has **observable** effect on test output. If your test verifies the side effect (e.g., metric was incremented), SBR fails the test → mutant killed.

**When it's noise:** logging, debug prints, optional cache invalidation, telemetry calls. Pragma these.

**Recommendation:** if SBR dominates survived count (>30% of survived), consider excluding via `pre_mutation` hook based on operator type.

### UOI — Unary Operator Insertion

Inserts `not`, `-`, `+` in front of expressions.

**Signal varies with context:**
- `not bool_var` removed → HIGH signal (boolean inversion is behaviour change).
- `-x` inserted on numeric → HIGH signal (sign change).
- UOI on logging string concatenation → LOW signal (cosmetic).

### String literal mutation

Mutmut wraps string contents with sentinel `XX...XX` to detect string-as-data behaviour.

**HIGH signal contexts:**
- String used in comparison (`if x == "approved"`).
- String used in regex / parsing.
- String used as dict key or enum value.

**LOW signal contexts:**
- Error messages (test would have to assert exact error text).
- User-facing UI text.
- Log message content.

**Recommendation:** if the string is data, write a test using its value. If the string is for human consumption, pragma.

## Practical reading

When you see `mutmut show <id>` output:

1. Look at the diff first. Single-character or single-token change?
2. Apply the operator label. ROR? AOR? LCR? CRC?
3. Use the table above to set expectation: real gap or noise?
4. Then read the surrounding code context to confirm.

This order is important — start from operator, not from code. It's faster and avoids getting nerd-sniped by individual mutants when you should be looking at distribution.
