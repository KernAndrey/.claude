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

## Phase 4: Analyze and Split

Review all changes and group them into logical commits. A logical commit is a cohesive set of changes that represents one idea:

- A bug fix + its tests
- A new feature + its tests (model + view + template + test)
- A refactoring + updated tests
- A config change
- Documentation updates (only if the user explicitly requested it)
- Standalone test improvements (refactoring existing tests, adding coverage for old code)

### Splitting rules

- If all changes are related to one thing — single commit is fine.
- If there are 2+ distinct changes — propose a split to the user with a short summary of each commit.
- Wait for the user to confirm or adjust the split before proceeding.
- Each commit contains one logical change — unrelated changes in one commit make bisect and revert impossible.
- Keep code and its tests in the same commit — splitting feat and test commits breaks bisect and revert.
- Only create standalone test commits for: test refactoring, adding coverage for previously untested existing code, or test infrastructure changes.

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
3. Commit using a HEREDOC to pass the message:
   ```bash
   git commit -m "$(cat <<'EOF'
   feat: add user authentication flow
   EOF
   )"
   ```
4. Run `git status` after each commit to verify success.

## Phase 7: Summary

After all commits are done, show:
- List of commits created (hash + message)
- Remaining uncommitted changes (if any)
- Push only when the user explicitly requests it.

## Reminders

- Run all commit hooks (no `--no-verify`).
- Create new commits rather than amending, unless the user explicitly asks.
- Use standard push (no `--force`).
- If a pre-commit hook fails, fix the issue, re-stage, and create a new commit.
