---
name: commit
description: Smart commit — security scan, logical split, branch safety checks. Use when the user asks to commit changes.
---

# Rules
- Omit all Co-Authored-By and AI attribution from commit messages
- Write commit messages in English
- Use conventional commits
- **Preflight test coverage before every commit.** The AI review hook
  blocks commits with untested new public behavior. Walk the staged
  diff in Phase 3.5, map each new-behavior unit to a same-diff test
  that asserts on it, and write the missing tests before `git commit`.
  Skipping this turns a 30-second local check into a 10-minute
  hook-review round trip.

# Smart Commit

Safely commit staged and unstaged changes with security checks, logical splitting, and branch protection.

## Phase 1: Branch Safety

1. Run `git branch --show-current` to get the current branch.
2. If the branch is `main` or `master` — halt and inform the user:
   > You are on `{branch}`. Per project rules, direct commits to shared branches are not allowed. Create a feature branch first.
   Suggest a branch name based on the changes and ask the user to confirm. Wait for the user to switch to a feature branch before proceeding.

## Phase 2: Gather Changes

1. Run `git status` (without `-uall` flag).
2. Run `git diff` (unstaged) and `git diff --cached` (staged).
3. If there are no changes at all — inform the user and stop.

## Phase 3: Security Scan

**This phase is mandatory.**

Scan ALL changed and new files (both staged and unstaged) for secrets and sensitive data. For each changed file, read its diff and check for:

### Patterns to detect

- **API keys / tokens:** strings matching patterns like `sk-`, `pk_`, `api_key`, `token`, `bearer`, `ghp_`, `gho_`, `github_pat_`, `xoxb-`, `xoxp-`, `AKIA` (AWS), `ya29.` (Google OAuth)
- **Passwords:** `password`, `passwd`, `secret` assignments with literal values (not references to env vars)
- **Private keys:** `-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----`
- **Connection strings:** URIs containing credentials (`://user:pass@`)
- **Environment files:** `.env`, `.env.local`, `.env.production` files being committed
- **Certificate files:** `.pem`, `.key`, `.p12`, `.pfx` files
- **Cloud credentials:** AWS credentials, GCP service account JSON, Azure connection strings
- **Hardcoded IPs / internal URLs** that look like staging/production infrastructure

### How to scan

- Read the full diff output — check file content, not just filenames.
- For new untracked files, read the file content.
- Check variable names AND their values.

### If secrets are found

Halt. Report each finding:

> **Security issue found:**
> - `path/to/file.py:15` — contains what appears to be an AWS access key (`AKIA...`)
> - `.env.production` — environment file with database credentials

Ask the user how to proceed. Suggest:
- Remove the secret and use an environment variable instead
- Add the file to `.gitignore`
- If it's a false positive, the user can confirm and you proceed

Proceed only after explicit user approval for each detected secret.

## Phase 3.5: Test Coverage Gate

The AI reviewer (`tests` lens) enforces 100% coverage of new public
branches. A commit with new behavior and no matching test is blocked.
This phase catches the gap **before** the review runs, saving a ~10
minute round trip per missed unit.

The lens's canonical rules are in
`/home/kern/.claude/review/prompts/tests.md`. This phase applies the
same rules locally; any mismatch means the review will block.

**Procedure — do this in one pass, in order.**

### Step 1 — Enumerate every new-behavior unit

Walk the staged diff end-to-end. For each `+` line, classify it. The
following are new-behavior units:

- New public (not `_prefixed`) `def` / `class` / method with business
  logic.
- New branch (`if` / `elif` / `else` / `match` arm) with distinct
  runtime effect a caller can observe (return value, state write,
  response code, exception).
- New `raise`, new exception type, new failure return value.
- Changed public function signature that alters behavior.

List them by name. This list is the contract you will fulfil with
tests.

### Step 2 — Apply the exclusion categories

For each listed unit, check whether it falls into one of these
exclusion categories (same as the review lens, same phrasing). If
yes, cross it off the list.

1. **Non-production file** — `.md`, config, CI, migration, `.json`,
   lockfile, shell script.
2. **Pure rename** — identical signature and body.
3. **Test file edit only** — tests don't test tests.
4. **Private `_prefixed` helper** with no new public entry point.
5. **Declarative / metadata change** — `@api.depends` path expansion,
   field kwargs (`tracking=`, `index=`, `copy=`), class-level
   declarative attrs, `_()` wrappers, defensive branch behind a
   declared invariant (`required=True`, `@api.constrains`, NOT NULL).
6. **Stdlib/framework-delegated error handler** *(category A)* — ALL
   FOUR: `try` body is exactly one stdlib call (`Path.read_text`,
   `Path.mkdir`, `Path.unlink`, `os.replace`, `shutil.copy2`,
   `input()`, HTTP-client `raise_for_status`); `except` body is
   `return` / `pass` / `continue` / `die(msg)` / `_logger.<level>`
   only (no state write, no exception-type change); exception class
   is hard-to-fixture (`OSError`, `FileNotFoundError`,
   `PermissionError`, `EOFError`, `KeyboardInterrupt` — NOT
   `JSONDecodeError` / `UnicodeDecodeError` / `ValueError` / `KeyError`,
   which are fixture-testable); the enclosing function is called by
   at least one test in the diff.
