---
name: mutmut-analyst
description: Use this skill when working with mutmut mutation testing — running mutmut, interpreting `mutmut results` output, triaging surviving mutants, distinguishing real test gaps from equivalent mutants, fixing weak tests, and integrating mutmut into pre-commit/CI pipelines. Trigger when the user mentions mutmut, mutation testing, mutation score, surviving mutants, killing mutants, or asks why their tests have low mutation score despite high coverage. Also trigger when user shows mutmut output or asks to interpret mutation testing results.
---

# Mutmut analyst

You are working with mutmut, a Python mutation testing tool. This skill is for the **full workflow**: setup → run → triage → fix → re-run, with focus on the triage step where most engineering judgment happens.

## When to invoke this skill

Read this skill when the user:
- Runs mutmut or shows mutmut output
- Asks to interpret mutation score, surviving mutants, or mutation testing results
- Wants to fix surviving mutants
- Sets up mutmut in a new project or pre-commit pipeline
- Asks why coverage is high but mutation score is low

## Critical context Claude must internalize first

**Mutmut 3+ is materially different from mutmut 1-2.** Most online tutorials reference removed features. Specifically:
- `mutmut junitxml` — REMOVED in v3
- `mutmut --runner=...` flag — REMOVED, use `[tool.mutmut]` config or `mutmut_config.py`
- `.mutmut-cache` file — REPLACED by `mutants/` directory
- `mutmut show all` — REMOVED in v3.5; only `mutmut show <id>` works
- `mutmut results` no longer prints emoji summary line in v3.5; output is `<id>: <status>` per non-killed mutant
- pytest >= 9 incompatible with mutmut 3.5 — pin `pytest<9` in dev deps
- Output emojis — somewhat changed

When user shows old-style output or commands, suggest migrating but don't reject the workflow.

**Status mapping (mutmut 3.5+ exit codes in `mutants/<source>.py.meta`):**
| exit_code | mutmut status | meaning |
|-----------|---------------|---------|
| 0 | survived | all tests passed under mutation — tests didn't catch the change |
| 1 | killed | a test failed — mutation was caught |
| 33 | killed | mutmut's `MutmutProgrammaticFailException` raised before tests ran |
| -9, -15, -24, 124 | timeout | pytest got SIGKILL/SIGTERM/SIGXFSZ or hit GNU `timeout` |
| anything else | suspicious | pytest crashed unexpectedly |

⚠️ Beware: `0 = survived` is counterintuitive — exit 0 means "tests passed" which under mutation means the mutation went undetected. Don't invert.

**The fundamental insight:** mutation score answers "do tests catch behavioural changes" — coverage answers "do tests touch lines". A test can have 100% coverage and 0% mutation score (`assert result is not None` style). High survived count is not automatically bad — without operator-by-operator breakdown, raw score is uninformative.

## ⚠️ Server-crash hazards (READ BEFORE ANY mutmut WORK)

Mutmut workflows are **memory-hostile**. Several common patterns will OOM-kill a 16-32 GB workstation. Always check `free -h` before mutmut work and respect these rules:

**Hazard 1 — `mutmut run` is heavy by design.**
Each mutant forks pytest. With 2500 mutants and the default `--max-children=os.cpu_count()`, peak RAM = `cpu_count × (pytest_overhead + your test deps)`. On Odoo or Django, a single pytest worker can be 500 MB+. **Never run mutmut in parallel with anything else memory-significant** (LLM agents, browser automations, other test suites). Pin `--max-children=2` for Odoo, `=4` for typical pytest projects.

**Hazard 2 — never run two `mutmut run` instances on the same project.**
They share `mutants/`, fight for the cache, and add their RAM together. If you parallelize across projects, do it on different machines or with isolated `--mutants-dir` paths.

**Hazard 3 — looping `mutmut show <id>` in a script will eat RAM.**
Each `mutmut show` invocation loads mutmut + libcst + clicks + the whole mutated source file (~150 MB resident). 765 sequential calls = 5-10 GB peak depending on how the OS releases memory between forks. **Use `scripts/parse_results.py` instead** — it parses `mutants/*.py.meta` (JSON) and the mutated source AST in-process, **zero subprocess calls**, finishes in seconds with ~200 MB peak.

**Hazard 4 — don't fan out kimi/sub-agent jobs that each call mutmut.**
A 5-way parallel agent team where each agent runs `mutmut run` or repeated `mutmut show` will absolutely OOM the box. If you must parallelize, do **distribution analysis once with `parse_results.py`** and pass the JSON output to each agent — they read the JSON, no mutmut needed.

