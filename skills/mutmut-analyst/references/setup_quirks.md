# Setup quirks per project type

Project-specific gotchas when running mutmut. Read the section relevant to the user's project.

## Migrating from mutmut 1-2 to mutmut 3

Most online tutorials reference v1/v2 commands. Quick translation table:

| v1/v2 | v3+ |
|-------|-----|
| `mutmut run --runner='pytest -x'` | **No replacement.** The runner is hardcoded to pytest (`runner = PytestRunner()`, with a `# TODO: config/option for runner` above it). There is no `runner` config key. Shape the pytest invocation with `pytest_add_cli_args` / `pytest_add_cli_args_test_selection`, or make your tests runnable under pytest via `conftest.py` (see the Odoo section). |
| `mutmut_config.py` with `pre_mutation` / `post_mutation` | **Removed entirely** — zero references in the 3.5 package. Anything that hung off `context.config.test_command` or `context.skip` has no v3 equivalent; use `paths_to_mutate` / `do_not_mutate` / `# pragma: no mutate` instead. |
| `mutmut junitxml` | REMOVED. Read `mutants/` directly — `scripts/parse_results.py` does exactly that (`mutmut show all` is gone too; only `mutmut show <id>` remains). |
| `--no-progress` flag | REMOVED. |
| `.mutmut-cache` file | REPLACED by `mutants/` directory (file → folder). |
| `mutmut show <id>` (numeric) | Still works — IDs are still numeric. |
| `mutmut browse` TUI | Still present, expanded. Press `f` to retest a function, `m` for module, `r` for individual mutant. |

If user has CI scripts using removed flags, suggest the migration path. Don't reject the workflow.

## Odoo

⚠️ **There is no `test_command` / `runner` knob in mutmut 3.** `mutmut_config.py`, the `pre_mutation` hook, and `context.config.test_command` are all mutmut 1/2 API and were removed — grep the installed package, you will find zero hits. mutmut 3.5 hardcodes `runner = PytestRunner()` (there is a literal `# TODO: config/option for runner` above it). Any recipe that "configures odoo-bin as the runner" cannot work.

So the only way to mutation test an Odoo addon is **to make Odoo's tests run under `pytest.main()` via a `conftest.py` shim.** The payoff is large, because of how mutmut 3 actually executes:

- it calls `pytest.main()` **in-process**, with cwd set to `mutants/`;
- it then `os.fork()`s **once per mutant**;
- mutants are selected at **call time** via the `MUTANT_UNDER_TEST` env var (trampolines), so mutated modules are **never re-imported**;
- it runs **only the tests covering the mutated function** (from its stats phase).

Therefore: **boot the Odoo registry once, in the parent.** Every forked child inherits it copy-on-write. The ~20s registry load stops being a per-mutant cost, and per-mutant time collapses to just the covering tests. This is the "persistent DB" optimization, and the fork gives it to you for free.

**Working shim (verified against Odoo 19 and Odoo 17, mutmut 3.5):**

```python
# conftest.py at the directory you run mutmut from
import io, os, sys, unittest
from pathlib import Path

REPO = Path("/abs/path/to/repo")          # absolute! see gotcha 1
sys.path.insert(0, str(REPO / "vendor" / "odoo"))   # odoo is not pip-installed

import odoo, odoo.netsvc, odoo.sql_db
from odoo.modules.module import initialize_sys_path
from odoo.modules.registry import Registry          # Odoo 19 API; 17 has it too
from odoo.tools import config as odoo_config

DB = os.environ.get("MUTMUT_ODOO_DB", "your_test_db")
MUTANTS = Path(os.environ["MUTMUT_WORKDIR"]) / "mutants" / "odoo" / "addons"   # gotcha 9

def _is_addons_dir(path):                            # gotcha 11
    return path.is_dir() and any(
        (c / "__manifest__.py").exists() for c in path.iterdir() if c.is_dir()
    )

def pytest_configure(config):                        # MUST be named `config`
    if getattr(odoo, "_mutmut_booted", False):       # gotcha 12
        return
    odoo._mutmut_booted = True

    # the mutated copy must SHADOW the pristine addon: mutants first
    addons = [p for p in (MUTANTS,) if _is_addons_dir(p)] + [
        REPO / "custom-addons",
        REPO / "third-party-addons",
        REPO / "vendor" / "odoo" / "addons",
    ]
    args = [
        "-d", DB,
        "--addons-path=" + ",".join(str(p) for p in addons),
        "--max-cron-threads=0",
    ]
    # gotcha 10 — Odoo 19 has setup_logging=False; Odoo 17 does not.
    try:
        odoo_config.parse_config(args, setup_logging=False)
    except TypeError:
        odoo_config._parse_config(args)              # same work, minus init_logger
        try:
            odoo.netsvc.init_logger()
        except io.UnsupportedOperation:
            pass                                     # no fileno() under mutmut
    odoo_config["dev_mode"] = []                     # conf may set reload,xml,qweb
    initialize_sys_path()

    # gotcha 3
    from odoo.tests.common import BaseCase
    BaseCase.run = lambda self, result=None: unittest.TestCase.run(self, result)

    # gotcha 2
    os.register_at_fork(before=odoo.sql_db.close_all)

    Registry(DB)                                     # boot once, forks inherit
```