7. **Log-only observer branch** *(category B)* — `if` / `elif` / `else`
   branch body is only `_logger.<level>(...)` calls, no return, no
   state write, no raise, no response-code change, no counter. Does
   NOT apply to `except` blocks or to branches that fall through to
   a state-writing path.
8. **Idempotent bootstrap wrapper** *(category C)* — whole function
   body is one idempotent stdlib setup call (`mkdir(exist_ok=True)`,
   `Path.touch`, `logging.getLogger`) plus optional category-A
   handler.
9. **Declarative view/XML conditional** *(category D)* —
   single-attribute `invisible=` / `readonly=` / `required=` /
   `decoration-*` / `attrs=` in Odoo XML, Jinja `{% if %}` show/hide.
   Does NOT apply when the attribute wires `context=` or
   `button type="object"` that reshapes state.
10. **Cosmetic-only outbound kwarg** *(category E)* — one kwarg at
    one call site; the kwarg is purely cosmetic (formatting strings,
    log labels, descriptions); the kwarg is NOT `audience=` /
    `issuer=` / `verify=` / `algorithms=` / `scope=` / `client_id=`
    (security), NOT `identity_key=` / `dedupe_key=` /
    `idempotency_key=` (idempotency), NOT `channel=` / `queue=` /
    `topic=` / `routing_key=` (routing), NOT `sudo=` /
    `allowed_company_ids=` / `uid=` (authz scope).

For borderline cases (3-of-4 category-A conditions; unclear kwarg
class; audit-trail log-only branch), **write the test** rather than
claim the exclusion. A `[WARNING]` from the hook is cheaper than
arguing.

### Step 3 — For each remaining unit, verify a matching test exists

`Glob` for test files: `tests/**`, `test_*.py`, `*_test.py`,
`*.test.ts`, `*.test.tsx`, `*.test.js`, `*_test.go`, `*Spec.*`,
`*_spec.rb`.

Confirm the staged diff adds or modifies a test that **exercises**
the unit. A test exercises a unit when it both:

- Triggers execution of the unit — either calls the new function/
  method by name, or supplies input that makes the new branch fire.
- Makes an assertion about the resulting behavior — return value,
  state change, raised exception, response code.

A test that does only one of these does NOT exercise the unit.
Disqualifiers:

- `assert True` / `pass` body.
- Mocks the thing under test (patching `new_fn` itself, then calling
  `new_fn` — you asserted against the mock, not the code).
- Reference to the unit in a comment or docstring but no call.
- Adds a fixture that uses the unit but no assertion about it.

For bug-fix commits: the regression test must fail on the diff before
the fix and pass after. Run the test locally before committing.

### Step 4 — Decide

- Every remaining unit has a matching exercising test → pass silently,
  continue to Phase 4.
- At least one unit has no matching exercising test → BLOCK:

> ❌ Commit blocked — test coverage required:
> - `path/to/mod.py::new_fn` (new public function, no matching test)
> - `path/to/mod.py:142` (new `elif ctx.role == "admin"` branch, no assertion)
>
> Add tests in the same commit. 100% coverage is enforced; tests cannot be deferred. The AI reviewer (`tests` lens) will block this commit anyway — catching it here saves a round trip.

The only legal escape is an inline comment on or immediately above
the new unit:
```python
# review-note: covered by tests/integration/test_foo.py::test_bar
```
Where the referenced test path actually exists AND contains an
assertion on the new behavior. Verify with `Read` before honoring.
Unverifiable references do not unblock the commit.

## Phase 4: Analyze and Split

Review all changes and group them into logical commits. A logical commit is a cohesive set of changes that represents one idea:

- A bug fix + regression test (both required in one commit)
- A new feature + its tests (model + view + template + test)
- A refactoring + updated tests (if behavior changes)
- A config / CI / infrastructure change (tests optional, justify in body)
- Standalone `test:` commits (see exceptions below)
- Documentation updates (only if the user explicitly requested it)

### Splitting rules

- If all changes are related to one thing — single commit is fine.
- If there are 2+ distinct changes — propose a split to the user with a short summary of each commit.
- Wait for the user to confirm or adjust the split before proceeding.
- Each commit contains one logical change — unrelated changes in one commit make bisect and revert impossible.
- Keep production code and its tests in the same commit. Splitting into "all code" + "all tests" is forbidden: the first commit hits the `tests` lens with no coverage and gets blocked.
- If a change exceeds 3000 lines (the hook rejects it wholesale), split by **vertical slice**: each commit = a portion of the feature + its tests.
  - ✅ Commit 1: `feat(auth): user model + model tests` / Commit 2: `feat(auth): login endpoint + endpoint tests` / Commit 3: `feat(auth): middleware + middleware tests`
  - ❌ Commit 1: all production code / Commit 2: all tests
