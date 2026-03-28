# /review-commit — Deep Commit Safety Review

## Purpose

Perform a thorough safety-focused review of the latest commit (typically a merge commit). The primary goal is to detect regressions, new bugs, broken logic, and unintended side effects.

## Usage

```
/review-commit <commit-ref>
```

`<commit-ref>` — commit hash (full or short), branch name, or any valid git ref. Examples:
- `/review-commit a1b2c3d`
- `/review-commit feature/carrier-settlements`

## Instructions

1. Resolve the provided `$COMMIT` ref. Run `git log -1 --format="%H %s %P" $COMMIT` to get the hash, message, and parent(s).
2. Determine the diff range:
   - **Merge commit** (2+ parents): use `git diff $COMMIT^1..$COMMIT` (diff against the branch being merged into).
   - **Regular commit**: use `git diff $COMMIT~1..$COMMIT`.
3. Run the diff command with `--stat` first to get an overview of changed files.
4. Run the full diff. If it is too large, review file-by-file using `git diff <range> -- <path>`.

## Review Checklist

For every changed file, analyze the diff against the following criteria:

### Critical — Bugs & Regressions
- **Removed or altered safety checks** (validations, permission guards, try/except blocks)
- **Changed function signatures** that break existing callers
- **Removed or weakened constraints** (SQL constraints, ORM domain filters, required fields)
- **Off-by-one errors, wrong operators, inverted conditions**
- **Missing return statements** or changed return types
- **Broken exception handling** (bare except, swallowed errors, wrong exception class)
- **Race conditions** introduced by reordering or removing locks
- **Hardcoded values** replacing configurable ones

### Logic & Data Integrity
- **Changed query domains/filters** that may return wrong recordsets
- **Altered write/create values** that corrupt data
- **Removed or changed `_sql_constraints`** or `_check` methods
- **Modified compute/depends** that break field recalculation chains
- **Changed `onchange` / `ondelete`** behavior
- **Sudo/elevated privileges** added without justification

### Odoo-Specific (when applicable)
- **Security XML rules** removed or loosened
- **Access rights (ir.model.access)** changed
- **Overridden methods** missing `super()` call
- **Changed view inheritance** (`xpath`) targeting wrong nodes
- **Cron jobs** with altered intervals or domain filters
- **Workflow/status transitions** with missing or wrong conditions

### General Quality
- **Dead code** introduced (unreachable branches, unused imports/variables)
- **Debugging artifacts** left in (`print()`, `_logger.info` with sensitive data, `breakpoint()`, `pdb`)
- **Performance regressions** (N+1 queries, missing `sudo()` cache, unbounded loops)
- **Missing or broken tests** for changed functionality

## Output Format

Structure your review as follows:

### 1. Commit Summary
Brief description of what the commit does (1-3 sentences).

### 2. Risk Assessment
Rate overall risk: **🟢 Low** / **🟡 Medium** / **🔴 High**

### 3. Findings
For each issue found:
- **File**: `path/to/file.py`
- **Line(s)**: relevant line numbers from the diff
- **Severity**: 🔴 Critical / 🟡 Warning / 🔵 Info
- **Description**: What is wrong and why it matters
- **Suggestion**: How to fix it (if applicable)

### 4. Questions
List anything unclear or suspicious where you need confirmation from the developer. For example:
- "Was the removal of `_check_duplicate()` in `sale_order.py` intentional?"
- "The domain filter on `stock.picking` changed from `state != 'done'` to `state = 'draft'` — is this narrowing deliberate?"

**If a change looks potentially destructive, ASK. Assume nothing about intent.**

### 5. Verdict
One of:
- ✅ **Safe to keep** — No issues or only minor info-level notes.
- ⚠️ **Needs attention** — Has warnings that should be reviewed before deploying.
- 🚫 **Risky** — Contains critical issues that likely introduce bugs or regressions.