---
id: "{{ID}}"
title: "{{TITLE}}"
status: awaiting-approval
created: "{{CREATED_DATE}}"
spec_date: "{{DATE}}"
updated: "{{DATE}}"
priority: "{{PRIORITY}}"
draft_source: "{{DRAFT_PATH}}"
depends_on: []
blocks: []
---

<!--
This spec is the single source of truth for a Coder. It must be readable
front-to-back by both a domain stakeholder (for the business sections) and
a Coder (for Architecture Decisions). Every ambiguity must be resolved
before /task-approve — unresolved questions live in ## Blockers.

Section ownership:
  Analyst   → Objective, Glossary, Scope, Behavior, Acceptance Criteria,
              Examples, Edge Cases & Risks, Affected Areas, Testing Strategy,
              Definition of Done, Dependencies
  Architect → Architecture & Implementation Plan (both hard and soft
              subsections), plus depends_on / blocks in frontmatter
  Lead      → Blockers (adds entries when the user defers questions)
  Template  → Change Control (static text, never edited)
-->

## Objective

<!-- One or two sentences: what and why. Business outcome, not implementation. -->

## Glossary

<!-- 3-5 terms whose meaning a reader could get wrong. Include only terms that
     are actually used in Behavior / Acceptance Criteria below and that are
     ambiguous (multiple meanings, project-internal jargon, overloaded words).
     One sentence per term. Link to source of truth if one exists.

     Example:
     - **active employee**: `hr.employee.active == True` AND `state != 'terminated'`.
       Both conditions required. Matches existing HR dashboard definition.
-->

-

## Scope

### In Scope

-

### Out of Scope

-

## Behavior

<!-- Describe what the system should do in plain English: user-facing changes,
     data flow, system interactions. Narrative, not code. No file paths, no
     class names.

     Every numbered list in this section must carry an explicit order marker:
       `Order: strict`  — steps must run in the given sequence
       `Order: any (listed for readability)` — order is illustrative, not binding

     If a behavior is non-trivial (more than one input/output combination,
     any state transition, any transformation), add a matching entry in
     ## Examples below. -->

## Acceptance Criteria

<!-- Every AC must be binary-verifiable: a test can assert it in one expression.
     Format:
       `Given <literal precondition>, when <literal action>, then <literal observable>`

     Use concrete values: exact field values, exact UI strings, exact counts,
     exact error messages. Never "appropriate", "reasonable", "typical",
     "as needed", "if applicable", "gracefully", "sensibly" — these are hidden
     decisions. If the precise value isn't known, escalate as a blocker, do not
     invent.

     Example:
     - [ ] Given an employee with `active=True` and `state='open'`, when the
           user clicks "Archive" and enters reason "Retired", then
           `employee.active = False` AND `employee.archive_reason = 'Retired'`
           AND the audit log records `archived_by = current_user.id`. -->

- [ ]
- [ ]
- [ ]

## Examples

<!-- For every non-trivial Behavior rule, one concrete input→output example
     with literal values — not pseudocode. Before/Input/After blocks.

     Example:

     ### Example: Archive a lead-stage contact by confirming their PRO#

     Before:
       partner.contact_lifecycle_stage = 'lead'
       partner.contact_lead_status = 'engaged'

     Wizard input:
       pro_number = '78432'

     After:
       partner.contact_lifecycle_stage = 'contact'
       partner.contact_lead_status = False
       partner.contact_rfq_activity = False
       partner.setup_info_ids += [{event_type: 'contact_first_load',
                                   load_id: 5, pro_number: '78432'}]
-->

## Edge Cases & Risks

<!-- Table form. Found by Analyst (seeded from the draft) and expanded by
     Critic. Severity forces prioritization; Status tracks mitigation progress.

     Severity: HIGH (breaks prod / data loss if missed)
             | MEDIUM (feature misbehaves)
             | LOW (cosmetic / rare)
     Status: OPEN (no mitigation yet)
           | MITIGATED (mitigation described below)
           | RESOLVED (addressed in Architecture Decisions)
-->

| #  | Risk | Severity | Mitigation | Status |
|----|------|----------|------------|--------|
| 1  |      |          |            |        |

## Affected Areas

<!-- Which parts of the system are affected, in business terms
     (e.g. "employee archival workflow", "partner lead lifecycle").
     Do NOT list file paths or class names — those belong in Architecture. -->

## Testing Strategy

