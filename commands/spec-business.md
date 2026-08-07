Generate a **preliminary** specification from a draft task. This is the business-scope sibling of `/spec`: you (the main conversation agent) play the role of a project manager, study the codebase, ask only business questions, and produce a partially filled spec that a developer will refine later. No subagents. No architect. No critics.

<critical>
Ask only business questions. Architecture, file paths, class names, module layout, data schemas, API surfaces, test file layout, deployment, and migrations are OFF-LIMITS as questions to the user. When a technical question arises, record it in the spec under `## Architecture & Implementation Plan → Open architectural questions` as a note for the developer — do not ask the user.
</critical>

## 0. Setup

1. Read `.tasks.toml` for `id_prefix` and `dir`. Several `.tasks.toml` in the repo (root plus `*/.tasks.toml`, `*/*/.tasks.toml`, skipping `node_modules/`, `.git/`, `vendor/` and plugin/cache directories) means several SDD roots — use the one whose `id_prefix` matches the task ID. `{dir}` below is that config's `dir`, resolved relative to the config's own directory. None found → tell the user to run `/task-init` and stop.
2. Locate the target by `$ARGUMENTS` (ID, slug, or full path) in `{dir}/1-draft/`:
   - Found → proceed.
   - Found in `{dir}/2-spec/` instead → tell the user `/spec-business` operates on drafts only; resuming a preliminary spec is out of scope — use `/spec {ID}` to resume or edit the spec manually. Stop.
   - Not found → error and stop.
3. Read the draft file content.
4. Read the project `CLAUDE.md` for stack and conventions. You read it only to ground your domain exploration — you will not ask the user about anything you learn there.

## 1. Phase 1 — Discovery (mandatory)

This phase cannot be skipped. Ask as many as genuinely matter — no padding to a count.

1. Read the draft task carefully.
2. Explore the project codebase for **domain context only**: existing user-facing features related to the draft, domain vocabulary used in model names and UI strings, business rules visible in existing behavior (state machines, status transitions, lifecycle stages, permissions). As you find relevant facts, append them to the draft file under a `## Codebase Observations` section in **domain language**:
   - GOOD: "The system already has a Lead → Contact lifecycle with `lead_status` values {engaged, qualified, cold}."
   - GOOD: "Existing archival flow requires a reason from a closed list."
   - AVOID: file paths, class names, method names, decorators, module/addon names. Those belong in the Architect's notes, not here.
3. Compile a list of clarifying questions. Cover these topics:
   - **Цель**: Какую бизнес-задачу решаем? Кому и как это поможет?
   - **Заинтересованные стороны**: Кто пользователь? Кто выигрывает? Кто отвечает за результат?
   - **Метрики успеха**: Как поймём, что задача решена? Что измеряем?
   - **Границы**: Что явно НЕ входит? Смежные фичи, которые трогать не нужно?
   - **Поведение**: Любые неоднозначные сценарии — спрашивай, не додумывай.
   - **Крайние случаи**: Пустые данные, ошибки, нехватка прав, большие объёмы, одновременные действия разных пользователей.
   - **Роли и права**: Кто может выполнять действие? Кто видит результат?
   - **UX / тексты**: Точные формулировки сообщений пользователю, ошибок, заголовков.
   - **Приоритет и дедлайны**: Есть ли срок? Что блокирует другие задачи?
   - **Изменение существующего поведения**: Если драфт меняет существующую функциональность — что сейчас и что именно должно измениться.
4. Ask questions **one at a time** using the defer-aware prompt format below.
5. **After each answer**, immediately append the decision to the draft file under a `## Decisions` section using `Edit`. Number each decision sequentially. Format: `N. **Short label**: decision text`. This section becomes the authoritative source of user decisions for the spec you write in Phase 2.
6. After each answer, if it reveals a new business ambiguity, add follow-up questions to the queue. Continue until no business questions remain.

**Rules for this phase:**
- Frame questions in business/domain terms.
- Include what you learned from the codebase as context ("Я вижу, что сейчас система делает X — Y должен заменить это или работать параллельно?").
- One question at a time.
- If a question starts drifting toward architecture (files, classes, schemas, integration mechanics), drop it from the queue and write it under `## Architecture & Implementation Plan → Open architectural questions` in the spec instead.

### Before any question — the gate

1. Dig the code first. Find the answer where it lives — models, call-sites, existing conventions — before forming a question.
2. Resolve it yourself when you can. A purely technical point the code answers, or an obvious yes (e.g. "should I do the task at all?"), needs no question — decide and record it as context.
3. Ask only genuine decisions — ones with downstream consequences where a wrong guess causes rework. When unsure which kind it is, ask: a 30-second question beats a silent wrong default.
4. Ask as many as genuinely matter — never pad to a count.