Verify the shim before launching anything long, by running the selection under bare pytest from the same directory (this is also gotcha 6's front-loading):

```bash
MUTMUT_WORKDIR=$PWD PYTHONPATH=<target> <target>/bin/python -m pytest test_mutation_targets.py -q
```

Two things to check in that output, not just the exit code: `Modules loaded` must appear **exactly once** (that is the fork-inherit optimisation working), and the test count must match what odoo-bin reports for the same classes.

Point mutmut at the addon and pull the whole thing into `mutants/`:

Lay the working directory out as `odoo/addons/<addon>` — a symlink to the real addon
is enough — because the path is what mutmut derives the mutant key from (gotcha 9):

```toml
[tool.mutmut]
paths_to_mutate = ["odoo/addons/tms/models/tms_settlement.py"]
# Odoo cannot load an addon without its manifest/views/siblings, and
# paths_to_mutate only copies the mutated files. also_copy brings the rest.
# copytree follows symlinks, so nothing is duplicated on disk.
also_copy = ["odoo/addons/tms", "conftest.py", "test_mutation_targets.py"]
do_not_mutate = ["*/migrations/*", "*/__manifest__.py", "*/tests/*"]
pytest_add_cli_args_test_selection = ["test_mutation_targets.py"]
```

**Gotcha 1 — the conftest is copied into `mutants/` and run from there.** Deriving paths from `Path(__file__).parent` resolves to `mutants/`, where only the mutated addon exists; every sibling addon then fails to import (`No module named 'odoo.addons.<x>'`). Use absolute paths.

**Gotcha 2 — `odoo.sql_db._Pool` is a module global and is NOT pid-aware.** Forked children inherit live Postgres sockets and corrupt each other's sessions. `os.register_at_fork(before=odoo.sql_db.close_all)` closes pooled connections in the parent right before each fork; `close_all()` empties the pool but keeps it usable, so both sides reconnect lazily. The registry is in-memory and stays inherited — you keep the speedup.

**Gotcha 3 — `BaseCase.run` is a runbot retry wrapper, not plain unittest.** It expects an `OdooTestResult` (`had_failure`, `soft_fail()`, `wasSuccessful()`); pytest passes `TestCaseFunction`, so *every* test errors with `AttributeError: 'TestCaseFunction' object has no attribute 'wasSuccessful'`. Overriding it to `unittest.TestCase.run` is faithful to odoo-bin: `_tests_run_count` is `ODOO_TEST_FAILURE_RETRIES + 1` = 1 by default, so the wrapper already runs each test once. Retries would be wrong here anyway — a retried test can mask a mutant it should have killed.

**Gotcha 4 — never point pytest at the addon's test file (even with `--pyargs`).** pytest derives the module name from the path and imports it as `tms.tests.<x>`; Odoo asserts model classes are imported under `odoo.addons.` and collection dies with `AssertionError: Invalid import of tms.models...`. Instead use a thin top-level file that imports the classes through the addons namespace — pytest collects imported `TestCase` subclasses:

```python
# test_mutation_targets.py
from odoo.addons.tms.tests.test_settlement import TestTmsSettlement  # noqa: F401
```

**Gotcha 5 — `mutate_only_covered_lines = true` calls `gather_coverage(PytestRunner(), ...)`,** i.e. it re-runs the suite under pytest. It works with this shim, but it is not the free win it is for plain pytest projects — measure before enabling.

**Gotcha 6 — the stats and clean phases are a hard gate, and they run `pytest -x`.** Before testing any mutant, mutmut collects stats and runs the selected tests unmutated; **one** failing test aborts the whole worker ~10 min in ("failed to collect stats. runner returned 1"). Worse, `-x` means everything after the first failure is never exercised, so you fix them one per 10-minute cycle. Front-load the gate: run the selection under pytest yourself before launching anything long.

Three things reliably fail this gate on Odoo, none of which are real defects:

- **`HttpCase` tests need the server odoo-bin starts.** Under bare `pytest.main()` nothing is listening and they all error at setUp (the TMS suite had ~212). Deselect by `issubclass`, not by name, so transitive subclasses go too:
  ```python
  def pytest_collection_modifyitems(config, items):
      from odoo.tests.common import HttpCase
      items[:] = [i for i in items
                  if not (isinstance(getattr(i, "cls", None), type) and issubclass(i.cls, HttpCase))]
  ```
  Remember the consequence: code covered *only* by HttpCase tests then reports "no tests".
- **Tests that resolve repo paths from `__file__`.** The addon runs from `mutants/`, so a test doing `dirname(__file__)/../../.claude/rules/x.md` looks inside `mutants/` and fails. `also_copy` whatever it reaches for.
- **A stale database.** See the fresh-DB note below.

**Gotcha 7 — build the test DB fresh; never reuse an old one.** A DB that has been carried forward with `-u` drifts: `-u` does not backfill data and cannot add a NOT NULL constraint over rows that violate it. On one inherited DB, 13 tests failed — a required-field test whose `assertRaises(IntegrityError)` never fired (column still nullable), a perf test over rows with NULLs in a newer required column, sequence tests whose `ir.sequence` rows had a stale `company_id`, and demo-data consistency tests. All 13 passed on a DB built with `-i <all addons> --without-demo=False`. Beyond the gate, a drifting schema also fakes mutant verdicts — so this is correctness, not convenience.

**Gotcha 8 — `--max-children > 1` does not parallelise Odoo; it serialises with extra waiting.** Every fork shares the parent's DB, and a shared `setUpClass` typically writes rows that are identical across suites (a fixture user with a unique login, a group link, an `ir.config_parameter`). `TransactionCase` rolls back only at *class* teardown, so each fork holds those row locks for its entire life and siblings block on `Lock|transactionid` — visible in `pg_stat_activity` as `wait_event_type=Lock`. Real parallelism needs **one database and one working directory per worker** (mutmut hardcodes `mutants/` relative to CWD), each running `--max-children=1`. Symlink the addons dirs into each worker dir so `paths_to_mutate` resolves without copying the tree, and point the conftest at its DB via an env var.

**Gotcha 9 (READ THIS ONE FIRST) — the addon must sit at `odoo/addons/<name>/` in the mutmut working directory, or EVERY mutant reports "no tests".** This is the broken stats mapping the main skill warns about, and here is the mechanism.

mutmut names a mutant from the **file path** under `mutants/`, with separators turned into dots. The trampoline records a stats hit under the module's **runtime `__name__`**. Odoo always imports an addon as `odoo.addons.<name>...`, whatever directory it was loaded from. So the natural layout produces two namespaces that never meet:

```
mutant key   custom-addons.payroll_management.models.payroll_slip.xǁPayrollSlipǁ_lock_rows_for_update
stats key    odoo.addons.payroll_management.models.payroll_slip.xǁPayrollSlipǁ_lock_rows_for_update
```

Every lookup misses, every mutant gets exit code 33, and the run finishes fast and clean with `🎉 0  🫥 1244` — which reads like "the tests cover nothing" rather than "the tool could not find the tests". Nothing in the output says the word *mismatch*.

Fix it with layout, not with a patch. In the directory you run mutmut from:

```
odoo/addons/<addon>  ->  symlink to the real addon
```
```toml
paths_to_mutate = ["odoo/addons/<addon>/models/x.py", ...]
also_copy       = ["odoo/addons/<addon>", "conftest.py", "test_mutation_targets.py"]
```

and point the conftest's shadowing addons-path entry at `mutants/odoo/addons`. `paths_to_mutate` resolves through symlinks and `also_copy`'s `copytree` follows them, so nothing is duplicated on disk.

**Verify before committing hours to a run:** after the stats phase, compare the two key spaces directly — they must share a prefix.

```python
import json, glob
stats = json.load(open("mutants/mutmut-stats.json"))["tests_by_mangled_function_name"]
meta  = json.load(open(glob.glob("mutants/**/*.py.meta", recursive=True)[0]))["exit_code_by_key"]
print(next(iter(stats)))   # runtime __name__
print(next(iter(meta)))    # path-derived key
```

**Gotcha 10 — `parse_config(..., setup_logging=False)` is Odoo 19 only.** Odoo 17's signature is `parse_config(self, args=None)` and it calls `odoo.netsvc.init_logger()` unconditionally. `init_logger` probes for colour with `os.isatty(handler.stream.fileno())`, and mutmut runs `pytest.main()` **in-process** behind a capture object that has no `fileno`, so the whole thing dies before a single test runs:

```
io.UnsupportedOperation: fileno
```

Call the private `_parse_config(args)` instead — it is exactly `parse_config` minus `init_logger` and `_warn_deprecated_options` — and then call `init_logger()` yourself inside `try/except io.UnsupportedOperation`. That keeps Odoo's log output on the bare-pytest pre-flight run, where the streams are real, and degrades quietly under mutmut where logging is cosmetic anyway. Do NOT skip `initialize_sys_path()`: `parse_config` was calling it for you.

**Gotcha 11 — an empty `mutants/` addons dir aborts the pre-flight run.** Odoo validates every `--addons-path` entry and rejects the WHOLE option, rather than skipping one entry, with `option --addons-path: the path '...' is not a valid addons directory`. On the first (pre-mutmut) verification run `mutants/custom-addons` does not exist yet. Filter the list with a "contains a subdirectory holding `__manifest__.py`" predicate, which is Odoo's own criterion, so the same conftest serves both the pre-flight and the real run.

**Gotcha 12 — `pytest_configure` fires once per `pytest.main()` call, and mutmut makes several in one process** (stats, then the clean run, then the mutants). Odoo's `_parse_config` is not re-entrant — it consumes entries of `self.options` — so the second call dies with `KeyError: 'load_language'`, reported only as `Failed to run clean test`. Guard the whole bootstrap with a module-level "booted" flag. This is not merely defensive: booting once is the entire point of the fork model, since the registry built on the first call is what every child inherits.

**Gotcha 13 — `python -m mutmut` breaks every trampoline.** See the warning in the main skill file: under `-m`, `__main__.py` is loaded as `__main__`, and the trampoline's `from mutmut.__main__ import record_trampoline_hit` re-executes it, hitting `set_start_method('fork')` twice → `RuntimeError: context has already been set`. It surfaces as `failed to collect stats. runner returned 3` with the traceback nested inside a test failure, which reads exactly like a defect in the code under test. Always invoke the console script.

**Performance:** without the registry-inherit trick, budget 30-180s per mutant (the number most Odoo write-ups quote). With it, per-mutant cost is just the covering tests — often a few seconds. Verify the parent boots the registry exactly once by watching for a single "Modules loaded" line.

**Concurrency:** `--max-children=1` for Odoo — see Gotcha 8. Raising it does not buy parallelism against a shared DB; the forks serialise on row locks and you pay a full Odoo process image per fork for the privilege. Scale out with one DB + one working directory per worker instead, each pinned to 1.

**Exclude patterns for Odoo:**
- `**/migrations/**` (Odoo data migrations)
- `**/__manifest__.py` (module declarations)
- `**/views/**.xml` (XML views)
- `**/data/**` (demo data, security CSV)
- `**/static/**` (web assets)
- `**/i18n/**` (translations)

## freezegun / any time-mocking in the test suite (affects EVERY project type)

**Symptom:** a large block of mutants comes back `timeout` (exit `-24`) — often *all* mutants of one file — while the same tests pass fine unmutated and finish in seconds.

**Cause:** mutmut sizes each mutant's timeout from its stats run:

```python
if run_time > (m.estimated_time_of_tests_by_mutant[mutant_name] + 1) * 15:
    os.kill(pid, signal.SIGXCPU)
```

`freeze_time` patches `time.perf_counter`, which is exactly what pytest's `CallInfo.duration` measures. A frozen test records an epoch-scale duration — in one real run, a single `@freeze_time` class produced `-1_781_398_136` seconds. One negative test duration makes the estimate negative, the threshold negative, and **every** child of that file is SIGXCPU'd the instant it starts. Verify with:

```python
import json; d = json.load(open("mutants/mutmut-stats.json"))["duration_by_test"]
print([ (v, k) for k, v in d.items() if v < 0 or v > 3600 ])   # expect []
```

**Second, independent cause of the same symptom:** the checker loops over *every mutant of the file* and compares each one's estimate against *every live child pid* of that file — `m` is per-file, not per-mutant. So the **lowest** estimate in the file governs all its children, and any function with no covering tests has an estimate of `0`, capping every child of that file at `(0 + 1) * 15 = 15s`.

**Fix — replace the checker with a flat wall-clock cap.** It still kills genuinely hung mutants, which is the only property worth keeping:

```python
# in conftest.py's pytest_configure, before mutmut starts the checker thread
import os
import signal
from datetime import datetime          # mutmut stores start_time_by_pid as datetime.now()
from time import sleep

import mutmut.__main__ as mm

def flat_cap(mutants, cap=300.0):
    def inner():
        while True:
            sleep(5); now = datetime.now(); seen = set()
            for m, _name, _res in mutants:
                with mm.START_TIMES_BY_PID_LOCK:
                    starts = dict(m.start_time_by_pid)
                for pid, t0 in starts.items():
                    if pid in seen: continue
                    seen.add(pid)
                    if (now - t0).total_seconds() > cap:
                        try: os.kill(pid, signal.SIGXCPU)
                        except ProcessLookupError: pass
    return inner

mm.timeout_checker = flat_cap   # resolved from module globals when _run() starts the thread
```

⚠️ Get those imports right. The checker runs in a thread, and a thread that dies on `NameError` does not abort the run — mutmut simply proceeds with **no timeout enforcement at all**, silently, which is the hang-every-mutant failure this section exists to cure. Verify the cap fires before trusting a long run.

**Do not** "fix" this by trusting the estimates: even without freezegun they are wrong whenever per-test setup is amortised in the stats run but paid in full by a child running one test (any class-scoped fixture — Odoo, Django `setUpTestData`, expensive pytest fixtures).

## Django

⚠️ mutmut 3 has no `manage.py test` runner — it runs **pytest** regardless (see the "only runner" note above). For Django that means `pytest-django` with a `DJANGO_SETTINGS_MODULE`, not `manage.py test`; configuring the latter silently changes nothing and stats collection fails. Common setup issues:

**Path resolution:** mutmut runs pytest from inside the `mutants/` directory. If your `tests_dir` uses a relative path, it might not resolve. Workaround:

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

**Settings file:** typically excluded. Use `do_not_mutate` (fnmatch on the file path) — `mutmut_config.py` / `pre_mutation` no longer exist in mutmut 3:
```toml
[tool.mutmut]
do_not_mutate = ["*/settings.py", "*/settings/*"]
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

mutmut ships a `HammettRunner` class, but in 3.5 **it is not reachable from config** — `_run()` hardcodes `runner = PytestRunner()`, and the `HammettRunner` line next to it is commented out. `[tool.mutmut] runner = "hammett"` is silently ignored (unknown keys are not validated), so you get pytest regardless while believing otherwise.

Treat pytest as the only runner unless you patch mutmut yourself.

## CI integration

Mutmut runs are slow → not for every PR.

**Recommended pattern:**
- Nightly cron job: full mutmut run on `main`, results posted as artifact.
- PR pre-push: scoped run on changed files only. Positional args to `mutmut run` are globs matched against **mangled mutant keys** (`calc.x_fn__mutmut_3`), not file paths — passing `src/calc.py` matches nothing and aborts. Glob on the module stem instead:
  ```bash
  globs=()
  while IFS= read -r f; do
      [ -n "$f" ] && globs+=("*$(basename "$f" .py)*")
  done < <(git diff --name-only origin/main HEAD -- '*.py' | grep -v test_)
  [ ${#globs[@]} -gt 0 ] && mutmut run "${globs[@]}"
  ```
  (`*stem*` can over-match same-named modules in different packages — acceptable for a pre-push heuristic; narrow `paths_to_mutate` for exactness.)

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