**Hazard 5 — `mutants/scripts/<file>.py` itself is 50-100x the original size.**
For a 1500-line source, the mutated copy is ~170 KLOC. Parsing it via `ast.parse()` is ~200 MB transient. Don't parse it in a tight loop — parse once, reuse the AST.

**Recovery if OOM is happening:** `pkill -9 -f mutmut`, then `pkill -9 -f pytest`, check `free -h`. If swap is thrashing, may need to `swapoff/swapon` to clear it.

## Workflow phases

### Phase 1: Setup (when user is starting from scratch)

If `mutmut` is not configured:

1. Check Python version (mutmut 3 requires 3.7+, requires fork support — Windows users need WSL)
2. Verify test runner: pytest is default, but unittest/django/odoo all work
3. Set up `pyproject.toml` config:
   ```toml
   [tool.mutmut]
   paths_to_mutate = ["src/"]
   tests_dir = ["tests/"]
   pytest_add_cli_args_test_selection = ["tests/"]
   ```
4. Recommend `mutate_only_covered_lines = true` IF coverage.py is already integrated — this filter dramatically speeds up runs by skipping uncovered code (which would all "survive" trivially anyway).
5. Recommend `type_check_command` integration with mypy/pyrefly if project has type checking — filters out type-error mutants which are noise.

For Odoo-specific or Django-specific setup quirks, read `references/setup_quirks.md`.

### Phase 2: Run

```bash
mutmut run                              # full run, uses cached baseline timing
mutmut run "module.function*"           # scoped run via Unix glob
```

Mutmut writes mutants to `mutants/` directory. The directory persists between runs — incremental retesting is the default.

If user wants to start fresh: `rm -rf mutants/`. If user just wants to retest survived after fixing tests, `mutmut run` is enough — it remembers what was killed.

### Phase 3: Triage (this is where the skill earns its keep)

When user has survived mutants:

1. **First, get distribution.** Don't look at individual mutants until distribution is known. Run:
   ```bash
   python scripts/parse_results.py --mutants-dir /path/to/project/mutants --out /tmp/mut.json
   # or summary-only (no JSON):
   python scripts/parse_results.py --mutants-dir /path/to/project/mutants --summary-only
   ```
   The script reads `mutants/*.py.meta` (JSON exit codes) + `mutmut-stats.json` (test→function map) + the mutated source AST. **Zero subprocess calls** — it doesn't invoke mutmut at all. Safe to run on 2500-mutant projects (~200 MB peak, finishes in seconds). Output JSON breaks down by operator, file, and function with full per-survivor diffs and covering tests.

   **If 50%+ of survived are in shape "noisy operators"** (string mutations on docstrings, integer +1 on version constants, break↔continue swaps), the score is misleadingly low — fix is config-level, not test-level. See `references/false_positive_patterns.md`.

2. **Classify each survived mutant.** Three buckets:
   - **Real test gap** — mutation changes behaviour, tests don't catch it. Action: write a targeted test.
   - **Equivalent mutant** — mutation is semantically identical (e.g., `age >= 18` ↔ `age > 17` for integers, `break ↔ continue` in a loop where remaining iterations are no-ops). Action: `# pragma: no mutate` inline OR add to project whitelist with reason.
   - **Known noise** — mutation hits code that shouldn't be tested (version strings, log messages, indent constants). Action: refactor to triple-quoted string, exclude from `paths_to_mutate`, or pragma.

   Use `scripts/triage_classifier.py` to get an automated first-pass classification. It applies heuristics from `references/false_positive_patterns.md`. **Treat its output as a draft** — manual review of each candidate is required for anything except the most obvious noise patterns.

3. **Prioritize real test gaps by operator severity.** Read `references/operator_catalog.md` for the signal-to-noise ranking. Quick version:
   - HIGH signal: ROR (relational), AOR (arithmetic), LCR (logical) — these almost always indicate genuine test gaps in business logic.
   - MEDIUM signal: CRC (constants in conditionals), boundary numeric mutations.
   - LOW signal / often noise: SBR (statement removal in non-essential code), UOI on logging/declarative code, string literal mutations.

   When advising user, recommend they fix HIGH-signal gaps first. MEDIUM next. LOW is often where pragmas / whitelists belong rather than new tests.