**Language.** Run the QA session in Russian — questions, options, and the +/− trade-offs the user reads and answers. Everything that persists is English — spec sections, plan, code, commit messages, and recorded Decisions/Blockers (translate the gist of the user's Russian answer).

### Defer-aware prompt format

```
**Вопрос N/M**: {контекст для человека ВНЕ задачи: о чём вопрос, что ты нашёл в коде/драфте, почему выбор важен и чем грозит ошибка}

{Сам вопрос}

Варианты:
1. {вариант А} — + {плюс}; − {минус}
2. {вариант Б} — + {плюс}; − {минус}
3. {вариант В — если нужен} — + {плюс}; − {минус}
4. Другое (напиши свой вариант)

(можешь ответить или отложить вопрос — напиши "пропустить" / "позже" / "не знаю", и вопрос уйдёт в Blockers)
```

### Understanding the reply

- If the user's reply expresses "I don't know / ask someone else / later / skip / defer / поставим на паузу / не знаю / пусть разработчик решит" in any natural wording → treat as **DEFER**:
  1. Ask one follow-up in plain text: "Кому это может быть известно? (бизнес / разработчик / тестер / security / ux / не знаю)"
  2. Create a blocker entry in the spec file (format below). If the spec file doesn't exist yet, queue the blocker in memory and write it into the spec immediately after you create the spec in Phase 2.
- If the reply looks like an answer — even loosely phrased — treat it as an answer.
- If you genuinely cannot tell whether the user is answering or deferring, ask one short clarifier: *"Это твой ответ или хочешь отложить вопрос в Blockers?"* Do not guess.

No keyword matching — understand the intent from meaning.

### Blocker entry format

Each blocker is a level-3 heading inside the spec's `## Blockers` section. Generate `b-N` by counting existing `### b-` headings and taking the next integer (first is `b-1`). The spec is an English artifact: record every field in English (translate the gist of the Russian Q&A — the question need not be verbatim).

```markdown
### b-N — <short title summarizing the question>
- **status**: open
- **raised-by**: spec-business (Phase 1)
- **raised-on**: {TODAY}
- **expertise-needed**: business | architecture | testing | security | ux | unknown
- **context**: <what was found in the code or draft, what's ambiguous, what each option would imply>
- **question**: <the question you asked the user, in English>
- **options**:
  1. <option>
  2. <option>
  3. <option>
- **deferred-history**:
  - {TODAY}: deferred by user, note "<user's expertise-needed answer>"
- **resolution**: (empty while open)
```

## 2. Phase 2 — Write the preliminary spec

Create the spec at `{dir}/2-spec/{ID}-{slug}.md` from the template at `~/.claude/templates/sdd/spec.md`.

**Frontmatter fields you fill now:**
- `id`, `title` — from the draft.
- `status: pm-preliminary` (this status blocks `/task-approve` until a developer flips it to `awaiting-approval`).
- `created`, `spec_date`, `updated` — TODAY.
- `priority` — from the draft.
- `draft_source` — the draft path.
- `depends_on`, `blocks` — leave as `[]`; the developer fills them when they add architecture.

### Sections you populate

Fill every section below. Empty owned sections are a bug.

- **Objective** — one or two sentences, business outcome, not implementation.
- **Key Constraints** — populate AFTER writing Behavior and Acceptance Criteria. 3-7 items, each a positive-framed one-line restatement of a critical rule. Every item MUST trace to Behavior or AC. Synthesis, not first draft.
- **Glossary** — 3-5 terms actually used in Behavior or AC that a reader could get wrong. Skip obvious terms.
- **Scope** → **In Scope** / **Out of Scope**.
- **Assumptions** — each is one bullet: the assumption, then why it matters. Cover at minimum: external service availability, data integrity preconditions, concurrency assumptions, idempotency properties. Uncertain assumption → escalate as a blocker, do not write a silent dependency.
- **Behavior** — plain-English narrative. Numbered lists carry an explicit order marker (`Order: strict` or `Order: any (listed for readability)`). State machines get an FSM transition table (see template) plus an `Illegal transitions:` line. Embed exactly one `[SENTINEL]` marker in the middle of this section — a specific, verifiable detail (exact error message, specific constant name, naming convention).
- **Acceptance Criteria** — every AC uses the format `**AC-N** — Short title` then Given/When/Then on indented lines with concrete literal values. Two independent scenarios in one AC → `**Scenario A**` / `**Scenario B**`.
- **Examples** — for every non-trivial Behavior rule (anything with a transformation, a state transition, or more than one input/output combination), one concrete before/input/after block with literal values.
- **Edge Cases & Risks** — table form (see template). Severity = HIGH/MEDIUM/LOW. Status = OPEN / MITIGATED / RESOLVED.
- **Affected Areas** — business terms only (e.g. "employee archival workflow", "partner lead lifecycle"). No file paths, no class names.
- **Testing Strategy** — business/behavior terms only, one entry per AC: level (unit / integration / e2e), the success case, and the failure cases — or `no failure mode — <why>` when an AC genuinely has none. Fixture strategy in domain terms. Idempotency requirements. Mock boundaries described as "where real domain logic begins" — not as file paths. Boundary and variant enumeration, plus the `Edge Cases covered:` / `Examples covered:` mapping lines, belong to the full `/spec` run where the testing critic audits them; a preliminary spec carries success and failure per AC and no more.
- **Definition of Done** — tick off items clearly applicable. Mark unambiguous non-applicable items as `N/A — <reason>`. Leave the rest for the human reviewer.
- **Dependencies** — external systems, other tasks, or business decisions this work depends on, in business terms.

### Sections you leave as the template placeholder text

Do NOT remove or shorten these. Leave the template's HTML comments in place so the developer sees what to fill:

- **Architecture & Implementation Plan** (the whole block including `### Architecture Decisions (hard)`, `### Implementation Guidance (soft)`, the `AC → Implementation map`, Files to create / modify, Integration points, Work breakdown, Open architectural questions). If you collected technical notes during Phase 1, append them as bullets under **Open architectural questions** only.
- **Change Control** — static template text.

The `## Blockers` section is populated only when the user defers a question.

### Determinism rules (apply to every section you own)

Every preliminary spec must be refineable into a Coder-executable spec without guessing business decisions. Enforce:

- **Every default is an explicit value.** `default = "Other"`, not "a sensible default". Unknown → escalate.
- **Every enumeration is a closed list.** `{Retired, Fired, No Longer Working, Other}`, not "typical reasons such as …". Incomplete → escalate.
- **State transitions are spelled out.** `from-state → to-state | trigger | conditions` and name every field whose value changes.
- **Every behavioral decision traces** to the draft, a Phase 1 answer, or observable existing behavior.

### Positive framing rule

Phrase constraints as what the system MUST do, not what it must NOT do.
- "A Wait activity MUST separate any two Auto Email activities" — not "Two Auto Email activities in a row is forbidden".
- "Archive reason MUST be one of {Retired, Fired, Other}" — not "Empty archive reason is not allowed".

### Forbidden words in Behavior and ACs

These hide decisions. If you reach for one, you do not yet have the concrete answer — escalate.

`appropriately, reasonable, reasonably, typical, typically, as needed, if applicable, gracefully, sensibly, properly, correctly`

### Acceptance Criteria format

```
**AC-N** — Short title
  Given <literal precondition>,
  when <literal action>,
  then <literal observable>
```

Concrete values only. AC number appears exactly once.

### Examples format

```
### Example: <short title>

Before:
  <literal state>

Input:
  <literal input>

After:
  <literal state>
```

### Plain-English rule

The sections you own are read by non-technical stakeholders. Use domain language, not programming language. Describe *what* and *why*, never *how* in code terms. No code, pseudocode, SQL, class names, method names, file paths, or module names anywhere in your sections.

## 3. Finalisation

1. Read the spec file back and verify:
   - Exactly one `[SENTINEL]` marker in the Behavior section.
   - `## Key Constraints` has 3-7 items, each traceable to Behavior or an AC.
   - Every non-trivial Behavior rule has a matching entry in `## Examples`.
   - `## Glossary` has 3-5 terms.
   - `## Assumptions` covers service availability, data integrity, concurrency, idempotency (or explicitly marks categories `N/A — <reason>`).
   - `## Architecture & Implementation Plan` is left as the template placeholder (with optional `Open architectural questions` entries).
   - Frontmatter `status: pm-preliminary`.
2. Count open blockers (`### b-N` entries with `status: open` in `## Blockers`).
3. Move the draft from `{dir}/1-draft/` to `{dir}/archive/drafts/`. The spec now contains all captured decisions.
4. Commit:

   ```
   git add -A -- "{dir}/2-spec/{ID}-{slug}.md" "{dir}/archive/drafts/{ID}-{slug}.md"
   git add -A -- "{dir}/1-draft/{ID}-{slug}.md" 2>/dev/null || true
   git commit -m "spec-business({ID}): preliminary spec"
   ```

   The second `git add` stages the draft's move as a rename in the case where an earlier `/spec` run had already committed the draft. It stays separate and error-tolerant because the usual path — a draft straight from `/task` — leaves it untracked, and git aborts on a pathspec matching nothing.

   One commit per run, as the last action. Run it with `run_in_background: true` (the pre-commit review hook can take up to 20 minutes).

5. Output:
   - Spec path.
   - Number of acceptance criteria.
   - Number of open blockers, listed by `b-N` with short question.
   - Next step for the developer:

     ```
     Preliminary spec saved at {dir}/2-spec/{ID}-{slug}.md (status: pm-preliminary).

     Next steps:
       1. Review the business sections.
       2. Fill ## Architecture & Implementation Plan.
       3. Flip status: pm-preliminary → awaiting-approval in the frontmatter.
       4. Run /task-approve {ID}.

     Or, to run the full Architect + Critics pipeline: /spec {ID}
     (only works when at least one blocker is open; otherwise edit manually).
     ```

## Rules

- Ask only business questions. Technical questions go into `Open architectural questions` in the spec, not to the user.
- Every numbered list in the sections you own carries an explicit order marker (`Order: strict` or `Order: any (listed for readability)`).
- Every non-trivial rule has a matching Example.
- When in doubt between answer and defer, ask a one-line clarifier. Never guess.
- Silent defaults are forbidden. Every implicit decision becomes an escalation.
