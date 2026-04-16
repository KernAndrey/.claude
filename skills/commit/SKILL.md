---
name: commit
description: Smart commit — security scan, logical split, branch safety checks. Use when the user asks to commit changes.
---

# Rules
- Omit all Co-Authored-By and AI attribution from commit messages
- Write commit messages in English
- Use conventional commits

# Smart Commit

Safely commit staged and unstaged changes with security checks, logical splitting, and branch protection.

## Phase 1: Branch Safety

1. Run `git branch --show-current` to get the current branch.
2. If the branch is `main`, `master`, or `dev` — halt and inform the user:
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
This phase catches the gap before the review runs, saving a round
trip.

**Automatic classification — pick one path.**

### Path A: tests not required — pass silently

Skip this phase with no questions and no console output when any of
the following is true:
- No files of production code in staged changes (only `.md`, config,
  CI, migrations, `.json`, lockfiles).
- Only renames with identical signatures and bodies.
- Only edits inside test files.
- Only private `_prefixed` helpers with no new public entry point in
  the diff.

### Path B: tests required, tests present — pass silently

If the diff contains any of the following production-code signals:
- New public (not `_prefixed`) `def` / `class`.
- New branch (`if` / `elif` / `else` / `match` arm) with business
  logic.
- New `raise` or new exception type.
- Changed public function signature that alters behavior.

Then verify a matching test exists in the same staged set:
- `Glob` for test files across common conventions:
  `tests/**`, `test_*.py`, `*_test.py`, `*.test.ts`, `*.test.tsx`,
  `*.test.js`, `*_test.go`, `*Spec.*`, `*_spec.rb`.
- Confirm the staged diff adds / modifies a test that exercises the
  new unit.

Match present for every new-behavior unit → continue to Phase 4 with
no output.

### Path C: tests required, tests missing — BLOCK

Do not ask; do not propose options; do not negotiate. Print and halt:

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
- If a change exceeds 2000 lines (the hook rejects it wholesale), split by **vertical slice**: each commit = a portion of the feature + its tests.
  - ✅ Commit 1: `feat(auth): user model + model tests` / Commit 2: `feat(auth): login endpoint + endpoint tests` / Commit 3: `feat(auth): middleware + middleware tests`
  - ❌ Commit 1: all production code / Commit 2: all tests
- Valid standalone `test:` commits: test refactoring, adding coverage for previously untested existing code, migrating to a new test framework or fixtures.

## Phase 5: Lint Check

If any `.py` files are in the changeset:
1. Run `ruff check --fix <changed_py_files>`.
2. Run `ruff check <changed_py_files>` to verify.
3. If unfixable errors remain — report them and stop.

## Phase 6: Commit

For each logical commit:

1. Stage specific files: `git add <file1> <file2>` — blanket staging (`-A`, `.`) risks including secrets and binaries.
2. Write a commit message following **conventional commits** format:
   - `feat:`, `fix:`, `chore:`, `refactor:`, `docs:`, `test:`, `style:`, `perf:`, `ci:`
   - Short subject line (max 72 chars), imperative mood
   - Body if the change is non-trivial (separated by blank line)
   - Omit Co-Authored-By and AI attribution
   - **Write in English**
3. Commit using a HEREDOC to pass the message with `run_in_background: true` (the pre-commit AI review hook can take up to 20 minutes, exceeding Bash timeout):
   ```bash
   git commit -m "$(cat <<'EOF'
   feat: add user authentication flow
   EOF
   )"
   ```
4. Wait for the background commit to finish. Read the output file to check the result.
5. Run `git status` after each commit to verify success.

## Phase 7: Summary

After all commits are done, show:
- List of commits created (hash + message)
- Remaining uncommitted changes (if any)
- Push only when the user explicitly requests it.

## Reminders

- Run all commit hooks (no `--no-verify`).
- Create new commits rather than amending, unless the user explicitly asks.
- Use standard push (no `--force`).
- If a pre-commit hook fails or the AI review BLOCKs the commit, fix the reported issues, re-stage, and create a new commit.
- If the diff exceeds 2000 lines, the hook rejects it — split into smaller commits.
- The AI reviewer `tests` lens blocks any commit with new public behavior and no matching test. Phase 3.5 catches this early — keep code and tests in the same commit.
- When splitting a large feature, slice by **vertical** (each slice = code + its tests), never by layer (all code → all tests).
