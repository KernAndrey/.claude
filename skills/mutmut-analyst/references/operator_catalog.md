# Mutmut operator catalog

⚠️ **The ROR / AOR / LCR / CRC names below are the classical mutation-testing taxonomy, used here as a triage lens. They are NOT mutmut's operators**, and mutmut never prints them — `scripts/parse_results.py` infers them from each diff. mutmut 3.5's actual operators are the list in `mutmut/node_mutation.py`:

| mutmut 3.5 operator | What it does | Classical label used here |
|---|---|---|
| `operator_swap_op` | `+`↔`-`, `*`↔`/`, `//`→`/`, `%`→`/`, `**`→`*`, shifts, bitwise, and all augmented forms; `<`↔`<=`, `>`↔`>=`, `==`↔`!=`; `and`↔`or` | AOR, ROR, LCR |
| `operator_name` | `True`↔`False`, `deepcopy`→`copy` — **that is the whole list** | CRC |
| `operator_number` | numeric literal changes | CRC_numeric |
| `operator_string` | `"s"`→`"XXsXX"`, plus `.lower()` / `.upper()` of the literal. **Skips triple-quoted strings** (assumed docs) | string_literal, string_case |
| `operator_arg_removal` | each call arg → `None`; and, if >1 arg, drop each arg | arg_to_None, arg_dropped |
| `operator_keywords` | `is`↔`is not`, `in`↔`not in`, `break`→`return`, `continue`→`break` | — |
| `operator_remove_unary_ops` | drops `not` and `~` | LCR_not_removal |
| `operator_dict_arguments`, `operator_lambda`, `operator_assignment`, `operator_augmented_assignment`, `operator_symmetric_string_methods_swap` (`lower`↔`upper`, `lstrip`↔`rstrip`, `find`↔`rfind`, `partition`↔`rpartition`, …), `operator_unsymmetrical_string_methods_swap` (`split`↔`rsplit`), `operator_match` | as named | — |

**Things mutmut 3.5 does NOT do** — do not go looking for them, and do not accept a report that claims them:
- **No SBR (statement removal).** mutmut 3 never deletes a statement. The closest things are `operator_arg_removal` and `operator_remove_unary_ops`.
- **No UOI (unary operator insertion).**
- **No `break`↔`continue` swap.** It is `break`→`return` and `continue`→`break`, which are *not* symmetric and rarely equivalent.
- **No docstring mutation** — triple-quoted strings are skipped outright, so "string mutations on docstrings" cannot be a noise category.

## Signal-to-noise ranking

| Tier | Operators | When survived means |
|------|-----------|---------------------|
| **HIGH signal** | ROR, AOR, LCR (and↔or, not removal) | Almost always a real test gap. Write a test. |
| **MEDIUM signal** | CRC (True/False swap), numeric boundary, `is`↔`is not`, `in`↔`not in` | Often a gap. Review the surrounding logic to decide if equivalent. |
| **LOW signal** | `string_literal` / `string_case` on user-facing messages, `arg_to_None` on a message argument | Usually noise: asserting exact error wording makes tests brittle. Assert the *type* and leave it. |
| **OFTEN EQUIVALENT** | numeric change to a constant that is never read back, `arg_dropped` on a defaulted argument | Usually pragma. Don't write tests for these. |

A large `string_literal` + `string_case` + `arg_to_None` block concentrated on `raise SomeError(_("..."))` lines is the single most common false-gap cluster: the tests assert the exception type, which is correct design. Report those separately and don't count them against the suite.

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

### arg_to_None / arg_dropped — `operator_arg_removal`

⚠️ **mutmut 3.5 has no statement-removal operator.** It never deletes a statement, so "SBR" cannot appear in a mutmut 3 run — if a report claims it, the report is wrong. The nearest real operator mutates *call arguments*: each arg → `None`, plus (when the call has >1 arg) dropping each arg in turn.

**Why often noise:**
- `raise UserError(_("..."))` → `UserError(None)`: tests assert the exception type, not its wording. Correct design; leave it.
- `_logger.info("done %s", x)` → `_logger.info(None, x)`: no behavioural effect.

**When it's signal:** the argument carries behaviour and the mutant still passes — `create(vals)` → `create(None)`, `record(account, amount)` → `record(account, None)`. That means nothing asserts the value that argument produces.

**Recommendation:** classify by the *call*, not the operator. If message/logging noise dominates, report it as its own bucket and exclude it from the verdict — do **not** try to filter it in config: mutmut 3 offers no operator-level or line-prefix exclusion (`do_not_mutate` matches file paths only, and the `pre_mutation` hook no longer exists).

### not/~ removal — `operator_remove_unary_ops`

⚠️ **mutmut 3.5 does not insert unary operators.** There is no UOI. The only unary-related operator is the *opposite*: `operator_remove_unary_ops`, which **removes** an existing `not` or `~` (it never adds one, and never touches `-`/`+`).

**Signal:** removing `not` inverts a boolean condition, so a survivor is nearly always a real gap — the test never exercises both sides of the branch. Kill it with a case that is truthy and a case that is falsy, with *different expected outcomes*.

### String literal mutation

`operator_string` produces three variants per literal: `"s"` → `"XXsXX"`, plus `.lower()` and `.upper()` of the contents. Triple-quoted strings are **skipped** (mutmut assumes docs), so docstrings are never mutated.

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
