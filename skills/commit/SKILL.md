---
name: commit
description: Smart commit — security scan, logical split, branch safety checks. Use when the user asks to commit changes.
---

# Rules
- Omit all Co-Authored-By and AI attribution from commit messages
- Write commit messages in English
- Use conventional commits
- **Run Phase 3.5 before every `git commit`.** The hook's deterministic preflight enforces 100% coverage of new prod lines and ≥1 real assertion per new test. Then `mutmut-analyst` hardens those tests against weak assertions. Skipping these turns a 30-second local check into a 10-minute hook-review round trip.

# What the pre-commit hook does

1. Per-repo lint hook (ruff/pylint/xmllint where present) → gitleaks → semgrep → `python3 ~/.claude/review/hook.py`.
2. `review/hook.py` runs the deterministic preflight (`scripts/preflight_gate.py`): coverage gate (diff-cover, 100% of new prod lines) + assert gate (every new/changed test has ≥1 real assertion; no patching the unit under test). No LLM at this stage.
3. If the diff adds 400 or more production-code lines (`MAX_PROD_LINES`), the hook routes to the chunked-review pipeline and needs `.review/manifest.yaml` (auto-scaffolded on first hit).
4. AI reviewers from `review/config.py:PRIMARIES` run in parallel; arbiter (`claude/sonnet`) UPHOLDs CRITICAL clusters → commit BLOCKed. WARNINGs are non-blocking but addressed in the next commit.
5. Crashes fail-open. The global hook snapshots the index via `git write-tree` and restores it on exit if any sub-tool mutates it.

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

Two layers run: (a) you scan manually here, before `git commit`; (b) pre-commit hooks `gitleaks` (secrets in staged content) and `semgrep --config=auto` run automatically at commit time and either can BLOCK the commit. For semgrep false positives, drop `.semgrep-exclude-rules` at the repo root — one rule-id per line, `#` for comments. The hook passes each line as `--exclude-rule=<id>`. Example: `~/.claude/git-hooks/.semgrep-exclude-rules.example`.

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

The hook runs two checks before any LLM. Pass both locally before `git commit`.

### Step 1 — Cover new behavior

Run the project's test suite with coverage in XML form (e.g. `pytest --cov=. --cov-report=xml`). Every added production-code line must be exercised by a same-diff test; every new or modified test must contain ≥1 real assertion. Without a fresh `coverage.xml` the gate exits 2 and BLOCKs.

For bug-fix commits: the regression test must fail on the diff before the fix and pass after.

### Step 2 — Harden tests with mutation analysis

Invoke the `mutmut-analyst` skill on the changed production files. For every surviving non-equivalent mutant, add or strengthen a test until it dies. This catches assertion gaps the line-coverage gate cannot see.

### Step 3 — Stage and commit

The deterministic preflight then passes; the AI `tests` lens audits behavioral coverage and surfaces anything the first two steps missed. If it BLOCKs, write the missing tests in the next commit — never disable, mock around the unit under test, or split the production code out to land it bare.

### When the AI review hook blocks for missing tests

The only acceptable response is to write real tests for every flagged code path and address every `[CRITICAL]` and every `[WARNING]` in the **next** commit. The following are not fixes — they are cheating, and the next review will block them again or ship broken code:

- Deleting the production code so the branch disappears.
- Writing stub tests (`assert True`, mocking the very thing under test, tests that pass without exercising the branch).
- Splitting the commit so the untested code lands in a later one.
- Escalating to the user to skip tests — tests are not negotiable.

Partial fixes only re-trigger the same block. Write the tests.

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
- If `git diff --cached` adds 400 or more production-code lines (`MAX_PROD_LINES`), the hook routes to the chunked-review pipeline and needs `.review/manifest.yaml`. The hook auto-scaffolds it on first hit; fill in `chunks:` (group files by meaning, ≤400 prod lines per chunk, ≤6 chunks — `MAX_CHUNKS`) and re-run `git commit`. Tests, docs, configs, lock-files, and removals do not count toward the limit.
- Validate the manifest before committing — `validators/manifest.py` is a library, not a CLI, so run it as:

  ```bash
  cd "$(git rev-parse --show-toplevel)" && PYTHONPATH="$HOME/.claude/review" python3 -c "
  import subprocess
  from pathlib import Path
  from validators.manifest import validate
  diff = subprocess.run(['git', 'diff', '--cached'], capture_output=True, text=True).stdout
  print(validate(Path('.review/manifest.yaml').read_text(), diff, Path.cwd()).to_text())
  "
  ```

  Three failures recur:
  - `uncovered_file` — every file in the staged diff must be claimed by exactly one chunk.
  - `missing_related_file` — `default_related_files` must point at paths that exist; a task file moved between `tasks/N-*/` folders breaks it.
  - `stale_hash` — re-staging changes the diff hash, so regenerate the manifest after any re-stage (`python3 ~/.claude/review/scripts/scaffold_manifest.py`).
