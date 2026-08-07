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
  Analyst   → Objective, Key Constraints, Glossary, Scope, Assumptions,
              Behavior, Acceptance Criteria, Examples, Edge Cases & Risks,
              Affected Areas, Testing Strategy, Definition of Done, Dependencies
  Architect → Architecture & Implementation Plan (both hard and soft
              subsections), plus depends_on / blocks in frontmatter
  Lead      → Blockers (adds entries when the user defers questions)
  Template  → Change Control (static text, never edited)
-->

## Objective

<!-- One or two sentences: what and why. Business outcome, not implementation. -->

## Key Constraints

<!-- 5-10 lines maximum. Duplicates of the most critical rules from Behavior
     and Acceptance Criteria — the ones where a miss causes data loss, security
     holes, or broken invariants. Phrased as positive requirements.

     Why: LLM attention follows a U-curve (primacy + recency). Constraints
     buried in the middle of a long Behavior section receive less weight.
     This block gives them primacy-position reinforcement.

     Populated by Analyst AFTER writing Behavior — it is a synthesis, not
     a first draft. Every item here MUST have a matching detail in Behavior
     or AC. -->

1.

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

## Assumptions

<!-- Conditions the spec takes for granted but does not verify at runtime.
     If any assumption is false, the spec's behavior guarantees do not hold.

     Examples: "email credentials remain valid for the duration of a chain",
     "parse_all_emails() is idempotent", "queue_job service is available",
     "no concurrent enrollment modifications for the same contact".

     Each assumption is one bullet: the assumption, then why it matters.
     Reviewers (human or agent) can challenge any assumption — a challenged
     assumption becomes a Blocker. -->

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
     ## Examples below.

     FSM tables: when describing entities with states and transitions, include
     a compact FSM transition table after the prose description:

       | From → To | Trigger | Guard | Side-effect |
       |-----------|---------|-------|-------------|
       | active → paused | action_pause | user is enrollee or manager | — |

     Illegal transitions: list explicitly (e.g., "withdrawn → ANY is illegal
     (terminal state)"). The table complements the prose — both are required.

     Sentinel: Analyst MUST embed one specific, easily-verifiable detail
     somewhere in this section — a specific constant name, exact error message,
     or naming convention. Mark it inline as [SENTINEL]. This acts as a canary
     to verify implementing agents read the full section. Example:
     "the validation error message MUST be exactly: 'Enrollment requires
     an active contact — see AC-3' [SENTINEL]" -->

## Acceptance Criteria

<!-- Every AC must be binary-verifiable: a test can assert it in one expression.
     Format:
       **AC-N** — Short title
         Given <literal precondition>,
         when <literal action>,
         then <literal observable>

     Use concrete values: exact field values, exact UI strings, exact counts,
     exact error messages. Never "appropriate", "reasonable", "typical",
     "as needed", "if applicable", "gracefully", "sensibly" — these are hidden
     decisions. If the precise value isn't known, escalate as a blocker, do not
     invent.

     AC number appears exactly once. Two independent scenarios in one AC →
     **Scenario A** / **Scenario B**.

     Example:
     **AC-1** — Archive sets reason and audit trail
       Given an employee with `active=True` and `state='open'`,
       when the user clicks "Archive" and enters reason "Retired",
       then `employee.active = False` AND `employee.archive_reason = 'Retired'`
       AND the audit log records `archived_by = current_user.id`. -->

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

<!-- The test plan, AC by AC. Owned by Analyst and written in business/behavior
     terms; the Architect adds the file-level test target in the AC →
     Implementation map rather than editing here. A case missing here is an
     untested path in production — Spec-Critic-Testing audits this section
     against every AC, Behavior rule, Example, and Edge Case, and the Tester in
     /implement writes exactly the cases it lists.

     One `### AC-N (short title) — level` entry per AC, where level is
     unit | integration | e2e. Each entry lists:
       - Success: the happy path — literal input, literal observable that
         proves the AC holds
       - Failure: every way this AC can legitimately be violated — rejected
         input, missing permission, absent record, external service error,
         conflicting state. One line each. When an AC genuinely has none,
         write `no failure mode — <why>` so the reader sees it was considered.
       - Boundary: empty, zero, one, many, maximum, duplicate, and concurrent
         cases, plus every branch of an enumeration or state transition the AC
         touches. One line each.
       - Fixtures: where the test data comes from (synthetic, real samples,
         common base class)
       - Idempotency: which operations give the same result on a repeated run
         (migrations, sanitizers, cron jobs), or `n/a`
       - Mocks: for integration tests, where mocks end and real code begins
         (e.g. "mock requests.Session, not the model layer")

     Close with two coverage lines that map the rest of the spec into the plan:
       - Edge Cases covered: every row of ## Edge Cases & Risks → the entry
         that tests it
       - Examples covered: every ## Examples entry → the case that asserts its
         literal before/after values

     Example:

     ### AC-1 (archive via wizard) — integration
     - Success: employee `active=True`, reason "Retired" → `active=False`,
       `archive_reason='Retired'`, one audit row with `archived_by=uid`.
     - Failure: employee already archived → UserError "Employee is already
       archived", no second audit row.
     - Failure: user outside `hr.group_hr_manager` → AccessError, record
       unchanged.
     - Boundary: reason "Other" with empty free text → ValidationError.
     - Boundary: 50 employees archived in one batch → 50 audit rows.
     - Fixtures: TransactionCase, real ORM, `hr.employee` with active=True.
     - Idempotency: re-running the wizard on an archived employee is a no-op.
     - Mocks: none.

     ### AC-2 (signature sanitizer) — unit
     - Success: input containing a `<script>` block → output identical minus
       that block.
     - Failure: no failure mode — pure function, total over string input.
     - Boundary: empty string → empty string; 1 MB input → completes without
       truncation.
     - Fixtures: tests/fixtures/signatures/*.html (3 real samples).
     - Idempotency: sanitize(sanitize(x)) == sanitize(x).
     - Mocks: none.

     Edge Cases covered: risk 1 → AC-1 failure 2; risk 2 → AC-2 boundary 2.
     Examples covered: "Archive a lead-stage contact" → AC-1 success.

     A gap that survives /spec's fix rounds is recorded as a closing line
     `Uncovered: AC-N — <the missing case>`, one per gap, so /implement sees it
     instead of inheriting a silent hole.
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
       - Size cap: ~3000 lines of expected diff per Coder. Split further if
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
     - **raised-by**: spec-analyst | spec-architect | spec-critic-arch | spec-critic-business | spec-critic-premise | spec-critic-testing | spec-critic-adaptive:{lens-id} | lead (Phase 1 / Phase 3)
     - **raised-on**: YYYY-MM-DD
     - **expertise-needed**: business | architecture | testing | security | ux | unknown
     - **context**: <what was found, what's ambiguous>
     - **question**: <the actual question>
     - **options**: <optional numbered list of candidates>
     - **deferred-history**:
       - YYYY-MM-DD: deferred by user, note "<who should answer>"
     - **resolution**: <empty while open; filled on answer>
-->

## Review Lenses

*Review metadata — not requirements. Nothing in this section is implemented or traced to code.*

<!-- Keep the italic marker line above when filling this section — it is what tells
     Spec-Auditor to skip these entries during /implement.

     Filled by Lead in /spec §2c-0 before the critic batch spawns: the angles it
     designed for this specific spec, on top of the four fixed critics. Recorded
     here so a resume run can re-run or extend them instead of losing them.

     Entry format:

     ### {lens-id}
     - **angle**: <the stance, one line>
     - **justification**: <why this spec needs it, citing an AC / file / state / model>
     - **hunt**: <failure classes this angle should surface>
-->
