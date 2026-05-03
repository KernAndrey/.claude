# Triage workflow

This is the step-by-step process for handling survived mutants when there are many of them. It's optimized for **economic triage** — fixing the highest-value gaps with the least effort, not gold-plating the codebase.

## Before triage: get distribution

The single most important rule: **never start with individual mutants**. Always start with distribution.

```bash
mutmut results > /tmp/mutmut_results.txt
mutmut show all > /tmp/mutmut_show_all.txt

python ~/.claude/skills/mutmut-analyst/scripts/parse_results.py \
  --results-output /tmp/mutmut_results.txt \
  --show-output /tmp/mutmut_show_all.txt \
  --out /tmp/mutmut_parsed.json

python ~/.claude/skills/mutmut-analyst/scripts/triage_classifier.py \
  /tmp/mutmut_parsed.json \
  --output /tmp/mutmut_classified.json
```

Then look at the `summary` and `actions_by_frequency` blocks. Two questions:

1. **What dominates the survived count?** If 60%+ are in noise categories, your fix is config-level, not test-level — go to "Layer 0" below.
2. **Where do real gaps cluster?** Usually in 10-20% of files. Not evenly distributed.

## Layer 0: Config-level wins

Before writing a single test, check whether a config change kills most of the noise.

### Action 0.1: exclude noisy paths

If survived mutants cluster in:
- `migrations/`, `__pycache__/`, `_generated/` — exclude paths from `paths_to_mutate`.
- `__manifest__.py`, `version.py`, `__init__.py` — exclude or use pattern matching.

```toml
[tool.mutmut]
paths_to_mutate = ["src/"]  # narrow the scope
```

Re-run mutmut. The survived count typically drops 20-50% just from this.

### Action 0.2: skip noise operators via pre_mutation hook

If many SBR survived mutants are on logging / telemetry / metric calls:

```python
# mutmut_config.py
def pre_mutation(context):
    line = context.current_source_line.strip()
    skip_prefixes = (
        "logger.", "logging.", "log.",
        "metrics.", "tracer.", "stats.",
        "print(", "sys.stderr",
    )
    if line.startswith(skip_prefixes):
        context.skip = True
```

This is preferable to per-line pragmas — cleaner and applies project-wide.

### Action 0.3: enable mypy/pyrefly filter

If your project uses type checking but mutmut isn't using it, many "survived" mutants are actually type errors that should be filtered:

```toml
[tool.mutmut]
type_check_command = ["mypy", "src/", "--output", "json"]
```

Re-run. Survived count drops further (typically 5-15%).

After Layer 0, real survived count is usually 30-50% of original. **Now** work on individual mutants.

## Layer 1: Cluster by file and function

Use the `by_file` block from parse_results.py output. Identify functions where survived count is concentrated.

Heuristic: if a function has `survived >= 5`, the function's tests are systematically weak — not just "missing one boundary". Treat the entire function's test as the unit of work, not individual mutants.

For each high-survival function:
1. Read the existing test for that function.
2. Identify the assertion pattern. Is it `assert result is not None`? Is it `assert isinstance(result, X)`? These are the weak patterns.
3. Rewrite the test with **value assertions** — `assert result == expected_specific_value`.

Often, fixing one weak test pattern in one function kills 5-10 mutants at once. This is leverage.

## Layer 2: Operator-driven prioritization

Now sort remaining survived mutants by operator priority (see `operator_catalog.md`):

1. **HIGH signal first** — ROR, AOR, LCR, CRC boolean/None.
2. **MEDIUM signal next** — numeric boundary, defensive checks.
3. **LOW signal last** — string mutations, edge SBR.