4. **For each gap to fix, give an operator-specific recipe.** Read `references/fix_recipes.md` for templates. Don't generic-suggest "add a test" — for a ROR `>` → `>=` mutant, the right test is specifically the boundary value. For an LCR `and` → `or`, the right test is the truth-table case that distinguishes them. Wrong recipe = brittle test or test that doesn't actually kill the mutant.

### Phase 4: Fix

Show specific mutants with `mutmut show <id>` to see the diff. The diff format is the same as git unified diff — read the `-` line as the original and the `+` line as the mutation.

After writing a test, retest just that mutant:
```bash
mutmut run                       # incremental — only retests survived
```

Or in `mutmut browse` TUI, press `r` on a mutant to retest it.

If user is fixing many mutants, batch the work: don't run mutmut after every single test. Write 5-10 tests, then run mutmut once.

### Phase 5: Re-run and verify

After fix iteration, the survived count should drop. If it doesn't drop after a fix the user thought was correct:
- The test might not actually exercise the mutated line — verify with `coverage` that the new test hits that line.
- The test might pass on the mutant because the assertion is too weak (`assert result` where any truthy value passes).

Use `mutmut apply <id>` to literally apply the mutation to disk, then run the new test against it manually. If the test still passes — assertion is wrong. **Always git-commit before `apply`**, since it modifies source files.

## Special situations

### "Why is my mutation score 60% when coverage is 100%?"

This is the canonical question. Answer template:

> Coverage means "tests run this line". Mutation score means "tests would catch a bug on this line". They measure different things. A test like `def test_x(): foo(123)` gives 100% line coverage of `foo` but kills zero mutants — no assertion. Look at your survived mutants — they'll show specific bugs your tests miss. Run `python scripts/parse_results.py --mutants-dir mutants --summary-only` for breakdown.

### "I have 813 survived mutants — where do I start?"

Read `references/triage_workflow.md`. Short version: distribution first, never individual mutants first. The 80/20 rule applies — usually 60-70% of survived are in noise categories solvable by config tweaks (operator excludes, pragmas on declarative code), and the remaining 30-40% are real gaps concentrated in 10-20% of functions.

### "Should I aim for 100% mutation score?"

No, and the spec for mutation_gate explicitly calls this out as anti-pattern. 100% drives brittle tests. Healthy zone is 75-85% with low whitelist count. Anti-metric: 95%+ score with growing whitelist — means tests pin down implementation details.

### "Mutmut takes hours to run"

Optimizations in priority order:
1. `mutate_only_covered_lines = true` — biggest single win for projects with existing coverage data.
2. Operator pruning — exclude SBR and UOI globally if shown to be noise (see Phase 3 step 1). Done via `mutmut_config.py` `pre_mutation` hook returning `context.skip = True` based on operator type.
3. Smart test selection in `pre_mutation`:
   ```python
   def pre_mutation(context):
       # Run only tests for the file being mutated
       module_path = context.filename.replace('/', '.').rstrip('.py')
       context.config.test_command = f'pytest -x -k {module_name}'
   ```
4. Incremental mode is default — don't `rm -rf mutants/` unless you have to.

For Odoo projects specifically, read `references/setup_quirks.md` — DB bootstrap is the dominant cost and there are workarounds.

## Output style for the user

When presenting analysis:

- Lead with the **distribution breakdown**, not individual mutants. "Of 813 survived: 487 SBR (likely noise), 156 ROR (real gaps), 98 AOR (real gaps), 72 other."
- Group findings by **action**, not by file. "These 487 are config-level fixes, these 254 need new tests, these ~70 are equivalent — whitelist with reasons."
- For each real gap, show the **diff** + the **specific test recipe** for that operator.
- Avoid generic "add more tests" advice. Always operator-specific.

## Files in this skill

- `scripts/parse_results.py` — Parses `mutmut results` output to structured JSON with distribution analytics.
- `scripts/triage_classifier.py` — First-pass automatic classification of survived mutants into real_gap / equivalent_candidate / noise categories.
- `references/operator_catalog.md` — Mutmut operators with signal-to-noise ranking and what each detects.
- `references/false_positive_patterns.md` — Known false-positive patterns to recognize and how to silence them.
- `references/triage_workflow.md` — Full triage workflow including call-graph reasoning techniques (Trail of Bits-style).
- `references/fix_recipes.md` — Operator-specific templates for writing tests that kill specific mutant types.
- `references/setup_quirks.md` — Project-type-specific setup notes (Odoo, Django, src-layout, monorepo).

Read references on demand when relevant — don't preload all of them.