<!-- How each AC will be tested. Written by Analyst in business/behavior terms,
     expanded by Architect with file-level detail when needed.

     Must answer:
       - Level per AC: unit | integration | e2e
       - Fixture strategy: where test data comes from (synthetic, real samples,
         common base class)
       - Idempotency requirements: which operations must give the same result
         on repeated runs (migrations, sanitizers, cron jobs)
       - Mock boundaries: for integration tests, where mocks end and real code
         begins (e.g. "mock requests.Session, not the model layer")

     Example:
       - AC-1 (archive via wizard): integration — TransactionCase, real ORM,
         fixture: `hr.employee` with active=True. Idempotent: re-running
         wizard on already-archived employee must be a no-op. Mocks: none.
       - AC-2 (signature sanitizer): unit — pure function, pytest-compatible,
         fixtures in tests/fixtures/signatures/*.html (3 real samples).
         Idempotent: sanitize(sanitize(x)) == sanitize(x).
-->

## Architecture & Implementation Plan

<!-- Owned by Architect. The ONLY section where file paths, modules, classes,
     addons, decorators, and method names belong — they are required here,
     not optional.

     Split into two subsections with different authority:

     - Architecture Decisions (hard): the Coder must follow these. Any
       deviation requires a Change Control entry and author approval.
     - Implementation Guidance (soft): hints the Coder may deviate from
       without ceremony if the end result is equivalent.
-->

### Approach

<!-- 2-5 sentences: how this fits the project's existing architecture. New
     addon vs extending an existing one, key patterns reused, why this
     approach over alternatives. -->

### Architecture Decisions (hard)

<!-- Coder-binding decisions. Any deviation requires a Change Control entry. -->

#### AC → Implementation map

<!-- Every AC from Acceptance Criteria must appear here, mapped to a concrete
     file + element + test target. An AC without a mapping is a planning gap.
     Format:
       AC-N: <one-line restatement> → `path/to/file.ext`: `<element>` + `tests/test_x.py::test_y`
-->

- AC-1: <restatement> → `path/to/file.ext`: `<element>` + `tests/test_x.py::test_y`

#### Files to create

<!-- New files with one-line purpose each. Group by module/addon.
     Every file here must appear in exactly one coder's files: list under
     Work breakdown. -->

- `path/to/file.ext` — purpose

#### Files to modify

<!-- Existing files with one-line description of what changes. Every file
     here must appear in exactly one coder's files: list. -->

- `path/to/existing.ext` — what changes

#### Integration points

<!-- How this hooks into existing systems: which models extended, which
     hooks/signals/events used, which routes/menus added, which dependencies
     declared. -->

#### Work breakdown

<!-- How implementation is split across parallel Coders during /implement.
     ALWAYS filled, even for single-coder tasks (list one coder with full
     scope).

     Rules:
       - Every file from "Files to create" / "Files to modify" appears in
         exactly one coder's files: list (no overlaps, no gaps).
       - Stable names: coder-1, coder-2, … (single-coder tasks use coder-1).
       - Split only when work streams touch different files with no shared
         logic. Tightly coupled work stays with a single Coder.
       - Size cap: ~2000 lines of expected diff per Coder. Split further if
         estimate exceeds the cap.
       - Do not list Tester — there is always a single Tester spawned by
         the /implement lead. -->

- **coder-1** — scope: <what this coder builds>; files: `path/a`, `path/b`

#### Open architectural questions

<!-- Anything Architect could not resolve from codebase + user answers.
     Empty if everything is resolved. Phase 3 of /spec picks these up. -->

### Implementation Guidance (soft)

<!-- Hints the Coder can deviate from without ceremony if the end result is
     equivalent. Typical content:
       - Suggested helper function names
       - Internal refactor opportunities noticed during exploration
       - Stylistic preferences inside a single file
     Do not put binding decisions here — those belong in Architecture Decisions. -->

## Change Control

<!-- Static rules. Not edited by Analyst or Architect. -->

Any deviation from Behavior, Acceptance Criteria, or Architecture Decisions
during implementation must be handled as follows:

1. The Coder adds a `DEVIATION:` entry under Known Concerns in their commit
   message and in the implementation report, describing what was changed and
   why.
2. The deviation either gets explicit approval from the spec author, or is
   marked `accepted by coder, pending review` for the final human review
   step.
3. Silent scope expansion is forbidden. Refactors, cleanups, and "while I
   was here" edits that do not trace to a spec item are scope creep and will
   be rejected by Spec-Auditor.

Implementation Guidance (soft) entries are exempt — deviating from a soft
hint needs no Change Control entry.

## Definition of Done

<!-- Gate list for closing the task. Analyst marks non-applicable items as
     `N/A — reason`. -->

- [ ] All Acceptance Criteria pass
- [ ] Tests run green on a clean database
- [ ] Migration verified on a production database copy (if the spec touches schema)
- [ ] Code-Reviewer reports no MUST FIX findings
- [ ] Test-Reviewer reports no MUST FIX findings
- [ ] Spec-Auditor reports COMPLIANT
- [ ] No regressions in modules listed under Affected Areas
- [ ] Feature branch rebased on fresh dev

## Dependencies

<!-- External systems, other tasks, or business decisions this work depends on,
     in business terms. Machine-readable dependencies belong in the frontmatter
     `depends_on` / `blocks` arrays. -->

## Blockers

<!-- Deferred questions. Each entry is added by Lead when the user defers a
     question during /spec (in free-form wording — "не знаю", "пропустить",
     "позже") or when an agent escalates via SPEC <ROLE> QUESTION FOR USER
     and the user defers.

     Resolved entries stay here as an audit trail — only entries with
     `status: open` block /task-approve.

     Entry format:

     ### b-N — <short title>
     - **status**: open | resolved-by-user
     - **raised-by**: spec-analyst | spec-architect | spec-critic | lead (Phase 1 / Phase 3)
     - **raised-on**: YYYY-MM-DD
     - **expertise-needed**: business | architecture | testing | security | ux | unknown
     - **context**: <what was found, what's ambiguous>
     - **question**: <the actual question>
     - **options**: <optional numbered list of candidates>
     - **deferred-history**:
       - YYYY-MM-DD: deferred by user, note "<who should answer>"
     - **resolution**: <empty while open; filled on answer>
-->
