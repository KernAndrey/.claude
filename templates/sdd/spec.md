---
id: "{{ID}}"
title: "{{TITLE}}"
status: awaiting-approval
created: "{{CREATED_DATE}}"
spec_date: "{{DATE}}"
updated: "{{DATE}}"
priority: "{{PRIORITY}}"
draft_source: "{{DRAFT_PATH}}"
---

## Objective

<!-- One or two sentences: what and why -->

## Scope

### In Scope

-

### Out of Scope

-

## Behavior

<!-- Describe what the system should do in plain English: user-facing changes, data flow, system interactions. No code, no file paths, no class names. Write as a narrative a non-technical stakeholder can read. -->

## Acceptance Criteria

<!-- Each criterion should be verifiable. Use "Given / When / Then" or simple declarative statements. No code. -->

- [ ]
- [ ]
- [ ]

## Edge Cases & Risks

<!-- Found by Critic agent. Potential issues, error scenarios, data volume concerns, security considerations. Describe in business terms. -->

## Affected Areas

<!-- Which parts of the system are affected (e.g. "user authentication flow", "order processing pipeline"). Do NOT list file paths or class names. -->

## Architecture & Implementation Plan

<!-- Filled by the Architect teammate. This is the ONLY section of the spec
that may reference file paths, modules, classes, addons, or other implementation
details. Do NOT include function bodies or full code blocks here — only
structural decisions and the file map. -->

### Approach

<!-- 2-5 sentences: how this feature fits into the project's existing
architecture. New addon vs extending an existing one, key patterns reused,
why this approach over alternatives. -->

### AC → Implementation map

<!-- Filled by the Architect. For each AC from the Acceptance Criteria
section, name the concrete architecture element that fulfills it plus
a test target. Format:
  AC-N: <one-line restatement> → <file>: <method/class/section> + <test target>
Every AC must appear here. AC without a mapping is a planning gap. -->

- AC-1: <restatement> → `path/to/file.ext`: `<element>` + `tests/test_x.py::test_y`

### Files to create

<!-- List of new files with one-line purpose each. Group by module/addon. -->

- `path/to/file.ext` — purpose

### Files to modify

<!-- Existing files that need changes, with one-line description of what changes. -->

- `path/to/existing.ext` — what changes

### Integration points

<!-- How this hooks into existing systems: which models extended, which
hooks/signals/events used, which routes/menus added, which dependencies
declared. -->

### Open architectural questions

<!-- Anything the Architect could not resolve from the codebase + user answers.
Will become decisions for the Coder during implementation. -->

### Work breakdown

<!-- Filled by the Architect. Defines how the implementation is split across
parallel Coders during /implement. ALWAYS filled, even for single-coder tasks
(in that case list one coder with the full scope).

Every file from "Files to create" and "Files to modify" must appear in exactly
one coder's scope — no overlaps, no gaps.

Note: only Coders are parallelized. There is always a single Tester that
writes tests for everything (parallel test execution causes conflicts on
shared infrastructure — DBs, ports, fixtures). -->

- **coder-1** — scope: <what this coder builds>; files: `path/a`, `path/b`

## Dependencies

<!-- Dependencies on other tasks, external systems, or business decisions -->
