---
name: analyze-meeting
description: Analyze meeting-processor output (transcript + screenshots) against the current project's codebase to produce actionable tasks with code references and visual context
---

# Analyze Meeting Recording

Analyze a meeting-processor output directory and produce an actionable task document. You are invoked from the user's working project — you already have full context of its codebase. Use that context to cross-reference what was discussed with actual code.

## Input

`$ARGUMENTS` format: `<path-to-meeting-output-dir> [optional instructions]`

- First token: path to meeting output directory (contains `transcript.json`, `context.md`, `frames/`)
- Remaining text (if any): focus instructions from the user (e.g., "focus on backend tasks", "ignore UI discussion", "only Speaker A's tasks")

## Phase 1: Load Meeting Data

1. Parse `$ARGUMENTS`: split into directory path (first token) and optional instructions (rest).
2. Read `<dir>/transcript.json`. Extract:
   - `meeting_name`, `duration_formatted`, `speakers`, `language`
   - `segments[]` — each has: `speaker`, `text`, `start_ms`, `end_ms`, `start_formatted` (MM:SS), `end_formatted`, `frames[]` (list of filenames)
   - `frames[]` — each has: `filename`, `timestamp_ms`, `timestamp_formatted`, `extraction_type` ("scene" or "interval")
3. Read `<dir>/context.md` for the full conversational flow. Format:
   ```
   [00:19-00:46] **{Speaker A}**: text here...
   > 📸 frames/00m29s_scene.jpg, frames/00m30s_scene.jpg
   ```

### Timestamp reference
- Segments: `start_formatted`/`end_formatted` in MM:SS, precise `start_ms`/`end_ms` in milliseconds
- Frame filenames: `XXmYYs_scene.jpg` or `XXmYYs_interval.jpg` — minutes and seconds encoded in the name
- Frame metadata: `timestamp_ms` gives precise millisecond timestamp
- Frame-to-segment matching: a frame belongs to a segment if `frame.timestamp_ms` falls within `[segment.start_ms - 2000, segment.end_ms + 2000]`

## Phase 2: Identify Signal Segments

Scan the full transcript text and mark segments that contain:

**Tasks / action items** — language indicating work to be done:
- EN: "need to", "let's do", "should implement", "create", "fix", "I'll do", "we have to", "add", "remove", "refactor"
- RU/UA: "нужно сделать", "давай сделаем", "надо", "сделать", "добавить", "исправить", "переделать", "я сделаю"

**Decisions** — agreement or choice being made:
- EN: "agreed", "decided", "let's go with", "okay we'll do", "confirmed"
- RU/UA: "решили", "окей, делаем", "давай так", "понял, делаем", "согласен"

**Blockers / concerns** — problems or open questions:
- Expressions of uncertainty, risk, dependency on external factors

**Note-taking moments** — someone explicitly recording:
- "записываю", "пишу", "let me note this", "I'm writing this down", "сек, пишу"

**Topic transitions** — shifts in subject matter (new feature, new problem, etc.)

## Phase 3: Cross-Reference with Project Codebase

THIS IS THE MOST IMPORTANT PHASE. The meeting discusses features, components, and problems that exist (or will exist) in the current project. You must bridge the discussion to real code.

For each signal segment:

1. **Extract mentions** — identify references to: features, pages, models, API endpoints, database tables, UI components, file names, function names, services, configuration, anything code-related.

2. **Search the codebase** — use Glob and Grep to find relevant files and code:
   - If a model/entity is mentioned (e.g., "carrier", "payment", "user") → search for model definitions, migrations, views
   - If a page/screen is mentioned → search for route definitions, templates, components
   - If an API is mentioned → search for endpoint handlers, serializers
   - If a specific file or function is mentioned → find it directly

3. **Read and understand** — read the relevant code sections to understand the current state. Note:
   - Does this code already exist or is it new?
   - What would need to change to implement what was discussed?
   - What related code might be affected?

4. **Record findings** — for each task/decision, save:
   - Exact file paths and line numbers
   - Current function/class names
   - Brief description of current state vs. discussed change

## Phase 4: Selective Frame Analysis

You MUST look at screenshots — they show what was on screen during the meeting (code editors, UIs, diagrams, browser tabs). This adds critical context that audio alone cannot capture.

### Frame selection strategy — READ ALL MEANINGFUL FRAMES

Quality over context savings. Do NOT limit yourself to a small sample. Read every frame that could add value:

1. **ALL scene-change frames** (`_scene.jpg`) — every single one. These are triggered by visual changes and almost always carry useful information. The only exception: if 3+ scene frames fall within the same 3-second window, read the first and last (the rest are transition artifacts).

2. **ALL interval frames** that fall during or near signal segments (±30 seconds).

