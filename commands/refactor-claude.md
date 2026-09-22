---
description: Refactor .claude/ and CLAUDE.md to shrink the always-loaded prompt budget without losing information
argument-hint: [optional scope hint, e.g. "rules-only" or "claude-md-only"]
---

# Refactor Claude Code prompts to reduce token budget

Goal: shrink the always-loaded prompt budget for `.claude/` and `CLAUDE.md` substantially without losing useful information. Strategy: move reference-grade material into `.claude/refs/` (NOT under `.claude/rules/` — see Critical Trap), path-scope narrow rule files via `paths:` frontmatter, deduplicate across files, and tighten verbose rationale.

If `$ARGUMENTS` contains a scope hint, restrict the pass to that scope. Otherwise do a full pass.

## Loading-mode mechanics (per https://code.claude.com/docs/en/memory)

- `.claude/rules/*.md` is recursively auto-discovered
- Rule files WITHOUT `paths:` frontmatter load into every session
- Rule files WITH `paths:` frontmatter load only when Claude reads a matching file
- `.claude/commands/` and `.claude/skills/` are on-demand, not part of the always-loaded budget
- `CLAUDE.md` always loads

## Step 1 — Inventory and classify

Walk the project and produce this table for every prompt-instruction file:

| Path | Lines | Has `paths:` frontmatter? | Loading mode |
|------|-------|---------------------------|--------------|
| `CLAUDE.md` | … | n/a | always-loaded |
| `.claude/rules/foo.md` | … | yes/no | path-scoped / always-loaded |
| `.claude/commands/*.md` | … | (skill metadata) | on-demand only |

## Step 2 — Find the four kinds of fat

1. **Hard duplicates** — same rule stated in 2+ files. Grep shared keywords across always-loaded files.
2. **Reference-grade content** inside rule files — lookup tables, code examples >20 lines, canonical-import maps, widget catalogs, matchers tables. Cheat sheets, not behavior rules.
3. **Verbose rationale** — paragraphs that re-explain a rule in narrative form after stating it as a bullet. "Why:" sections that just restate the rule.
4. **Narrow always-loaded files** — files semantically scoped to one workflow (test coverage checklist, one specific UI review). Candidates for `paths:` frontmatter.

## Step 3 — Propose a plan, check in before any edits

Before writing anything, draft:

- For each duplicate: which file owns the canonical version; replace others with one-line cross-reference.
- For each reference-grade block: destination path under `.claude/refs/`.
- For each verbose rationale: target line count after tightening.
- For each narrow always-loaded file: proposed `paths:` glob, verified against actual file paths in the project.

**CRITICAL TRAP**: `.claude/rules/` is recursively auto-discovered. So `.claude/rules/refs/foo.md` (or `.claude/rules/references/foo.md`) without frontmatter still loads into every session — defeats the goal. Put extracted reference material in `.claude/refs/` instead, *outside* the auto-discovery zone. It loads only when Claude explicitly Reads a file referencing it.

Pointers from rule files take this form:
> Full table — `.claude/refs/<name>.md`. Read when …

Show the plan to the user. Get an explicit yes before writing.

## Step 4 — Execute as small commits

One concern per commit. Order (cheapest leverage first):

1. **Scaffold** `.claude/refs/README.md` (index file, no content moves yet).
2. **Path-scope narrow always-loaded rules** by adding `paths:` frontmatter — no content edits, biggest token savings per minute of work.
3. **CHECKPOINT** — user starts a fresh session, runs `/memory`, confirms:
   - Path-scoped files dropped out of the always-loaded list
   - They reappear when Claude reads a matching file
   - No `.claude/refs/*.md` shows in `/memory` at all
   
   Do NOT proceed past this checkpoint without observed confirmation. Locally-valid YAML is not the same as Claude Code honoring it.

4. **Extract reference-grade content** from each large path-scoped rule file into `.claude/refs/`. One commit per source file. Keep load-bearing summaries inline (5–10 highest-leverage facts that guide decisions); move lookup tables and verbose code examples.
5. **De-duplicate** — replace duplicate bullet lists in always-loaded files with cross-references to canonical owners.
6. **Tighten verbose rationale** — compress prose paragraphs to 1–3 sentences. Preserve facts and incident references; cut narrative repetition.
7. **Slim CLAUDE.md** to under 100 lines. Extract DB setup, deployment, full lifecycle workflows to `.claude/refs/`. Keep a 5-line command sequence inline so Claude knows the commands exist.

## Step 5 — Commit-message hygiene

If the project has an AI-review pre-commit hook, every commit body must include:

> Diff is move-only — no information lost. Source content preserved verbatim in `<destination-file>`. AI-review: this is a content relocation, not a content change.

List each extracted block with its destination so a reviewer can audit by eye.

If hooks are slow (some take 10–20 minutes), run `git commit` in the background. Don't pile up new commits before the previous one's hook finishes.

## Step 6 — Verification before push

In a fresh session:

1. `/memory` — confirm the always-loaded list contains only intended files. Note line counts; sum them.
2. Open Read on a file matching each `paths:` glob — confirm path-scoped files appear.
3. Cross-reference integrity:
   ```bash
   grep -roE '\.claude/refs/[a-z0-9_-]+\.md' .claude/rules/ CLAUDE.md \
     | awk -F: '{print $2}' | sort -u \
     | while read f; do test -f "$f" && echo "OK: $f" || echo "MISSING: $f"; done
   ```
4. Diff always-loaded total before/after. Report line count delta and approximate token savings.

Push only after verification passes. Single PR with all commits, branch like `chore/claude-prompt-compression`.

## Refuse to do

- Delete content because it doesn't fit a tighter rule.
- Move load-bearing rules (the kind written because Claude kept getting them wrong) to `.claude/refs/`. References are for lookup, not behavior modification.
- Replace a 20-line code example with a vague summary like "follow the standard pattern" — loses information.
- Put `.claude/refs/` *under* `.claude/rules/`. Re-read the Critical Trap.
- Mark the verification checkpoint complete without an observed change in load state.

## What does NOT compress well — keep inline

- Load-bearing rules (the 5–10 highest-leverage facts) — these exist because Claude forgets them; making them on-demand defeats the purpose.
- Critical absolute prohibitions — keep at top or bottom (primacy/recency).
- Project-specific architectural decisions that contradict common patterns — highest-value rules in the file.

## Output expectations

- Single branch, 8–12 atomic commits
- Always-loaded line count drops 30–50%
- `.claude/refs/` exists with one README index plus extracted material
- All cross-references resolve
- No content lost — every fact, code example, and incident reference still exists somewhere

## Files that typically take this refactor well

- **CLAUDE.md** — almost always >100 lines; usually has DB setup or deployment commands that belong in `refs/`.
- **`.claude/rules/guidelines.md`** (or equivalent project-conventions file) — usually duplicates content in path-scoped rule files.
- Any rule file >400 lines — has reference-grade tables and code examples that compress well.
- Any always-loaded rule file with a narrow subject (one specific testing pattern, one UI workflow, one review checklist) — path-scope candidate.