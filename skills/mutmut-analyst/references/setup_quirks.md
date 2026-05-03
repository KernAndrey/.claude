# Setup quirks per project type

Project-specific gotchas when running mutmut. Read the section relevant to the user's project.

## Migrating from mutmut 1-2 to mutmut 3

Most online tutorials reference v1/v2 commands. Quick translation table:

| v1/v2 | v3+ |
|-------|-----|
| `mutmut run --runner='pytest -x'` | `[tool.mutmut] runner = "pytest -x"` in `pyproject.toml`, or `mutmut_config.py` `pre_mutation` hook |
| `mutmut junitxml` | REMOVED. Parse `mutmut results` + `mutmut show all` manually (see `scripts/parse_results.py`). |
| `--no-progress` flag | REMOVED. |
| `.mutmut-cache` file | REPLACED by `mutants/` directory (file → folder). |
| `mutmut show <id>` (numeric) | Still works — IDs are still numeric. |
| `mutmut browse` TUI | Still present, expanded. Press `f` to retest a function, `m` for module, `r` for individual mutant. |

If user has CI scripts using removed flags, suggest the migration path. Don't reject the workflow.

## Odoo

Odoo's test runner is `odoo-bin --test-enable`, not pytest. Tests require a database bootstrap each run (or transaction rollback if persisting DB). This is the dominant cost driver.

**Setup:**

```python
# mutmut_config.py
import os

def pre_mutation(context):
    # Limit test scope to the module being mutated
    module = extract_odoo_module(context.filename)
    db_name = f"test_db_{os.getpid()}"  # avoid concurrent worker DB collision

    context.config.test_command = (
        f"coverage run --branch odoo-bin "
        f"--test-enable --stop-after-init "
        f"-i {module} -d {db_name}"
    )

def extract_odoo_module(filepath):
    # e.g. "addons/tms_core/models/load.py" → "tms_core"
    parts = filepath.split("/")
    if "addons" in parts:
        idx = parts.index("addons")
        return parts[idx + 1] if idx + 1 < len(parts) else None
    return None
```

**Performance expectations:** even with smart test selection, per-mutation runtime is 30-180 seconds because of DB setup. A diff with 50 changed lines → 200-300 mutants → 1.5-9 hours. This is acceptable for AI-agent contexts but not for interactive use.

**Optimizations:**
- `mutate_only_covered_lines = true` — critical, Odoo codebases have lots of view definitions / declarative code that isn't behaviorally tested.
- Persistent test DB with transaction rollback (advanced) — converts 60-second per-mutant cost to ~5 seconds. Setup is non-trivial; only worth it for large mutation budgets.
- Concurrency limited to ~2 workers (DB IO becomes bottleneck).

**Exclude patterns for Odoo:**
- `**/migrations/**` (Odoo data migrations)
- `**/__manifest__.py` (module declarations)
- `**/views/**.xml` (XML views)
- `**/data/**` (demo data, security CSV)
- `**/static/**` (web assets)
- `**/i18n/**` (translations)

## Django

Django's `manage.py test` works as a runner. Common setup issues:

**Path resolution:** mutmut runs pytest/manage.py from inside `mutants/` directory. If your `tests_dir` uses a relative path, it might not resolve. Workaround:

```toml
[tool.mutmut]
tests_dir = ["../tests/"]  # note the leading ../
```

(See https://github.com/boxed/mutmut/issues/456 for details.)

**Migrations:** always exclude. They're auto-generated.

```toml
[tool.mutmut]
paths_to_mutate = ["src/myapp/"]  # exclude migrations from include list
```

**Settings file:** typically excluded. Use `mutmut_config.py` `pre_mutation` to skip if needed:
```python
def pre_mutation(context):
    if "settings" in context.filename:
        context.skip = True
```

**Common false positives:**
- Admin definitions (`admin.py`) — UI layer, often pragma-able.
- Form field declarations — declarative, often equivalent.
- URL patterns — declarative.

## src/ layout (modern Python packages)

Mutmut 3 has [a known issue](https://github.com/boxed/mutmut/issues/373) where it treats `src/` directory specially in some cases. If you see assertion errors about `src`, options:

1. Ensure `src/` is in `paths_to_mutate`:
   ```toml
   [tool.mutmut]
   paths_to_mutate = ["src/your_package/"]  # not just "src/"
   ```

2. If still failing, rename `src/` directory (last resort, generally not worth doing).

## Monorepo (multi-package)

Run mutmut **per-package**, not from the monorepo root. Each package gets its own:
- `pyproject.toml` `[tool.mutmut]` section
- `mutants/` directory
- Separate `mutmut run` invocation

Aggregate results across packages manually if needed for global score.

```bash
# Top-level convenience script
for pkg in packages/*/; do
    (cd "$pkg" && mutmut run)
done
```

## Hammett test runner (boxed/hammett)

Mutmut has special integration with hammett — significantly faster. If user has hammett, use it:

```toml
[tool.mutmut]
runner = "hammett"
```

Otherwise pytest is fine.

## CI integration

Mutmut runs are slow → not for every PR.

**Recommended pattern:**
- Nightly cron job: full mutmut run on `main`, results posted as artifact.
- PR pre-push: scoped run on changed files only:
  ```bash
  CHANGED=$(git diff --name-only origin/main HEAD -- '*.py' | grep -v test_)
  if [ -n "$CHANGED" ]; then
      mutmut run "${CHANGED[@]}"
  fi
  ```

**Persistent cache in CI:** cache the `mutants/` directory between runs (e.g., GitHub Actions `actions/cache`). Cache key by codebase hash for stability.

## "Stopping early, because we could not find any test case for any mutant"

This common error means mutmut's test discovery isn't finding tests that cover mutated code. Usually:

1. `tests_dir` path is wrong relative to mutmut's working directory.
2. Test runner doesn't auto-discover (e.g., needs explicit `-k` or test file paths).
3. `mutate_only_covered_lines = true` is set, but coverage data is missing or stale.

Debug by running the test command mutmut would use, manually:
```bash
cd mutants/   # mutmut's working dir during runs
python -m pytest <your tests_dir>
```

If pytest doesn't find tests there, fix paths in config.

## "TypeError: function() takes 1 positional argument but 2 were given" during mutation

Mutmut sometimes generates mutants that change function signatures (e.g., decorator removal). The mutation produces a TypeError when called by tests. Mutmut counts this as "killed" (test failed).

Usually fine — typeerror-killed mutants are valid kills. But if you see this dominating, it might indicate the mutation is too aggressive on decorators / signature-affecting code. Pragma those lines or filter via type checker integration.

## Type checker integration (mypy/pyrefly)

If your project uses mypy or pyrefly, enable filtering:

```toml
[tool.mutmut]
type_check_command = ["mypy", "src/", "--output", "json"]
# or for pyrefly:
# type_check_command = ["pyrefly", "check", "--output-format=json"]
```

This filters out mutants that produce type errors before running tests — they're not real test gaps, they're type errors that the type checker catches.

**Caveat:** filtering may hide some real gaps too. For example, `x: str = None` is a type error AND a sign that the test doesn't exercise None inputs. The mutmut docs explicitly note this trade-off.

**Recommendation:** enable the filter for noise reduction, but if your mutation score seems suspiciously high, temporarily disable to see what's hiding.

**Currently supported type checkers:** mypy and pyrefly only. pyright and `ty` are NOT supported because they don't isolate type errors per-mutation reliably.