- Valid standalone `test:` commits: test refactoring, adding coverage for previously untested existing code, migrating to a new test framework or fixtures.

## Phase 5: Lint Check

If any `.py` files are in the changeset:
1. Run `ruff check --fix <changed_py_files>`.
2. Run `ruff check <changed_py_files>` to verify.
3. If unfixable errors remain — report them and stop.

## Phase 6: Commit

For each logical commit, run these steps **in order, per commit** (not once for the whole phase):

1. **Stage specific files**: `git add <file1> <file2>` — blanket staging (`-A`, `.`) risks including secrets and binaries.
2. **Stash-guard the unstaged tail** — critical to avoid pre-commit index corruption (see Phase 8):
   ```bash
   git stash push -u -k -m "commit-skill-wip-$(date +%s)"
   ```
   - `-u` covers untracked files; `-k` keeps staged files in the working tree so hooks see real content.
   - If output is `No local changes to save`, remember **stashed=false** and skip step 6. Otherwise **stashed=true**.
3. **Verify the working tree is clean of unstaged content**: run `git status`. The only section present must be `Changes to be committed:`. If `Changes not staged for commit:` or `Untracked files:` is still there — halt and investigate (something unusual, e.g. a submodule, .gitignore edge case). Do not proceed.
4. **Write a commit message** following **conventional commits** format:
   - `feat:`, `fix:`, `chore:`, `refactor:`, `docs:`, `test:`, `style:`, `perf:`, `ci:`
   - Short subject line (max 72 chars), imperative mood
   - Body if the change is non-trivial (separated by blank line)
   - Omit Co-Authored-By and AI attribution
   - **Write in English**
5. **Commit** using a HEREDOC with `run_in_background: true` (the pre-commit AI review hook can take up to 20 minutes, exceeding Bash timeout):
   ```bash
   git commit -m "$(cat <<'EOF'
   feat: add user authentication flow
   EOF
   )"
   ```
6. **Wait** for the background commit to finish. Read the output to check the result.
7. **Restore the stashed tail** if stashed=true (do this whether the commit succeeded or failed — the user's WIP must not be lost):
   ```bash
   git stash pop
   ```
   If `git stash pop` reports a conflict — halt. Show the user `git status` and `git stash list`, do not start the next commit. The stash ref stays in the list so the user can resolve manually.
8. **Verify**: `git status` to confirm the commit landed and the WIP was restored.

Repeat steps 1–8 for each subsequent logical commit.

### Why the stash-guard exists

The pre-commit framework has a bug where, when unstaged changes are present at commit time, it generates a binary patch, resets the working tree to HEAD, runs hooks (which can reformat staged files via `ruff-format`), then restores the patch with `git apply --index`. The `--index` flag writes blob hashes from the patch header into `.git/index` without ensuring those blobs are written to `.git/objects/`. Result: the index references missing blobs, and subsequent commits fail with `invalid object … Error building trees`. Keeping the working tree clean of unstaged content at commit time prevents pre-commit from entering its stash/restore path, which is the only way to avoid this corruption reliably.

## Phase 7: Summary

After all commits are done, show:
- List of commits created (hash + message)
- Remaining uncommitted changes (if any)
- Push only when the user explicitly requests it.

## Phase 8: Troubleshooting — index corruption

If `git commit` reports `invalid object <sha> … Error building trees`, or `git fsck` lists `missing blob` entries, the index has been poisoned by the pre-commit stash/restore bug (see Phase 6 rationale). Recover:

```bash
git reset                          # reset index to HEAD; working tree untouched, files safe
git fsck --no-dangling             # expect empty output — index is clean
rm -f ~/.cache/pre-commit/patch*   # drop stale pre-commit patches; it regenerates as needed
```

Then re-stage the intended files and run Phase 6 again. The stash-guard there prevents recurrence.

If `git fsck` still reports missing blobs after `git reset`, stop and report to the user — the object DB itself is damaged and needs manual intervention.

## Reminders

- Run all commit hooks (no `--no-verify`).
- Create new commits rather than amending, unless the user explicitly asks.
- Use standard push (no `--force`).
- If a pre-commit hook fails or the AI review BLOCKs the commit, fix the reported issues, re-stage, and create a new commit.
- If the diff exceeds 3000 lines, the hook rejects it — split into smaller commits.
- The AI reviewer `tests` lens blocks any commit with new public behavior and no matching test. Phase 3.5 catches this early — keep code and tests in the same commit.
- When splitting a large feature, slice by **vertical** (each slice = code + its tests), never by layer (all code → all tests).
- Before `git commit` the working tree must contain only staged changes. The skill stashes the unstaged tail in Phase 6 — do not skip that step: it is the workaround for a pre-commit framework bug where `git apply --index` writes blob hashes to the index without writing the blobs themselves.
