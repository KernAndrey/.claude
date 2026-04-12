Interactively collect information about a bug and generate a standardized bug report.

## Instructions

You are helping a QA engineer file a bug report. Your goal is to produce a clear, actionable, developer-ready bug report. All output in the report must be in **English**, regardless of the language the reporter uses.

### Phase 1 — Initial Context

1. Read `.tasks.toml` in project root. Get `dir` (default: `tasks`). If `.tasks.toml` is missing, tell the user to run `/task-init` first and stop.
2. Read the bug counter from `{dir}/bugs/.counter`. If the file does not exist, create `{dir}/bugs/` directory and `{dir}/bugs/.counter` with `0`.
3. The user's initial description is below in `$ARGUMENTS`. If empty, ask the user to describe what happened.

### Phase 2 — Codebase Exploration

4. Based on the description, search the codebase for relevant files: models, views, routes, components, services — anything that could be related to the reported behavior. Read the most relevant files to understand the area.
5. Form a mental model of which components are involved and what could go wrong.

### Phase 3 — Clarifying Questions

6. Ask clarifying questions **one at a time** using `AskUserQuestion`. Adapt your questions based on what you found in the codebase and what information is still missing. Typical questions (ask only what's relevant — skip what's already clear):

   - **Steps to reproduce** — ask for exact steps if the initial description is vague.
   - **Expected vs actual behavior** — if not obvious from context.
   - **Screenshots** — ask the user to paste/attach screenshots if the bug is visual or UI-related. Say: "Can you attach a screenshot showing the problem?"
   - **Reproducibility** — always, sometimes, or once?
   - **Environment** — only ask if the bug looks browser/OS/device-specific (frontend, CSS, responsive issues). For backend or logic bugs, skip this.
   - **Severity** — propose a severity level based on your understanding and ask the user to confirm or change:
     - `critical` — app crash, data loss, security vulnerability, complete feature block
     - `high` — major feature broken, no workaround
     - `medium` — feature partially broken, workaround exists
     - `low` — cosmetic, minor inconvenience

   Stop asking when you have enough information for a clear report. Usually 3-5 questions is enough. Do not over-ask.

### Phase 4 — Generate Report

7. Increment the bug counter and save it back.
8. Generate ID: `BUG-{counter:03d}`.
9. Generate a slug from the bug title (kebab-case, max 5 words, ASCII only).
10. Copy template from `~/.claude/templates/sdd/bug-report.md`. If `.claude/templates/bug-report.md` exists in the project, use that instead (project override).
11. Fill all placeholders:
    - `{{ID}}` — generated bug ID
    - `{{TITLE}}` — concise bug title in English (you generate this)
    - `{{SEVERITY}}` — confirmed severity level
    - `{{DATE}}` — today's date (YYYY-MM-DD)
    - `{{REPORTER}}` — ask the reporter's name if not known; use "QA" as fallback
    - `{{SUMMARY}}` — 1-2 sentence summary of the bug
    - `{{STEPS}}` — numbered steps to reproduce
    - `{{EXPECTED}}` — what should happen
    - `{{ACTUAL}}` — what actually happens
    - `{{SCREENSHOTS}}` — reference any screenshots the user provided, or remove section if none
    - `{{ENVIRONMENT}}` — browser, OS, device info if collected; remove section if not applicable
    - `{{COMPONENTS}}` — list of affected files/modules you found during codebase exploration, with brief explanation of why each is relevant
    - `{{ADDITIONAL}}` — any extra context, error messages, logs, or notes
12. Remove any sections that have no content (don't leave empty sections with just a heading).
13. Save to `{dir}/bugs/{ID}-{slug}.md`.
14. Output: bug ID, file path, severity, brief summary.

## Reporter's Description

$ARGUMENTS