- Valid standalone `test:` commits: test refactoring, adding coverage for previously untested existing code, migrating to a new test framework or fixtures.

## Phase 5: Lint Check

If any `.py` files are in the changeset:
1. Run `ruff check --fix <changed_py_files>`.
2. Run `ruff check <changed_py_files>` to verify.
3. If unfixable errors remain — report them and stop.

## Phase 6: Commit

For each logical commit, run these steps **in order, per commit** (not once for the whole phase):

1. **Stage specific files**: `git add <file1> <file2>` — blanket staging (`-A`, `.`) risks including secrets and binaries.
   - `.review/` paths must never be staged. The hook hard-blocks. Add `.review/` to `~/.config/git/ignore` and set `git config --global core.excludesFile ~/.config/git/ignore`.
2. **Stash-guard the unstaged tail** — gives linters and security scanners a working tree that matches the index exactly, so the commit captures only what was reviewed:
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

Linters in the per-repo hooks operate on file paths in the working tree, not on staged blobs. If the working tree mixes staged content with unstaged edits to the same file, the verifier sees the mix — it can flag the unstaged edits and block a commit whose staged content is actually clean. Stashing the unstaged tail gives the hooks a working tree that matches the index exactly, so verification reflects what will actually be committed.

Historical note: untracked files leaking into commits and `error: invalid object … Error building trees` corruption were both blamed on the pre-commit framework (https://pre-commit.com) for a long time, but the dominant source turned out to be the AI review backend (`opencode`), which is an agent with bash access and freely runs `git add` / `git stash` during its investigation of the diff. The per-repo `.git/hooks/pre-commit` files in `hubcraft-console`, `hubcraft-tms`, `odoo`, and `usko_internal_webapp` were converted to verify-only vanilla bash (no auto-fix, no re-stage), and `~/.claude/git-hooks/pre-commit` now snapshots the index at hook entry via `git write-tree` and restores it on exit via `git read-tree`, rolling back any sub-tool mutation regardless of source. Do not run `pre-commit install` in those repos — it would reintroduce the framework wrapper.

## Phase 6.5: Review WARNINGs

After each successful commit, list every `[WARNING]` from the AI review and assign one decision per item:

- **Fix now** — default when no specific reason to accept exists.
- **Accept because `<concrete reason>`** — must be a concrete reason, not a hand-wave.

Surface the list and decisions to the user before moving on to the next commit or summary.

## Phase 7: Summary

After all commits are done, show:
- List of commits created (hash + message)
- Remaining uncommitted changes (if any)
- Push only when the user explicitly requests it.

## Phase 8: Troubleshooting — index corruption

If `git commit` reports `invalid object <sha> … Error building trees`, or `git fsck` lists `missing blob` entries, the index has been poisoned. The most common cause is a manual `pre-commit run` (or `pre-commit run --hook-stage pre-commit`) executed against a dirty working tree — its `git apply --index` writes blob hashes into the index without ensuring the blobs are written to `.git/objects/`. The commit-time hook in those repos no longer goes through the framework, so this should only show up after manual invocation. Recover:

```bash
git reset                          # reset index to HEAD; working tree untouched, files safe
git fsck --no-dangling             # expect empty output — index is clean
rm -f ~/.cache/pre-commit/patch*   # drop stale pre-commit patches; it regenerates as needed
```

Then re-stage the intended files and run Phase 6 again.

If `git fsck` still reports missing blobs after `git reset`, stop and report to the user — the object DB itself is damaged and needs manual intervention.

## Reminders

- Run all commit hooks (no `--no-verify`).
- Create new commits rather than amending, unless the user explicitly asks.
- Use standard push (no `--force`).
- If a pre-commit hook fails or the AI review BLOCKs the commit, fix the reported issues, re-stage, and create a new commit.
- Diffs of 400 or more added prod lines route to the chunked path and require `.review/manifest.yaml`.
- Two stages run before BLOCK: deterministic preflight (line coverage + asserts, no LLM), then AI lens. Pass both by writing real tests, not by trimming scope.
- When splitting a large feature, slice by **vertical** (each slice = code + its tests), never by layer (all code → all tests).
- Before `git commit` the working tree must contain only staged changes. The skill stashes the unstaged tail in Phase 6 — do not skip that step: it keeps linters and re-stages from leaking unstaged or untracked content into the commit.