3. **Every 2nd-3rd interval frame** in non-signal periods — skim for unexpected visual context (someone opened a doc, showed a diagram, etc.) that the transcript didn't capture.

In practice this means reading 50-100+ frames for a typical 60-90 minute meeting. This is intentional — missing visual context is worse than using more context window.

### How to read frames

Use the Read tool on `<dir>/frames/<filename>`. Claude is multimodal and will see the image. For each frame, note:
- What application is visible (code editor, browser, terminal, design tool, spreadsheet, presentation)
- Any visible file names, URLs, code, UI elements, diagram labels, terminal output
- How it relates to what was being discussed at that timestamp
- Any information visible in the image that was NOT captured in the audio transcript

## Phase 5: Write Output

Create directory `.meetings/summaries/` in the current working directory if it doesn't exist.
Write the analysis to `.meetings/summaries/<meeting_name>.md` where `meeting_name` comes from `transcript.json`.

**Output is ALWAYS in English**, regardless of the transcript language.

If the user provided focus instructions in `$ARGUMENTS`, apply them: prioritize relevant topics, de-emphasize or skip irrelevant ones.

### Output format

```markdown
# Meeting Analysis: {meeting_name}

**Date:** {date from meeting_name or processed_at} | **Duration:** {duration_formatted} | **Speakers:** {speakers list}
**Source:** `{path to meeting output dir}`

---

## Executive Summary

{2-4 sentences: what the meeting was about, main outcomes and decisions}

---

## Action Items

| # | Task | Owner | Timestamp | Priority |
|---|------|-------|-----------|----------|
| 1 | {short description} | {speaker} | [{time}] | high/medium/low |

### Task 1: {descriptive title}
- **Owner:** {speaker who accepted responsibility — whoever said "I'll do it" / "okay" / "понял"}
- **Discussed at:** [{start_formatted}-{end_formatted}]
- **Priority:** high/medium/low
- **Summary:** {1-2 sentence short description of what needs to be done}

#### Discussion Context

Reconstruct the FULL conversation around this task. Include:
- What problem or need was raised and by whom
- What options or ideas were discussed (even rejected ones — they show the reasoning)
- What constraints, edge cases, or concerns were mentioned
- Direct quotes from speakers where they add clarity (in original language, with translation if not English)
- What was visible on screen at this moment (code, UI, diagram — from frame analysis)

This section should read like a briefing for someone who wasn't in the meeting. Don't summarize — preserve the nuance and reasoning. A developer picking up this task should understand WHY it was requested and WHAT was considered.

#### Implementation Notes

Based on the discussion AND the current codebase:
- **Current state:** {what exists now in the code — specific files, functions, models}
- **Relevant code:**
  - `path/to/file.py:42` — {what this code does and how it relates}
  - `path/to/model.py` — {current state, what needs to change}
- **Proposed approaches:** {approaches discussed in the meeting or inferred from the conversation — list each with pros/cons if mentioned}
- **Dependencies:** {other tasks, external APIs, data, or decisions this depends on}
- **Risks / open questions:** {anything uncertain, unresolved, or potentially tricky that was mentioned or is apparent from the code}

#### Acceptance Criteria (inferred)
- {bullet list of what "done" looks like, based on the conversation}
- {be specific — not "implement payment" but "carrier can have multiple bank accounts with different payment types (ACH, cash, Zelle)"}

---

{repeat for each task}

---

## Decisions Made

| # | Decision | Timestamp | Participants | Rationale |
|---|----------|-----------|-------------|-----------|
| 1 | {what was decided} | [{time}] | {who} | {why — what reasoning led here} |

---

## Key Discussion Topics

### {Topic Name}
- **Timestamps:** [{start}-{end}]
- **Participants:** {speakers involved}
- **Summary:** {what was discussed}
- **Visual context:** {what was on screen, if frame was examined}
- **Related code:** {file paths and current state in the codebase}
- **Open questions:** {anything left unresolved}

{repeat for each major topic}

---

## Visual Context Log

Frames examined during analysis:

| Frame | Timestamp | Content |
|-------|-----------|---------|
| `{filename}` | {time} | {description of what's visible on screen} |

---

## Statistics

- Segments: {N} | Duration: {duration}
- Frames total: {N} (scene: {N}, interval: {N}) | Frames analyzed: {N}
- Tasks extracted: {N} | Decisions recorded: {N}
```

### Priority assignment rules
- **High:** explicitly called urgent/important, or is a blocker for other work
- **Medium:** normal discussion, clearly needs to be done, no urgency signals
- **Low:** "nice to have", "someday", "if we have time", exploratory ideas

### Owner assignment rules
- Assign to whoever explicitly accepted the task ("I'll do it", "okay", "понял, сделаю")
- If no one explicitly accepted, assign to the speaker who proposed it
- If ambiguous, note "TBD" and explain in context