For each HIGH-signal mutant:
1. Read the diff: `mutmut show <id>`.
2. Identify the operator (the script's classifier provides this).
3. Apply the matching recipe from `fix_recipes.md`.
4. Write the test.
5. Move on. Don't run mutmut after each test — batch 5-10 fixes, then run once.

## Layer 3: Equivalence reasoning (the hard cases)

Some survived mutants are genuinely equivalent. They look like real gaps until you reason carefully.

**Trail of Bits trick** (from their Trailmark workflow): if you have a call graph of the codebase, you can often see why a mutation is equivalent due to constraints upstream. Example:

```python
# This is the mutated function:
def step(value):  # value is a NAF digit
    if value > 16:  # mutmut: value >= 16
        ...
```

Looking at the function in isolation, you'd write a boundary test for `value == 16`. But if you read the call graph and see that `value` always comes from `nonAdjacentForm()` which returns digits in range `{-15, ..., 15}` — then `value == 16` is unreachable, and the mutation is equivalent.

**Practical heuristic without a call graph:** for any candidate-equivalent mutant, ask:
1. Can I construct a test input that distinguishes the original from the mutant?
2. If yes — it's a real gap, write the test.
3. If no, and the reason is an upstream constraint — it's equivalent. Pragma with reason.
4. If no, and you can't articulate why — assume real gap and dig deeper. Write the test or accept low confidence.

**Don't whitelist on intuition.** Equivalence requires articulation. If you can't write a one-line reason for why no test could ever distinguish, it's not equivalent — your tests are just weak.

## Layer 4: Whitelist with discipline

For confirmed equivalent mutants, two whitelist mechanisms:

### Inline pragma

```python
def is_adult(age: int) -> bool:
    return age >= 18  # pragma: no mutate
    # ROR mutation `age >= 18` → `age > 17` is equivalent for integer ages
```

Pros: visible in code, requires no separate file, comment forces explanation.
Cons: can spread, hard to audit project-wide.

### File-based whitelist (project-level)

If your project has a `.mutation-ignore.yaml` (or whatever name your project uses):

```yaml
- file: src/billing/invoice.py
  line: 142
  operator: ROR
  reason: amount validated > 0 by upstream caller, branch unreachable internally
  reviewed_by: andrii
  reviewed_at: 2026-04-15
```

Pros: centralized audit, structured metadata.
Cons: drifts when code moves; requires periodic cleanup.

**Discipline rules** (from mutation_gate spec):
- Reason must be ≥ 30 characters and explain WHY the mutation is unkillable, not "noise".
- Whitelist entries should expire (180 days default) — periodic cleanup prevents accumulation of stale entries.
- AI agents should not whitelist HIGH-signal operators (ROR/AOR/LCR/CRC) without human review.

## Layer 5: Re-run and verify

After fixing tests, re-run:

```bash
mutmut run
```

Mutmut's incremental mode only retests previously-survived mutants. New score is shown.

If survived count didn't drop after fixes:
- **The new test doesn't exercise the mutated line.** Verify with `coverage`:
  ```bash
  coverage run -m pytest tests/test_new_thing.py
  coverage report --include=src/billing.py
  ```
- **The new test's assertion is too weak.** Apply the mutant manually and run the test:
  ```bash
  git stash
  mutmut apply <id>
  pytest tests/test_new_thing.py
  ```
  If the test still passes — assertion needs strengthening. After verification:
  ```bash
  git checkout src/billing.py
  git stash pop
  ```

## Estimating effort

Rough time estimates per mutant category (after Layer 0 cleanup):

| Category | Effort per mutant |
|----------|-------------------|
| HIGH-signal real gap | 5-15 min (write targeted test) |
| MEDIUM-signal real gap | 10-30 min (verify behaviour first) |
| Equivalent candidate | 10-20 min (reason about equivalence, write whitelist entry) |
| Already-whitelist-eligible | 2 min (pragma + commit) |

For 100 survived mutants:
- After Layer 0, expect ~50 remaining real gaps.
- ~40 fixed by improving 10-15 weak test patterns at function level.
- ~10 individual fixes for one-off gaps.
- ~5 whitelisted as equivalent.

This typically maps to 1-2 days of focused work for an experienced developer, or several iterations for an AI agent doing it incrementally.

## What NOT to do

- **Don't aim for 100%.** It drives test brittleness. 75-85% with low whitelist count is healthy.
- **Don't whitelist instead of testing.** Whitelist only after articulating equivalence.
- **Don't fix mutants in random order.** Distribution → Layer 0 → file clusters → operator priority. Following this order is 5-10x more efficient than picking mutants randomly.
- **Don't run mutmut after every single test fix.** Batch 5-10 changes, run once. Mutmut is slow.
- **Don't disable operators globally because you saw a few false positives.** A targeted pragma costs nothing and preserves signal from genuine bugs that operator might catch elsewhere.
