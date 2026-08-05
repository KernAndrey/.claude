Interactively collect information about a bug and generate a standardized bug report.

## Instructions

You are helping a QA engineer file a bug report. Your goal is to produce a clear, actionable, developer-ready bug report. All output in the report must be in **English**, regardless of the language the reporter uses.

### Phase 1 — Initial Context

1. Locate the SDD config and read `dir` (default: `tasks`) from it. Look for `.tasks.toml` at the project root and one or two levels deep (`*/.tasks.toml`, `*/*/.tasks.toml`) — a repo may carry several SDD roots, e.g. one per client. With several configs, pick the one whose directory contains the code the bug lives in, or the one a project rule names. `dir` resolves relative to that config's own directory. No `.tasks.toml` anywhere → tell the user to run `/task-init` first and stop.
2. Read the bug counter from `{dir}/bugs/.counter`. If the file does not exist, create `{dir}/bugs/` directory and `{dir}/bugs/.counter` with `0`.
3. The user's initial description is below in `$ARGUMENTS`. If empty, ask the user to describe what happened.

### Phase 2 — Codebase Exploration

4. Based on the description, search the codebase for relevant files: models, views, routes, components, services — anything that could be related to the reported behavior. Read the most relevant files to understand the area.
5. Form a mental model of which components are involved and what could go wrong.

### Phase 3 — Clarifying Questions

**Before any question — the gate.**

1. Dig the code first. Find the answer where it lives — models, call-sites, existing conventions — before forming a question.
2. Resolve it yourself when you can. A purely technical point the code answers, or an obvious yes (e.g. "should I do the task at all?"), needs no question — decide and record it as context.
3. Ask only genuine decisions — ones with downstream consequences where a wrong guess causes rework. When unsure which kind it is, ask: a 30-second question beats a silent wrong default.
4. Ask as many as genuinely matter — never pad to a count.

**Language.** Run the QA session in Russian — questions, options, and the +/− trade-offs the user reads and answers. The bug report itself stays in English (see top of this file). Write each question with context legible to someone not living in the task; where a question offers options (e.g. severity, priority), put each option's trade-off in its `description` (+ upside; − downside).

6. Ask clarifying questions **one at a time** using `AskUserQuestion`. Adapt your questions based on what you found in the codebase and what information is still missing. Typical questions (ask only what's relevant — skip what's already clear):

   - **Steps to reproduce** *(required)* — ask for exact steps. If the initial description is vague, keep asking until you have a concrete, numbered sequence. Do not proceed without this.
   - **Expected vs actual behavior** *(required)* — if not obvious from context, ask explicitly. Do not proceed without both.
   - **Reproducibility** — always, sometimes, or once?
   - **Environment** — only ask if the bug looks browser/OS/device-specific (frontend, CSS, responsive issues). For backend or logic bugs, skip this.
   - **Severity** — propose a severity level based on your understanding and ask the user to confirm or change:
     - `critical` — app crash, data loss, security vulnerability, complete feature block
     - `high` — major feature broken, no workaround
     - `medium` — feature partially broken, workaround exists
     - `low` — cosmetic, minor inconvenience
   - **Priority** — propose a priority level and ask the user to confirm or change:
     - `urgent` — must fix immediately, blocks release or critical path
     - `high` — fix in current sprint/cycle
     - `medium` — fix soon, but not blocking
     - `low` — fix when convenient, backlog

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
    - `{{PRIORITY}}` — confirmed priority level
    - `{{DATE}}` — today's date (YYYY-MM-DD)
    - `{{REPORTER}}` — ask the reporter's name if not known; use "QA" as fallback
    - `{{SUMMARY}}` — 1-2 sentence summary of the bug
    - `{{STEPS}}` — numbered steps to reproduce
    - `{{EXPECTED}}` — what should happen
    - `{{ACTUAL}}` — what actually happens
    - `{{ENVIRONMENT}}` — browser, OS, device info if collected; remove section if not applicable
    - `{{COMPONENTS}}` — list of affected files/modules you found during codebase exploration, with brief explanation of why each is relevant
    - `{{ADDITIONAL}}` — any extra context, error messages, logs, or notes
12. Remove any sections that have no content (don't leave empty sections with just a heading).
13. Save to `{dir}/bugs/{ID}-{slug}.md`.
14. Output: bug ID, file path, severity, priority, brief summary.

### Formatting Rules

Apply these rules consistently across every section of the report:

**UI elements in double quotes.** Every button, menu item, tab, field label, dialog title, or any other clickable/visible UI element must be wrapped in straight double quotes, followed by the element type. Examples:
- Click the "Void" button.
- Open the "Invoices" menu.
- Switch to the "Shipment Details" tab.
- Fill the "Customer" field.

Do NOT use bold, backticks, or bare words for UI elements. Always `"Name" element-type`.

**System/UI error messages as fenced code blocks inside a blockquote.** Every error popup, toast, flash message, or system-generated message the user sees must be rendered as a fenced code block wrapped in a Markdown blockquote:

```
> ```
> Error Title
> Error body text exactly as shown to the user.
> ```
```

Apply this everywhere the error is mentioned — Summary, Actual Result, etc. — not just the first occurrence. Do NOT inline error messages with bold or italics.

## Reporter's Description

$ARGUMENTS
