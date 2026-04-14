Generate a specification from a draft task using an agent team. This command is orchestration-only — the Analyst, Architect, and Critic bodies live in `~/.claude/agents/spec-{analyst,architect,critic}.md`.

## 0. Setup

1. Read `.tasks.toml`. Missing → tell user to run `/task-init` and stop.
2. Locate the target by `$ARGUMENTS` (ID, slug, or full path):
   - Match in `tasks/1-draft/` → `RUN_MODE = new`.
   - Match in `tasks/2-spec/` → `RUN_MODE = resume` (the spec already exists; you are re-entering it to resolve open blockers or apply late findings).
   - Not found → error and stop.
3. Read the draft file content (new) or the existing spec file (resume).
4. Read the project `CLAUDE.md` for stack and conventions.

## 1. Phase 1 — Discovery (new runs only)

Skip this section on resume runs; jump to Phase 1.5.

This phase is **mandatory** for new runs and cannot be skipped.

1. Read the draft task carefully.
2. Explore the project codebase: domain structure, existing behavior related to the draft, top-level architecture (modules/addons layout, conventions for similar features), constraints. **As you discover relevant facts, append them to the draft file under a `## Codebase Observations` section** — file paths, model names, API signatures, existing patterns, gotchas, performance notes. This section accumulates throughout Phase 1 and becomes the persistent knowledge base for all agents.
3. Compile a list of clarifying questions. Topics to cover:
   - **Цель**: Какая бизнес-задача решается? Кому и как это поможет?
   - **Границы**: Что явно НЕ входит в задачу? Есть ли смежные фичи, которые трогать не нужно?
   - **Поведение**: Любые неоднозначные сценарии — спроси, не додумывай.
   - **Крайние случаи**: Что происходит при пустых данных, ошибках, нехватке прав, больших объёмах?
   - **Приоритет и ограничения**: Есть ли дедлайны, требования к производительности, зависимости от другой работы?
   - **Существующее поведение**: Если драфт меняет существующую функциональность — уточни, что сейчас и что именно должно измениться.
   - **Архитектура и интеграция**: Новый модуль или расширение существующего? Есть ли конвенция для похожих фич? Спрашивай ТОЛЬКО когда ответ не очевиден из кодовой базы.
4. Ask questions **one at a time** using the defer-aware prompt format below.
5. **After each answer**, immediately append the decision to the draft file under a `## Decisions` section using `Edit`. Number each decision sequentially. Format: `N. **Short label**: decision text`. This section becomes the authoritative source of user decisions for all agents — inline prompt text is supplementary.
6. After each answer, if it reveals new ambiguities, add follow-up questions to the queue. Continue until no questions remain. Minimum 3 questions total, no upper limit.

**Rules for this phase:**
- Questions and options are in Russian.
- Frame questions in business/domain terms, except architectural topics which are technical by nature.
- Architectural questions only when the codebase doesn't give a clear answer; if there's an obvious convention, note it as context for the Architect, don't ask.
- Include what you learned from exploring the codebase as context ("Я вижу, что сейчас система делает X — Y должен заменить это или работать параллельно?").
- One question at a time.

## Defer-aware prompt format

Every question to the user — in Phase 1, Phase 1.5, agent escalations, or Phase 3 — uses this format:

```
**Вопрос N/M**: {краткий контекст — что ты нашёл в кодовой базе, что написано в спеке, почему вопрос важен}

{Сам вопрос}

Варианты:
1. {вариант А}
2. {вариант Б}
3. {вариант В — если нужен}
4. Другое (напиши свой вариант)

(можешь ответить или отложить вопрос — напиши "пропустить" / "позже" / "не знаю", и вопрос уйдёт в Blockers)
```

### Understanding the reply

- If the user's reply expresses "I don't know / ask someone else / later / skip / defer / поставим на паузу / не знаю / пусть архитектор решит" in any natural wording, Russian or English → treat as **DEFER**:
  1. Ask one follow-up in plain text: "Кому это может быть известно? (бизнес / архитектор / тестер / security / ux / не знаю)"
  2. Create a blocker entry in the spec file (format below). If the spec file doesn't exist yet (e.g. during Phase 1 before Analyst has created it), queue the blocker in your working memory and write it into the spec file immediately after Analyst creates it.
- If the reply looks like an answer — even loosely phrased — treat it as an answer.
- If you genuinely cannot tell whether the user is answering or deferring, ask one short clarifier: *"Это твой ответ или хочешь отложить вопрос в Blockers?"* Do not guess.

No keyword matching — understand the intent from meaning.

### Blocker entry format

Each blocker is a level-3 heading inside the spec's `## Blockers` section. Generate `b-N` by counting existing `### b-` headings and taking the next integer (first is `b-1`).

```markdown
### b-N — <short title summarizing the question>
- **status**: open
- **raised-by**: lead (Phase 1) | lead (Phase 3) | spec-analyst | spec-architect | spec-critic
- **raised-on**: {TODAY}
- **expertise-needed**: business | architecture | testing | security | ux | unknown
- **context**: <what was found in the code or spec, what's ambiguous, what each option would imply>
- **question**: <the exact question you asked the user>
- **options**:
  1. <option>
  2. <option>
  3. <option>
- **deferred-history**:
  - {TODAY}: deferred by user, note "<user's expertise-needed answer>"
- **resolution**: (empty while open)
```

When a blocker is later resolved, update the same entry in place:
- `status: open` → `status: resolved-by-user`
- Append a new line to `deferred-history`: `{TODAY}: answered`
- Fill `resolution:` with the user's answer

## 1.5. Blocker re-ask (resume runs only)

1. Read the existing spec. Parse `## Blockers` for level-3 headings; collect entries with `status: open`.
2. If zero open blockers → tell the user "Spec {ID} has no open blockers. Did you mean `/task-approve {ID}`?" and stop.
3. Announce: "Resuming spec {ID}. {N} open blockers from previous runs. I'll go through them — you can answer or defer again."
4. For each open blocker, in order:
   - Print the stored `topic`, `question`, `context`, `expertise-needed`, and `deferred-history`.
   - Ask the user using the defer-aware prompt format (reuse the stored context in the prompt, don't re-explore).
   - On answer: update the blocker entry (status → `resolved-by-user`, append deferred-history line, fill resolution). Remember which teammate to re-invoke based on who raised the blocker and the expertise-needed tag (`business` → Analyst, `architecture` → Architect, sometimes both).
   - On defer: append a new `deferred-history` line `{TODAY}: deferred again`. Keep `status: open`. Move to the next.
5. Build a set of affected teammates from the resolved blockers. If zero blockers got resolved, tell the user "No blockers were resolved this run. Spec unchanged; come back later." and stop.

## 2. Phase 2 — Agent team

Create the team once: `TeamCreate(team_name: "spec-{ID}")`. All three teammates live in this team and stay alive across the phase so you can `SendMessage` follow-ups.

Shared context to pass in every teammate message:
- Draft path (or existing spec path on resume) — **instruct agents to read `## Decisions` (authoritative user decisions) and `## Codebase Observations` (verified facts about the codebase) from the draft file. These two sections are the persistent source of truth — inline prompt context is supplementary.**
- User's Phase 1 answers (new runs) or resolved-blocker answers (resume runs) — inline as supplementary context
- Project `CLAUDE.md` path

### 2a. Analyst

**New runs:** Spawn `spec-analyst` (`Agent(name: "spec-analyst", team_name: "spec-{ID}")`). Send:

> Read your instructions: `~/.claude/agents/spec-analyst.md`
> Spec output path: `tasks/2-spec/{ID}-{slug}.md`
> Spec template: `~/.claude/templates/sdd/spec.md`
> Draft path: `{draft path}` — **read `## Decisions` (authoritative user decisions) and `## Codebase Observations` (verified codebase facts). Every numbered decision MUST be reflected in the spec. Codebase observations inform your writing but don't need 1:1 mapping.**
> User Phase 1 answers: {inline all answers — supplementary context}
> Project CLAUDE.md: `{path}`
> Write the business sections (including Key Constraints, Assumptions, and one `[SENTINEL]` marker in Behavior). Signal `SPEC ANALYST DONE.` when ready. Escalate ambiguities with `SPEC ANALYST QUESTION FOR USER` and wait for my reply.
> Heartbeat: every 10 min of work OR 5 file edits send a one-line `PROGRESS: [just finished] → [doing next]`. If blocked for more than 5 min, send `BLOCKED: [reason]`.

**Resume runs, business blockers resolved:** Re-message (team persists, but re-spawn if needed):

> `FIX ROUND.` Blockers resolved since last run:
> - b-N: {question} → answer: {text}
> - b-M: {question} → answer: {text}
> Apply these to the business sections. Replace any `TBD (see Blockers → b-N)` placeholders with the answer. Update related AC, Examples, Testing Strategy as needed. Signal `SPEC ANALYST FIX ROUND DONE.` when ready.

**Resume runs, no business blockers resolved:** skip this sub-phase.

**Message loop** (runs during both new runs and fix rounds):

Loop until `SPEC ANALYST DONE.` or `SPEC ANALYST FIX ROUND DONE.`:
- On `SPEC ANALYST QUESTION FOR USER`: extract topic, context, question, options, expertise. Format for the user using the defer-aware prompt (embed context as the "Вопрос N/M" background). On answer → `SendMessage(to: "spec-analyst", "ANSWER: <text>")`. On defer → create a `### b-N` entry in the spec's Blockers section via Edit, then `SendMessage(to: "spec-analyst", "DEFERRED: b-N")`.
- On `SPEC ANALYST DONE.` or `SPEC ANALYST FIX ROUND DONE.`: break.
- On idle > 10 min without signal: send `STATUS CHECK` ping. On second silence, surface to the user.

### 2b. Architect

**New runs:** Spawn `spec-architect`. Send:

> Read your instructions: `~/.claude/agents/spec-architect.md`
> Spec path: `tasks/2-spec/{ID}-{slug}.md` (business sections already populated)
> Draft path: `{draft path}` — **read `## Decisions` (authoritative user decisions) and `## Codebase Observations` (verified codebase facts — API signatures, model fields, file paths, patterns, gotchas). Every numbered decision MUST be reflected in the architecture. Codebase observations are your primary reference for integration points.**
> User Phase 1 answers: {inline — supplementary context}
> Project root: `{working directory}`
> Project CLAUDE.md: `{path}`
> Fill the `## Architecture & Implementation Plan` section in place. Signal `SPEC ARCHITECT DONE.` when ready. Escalate ambiguities with `SPEC ARCHITECT QUESTION FOR USER` and wait.
> Heartbeat: as above.

**Resume runs, architecture blockers resolved:** Re-message analogously with `FIX ROUND.` and the resolved blocker answers.

**Message loop:** same shape as 2a, but with `spec-architect` and the Architect signal names.

### 2c. Critics (two agents in parallel)

Spawn both critics as teammates in the same team. They run in parallel — no dependencies between them.

#### 2c-i. Architecture Critic

Spawn `spec-critic-arch` (`Agent(name: "spec-critic-arch", team_name: "spec-{ID}")`). Send:

> Read your instructions: `~/.claude/agents/spec-critic-arch.md`
> Spec path: `tasks/2-spec/{ID}-{slug}.md`
> Draft path: `{draft path}` — **read `## Decisions` and verify EVERY numbered decision is correctly reflected in the spec. Any mismatch = CRITICAL finding. Also read `## Codebase Observations` — verify spec's integration points and API claims match the recorded observations.**
> Working directory: `{project root}`
> Phase 1 context: {inline user answers and Lead observations}
> Project CLAUDE.md: `{path}`
> {On resume:} `RESUMED_RUN: true`
> Run your full verification and lens pass (Pass 1 + Lenses A–G). Signal `SPEC ARCH CRITIC REPORT` when done.
> Heartbeat: as above.

#### 2c-ii. Business Critic

Spawn `spec-critic-business` (`Agent(name: "spec-critic-business", team_name: "spec-{ID}")`). Send:

> Read your instructions: `~/.claude/agents/spec-critic-business.md`
> Spec path: `tasks/2-spec/{ID}-{slug}.md`
> Draft path: `{draft path}` — **read `## Decisions` and verify EVERY numbered decision is correctly reflected in the spec's business sections. Any mismatch = CRITICAL finding.**
> Working directory: `{project root}`
> Phase 1 context: {inline user answers and Lead observations}
> Project CLAUDE.md: `{path}`
> {On resume:} `RESUMED_RUN: true`
> Run your full business quality lens pass (Lenses G–R). Signal `SPEC BUSINESS CRITIC REPORT` when done.
> Heartbeat: as above.

**Message loops:** run both in parallel. Both critics rarely escalate; if either does, handle like any other `QUESTION FOR USER`.

#### Optional: GPT-5.4 third critic

If `which opencode` succeeds, launch a GPT-5.4 architecture critic in parallel with the two teammates. Read and follow `~/.claude/guides/opencode-review-runner.md` for the full subprocess lifecycle (launch, parsing, validation, timeout, retry). Use `spec-critic-arch` as the agent and `github-copilot/gpt-5.4` as the model. Pass the same inputs as the arch critic above (spec path, working directory, Phase 1 context, CLAUDE.md path, draft path with `## Decisions`, `RESUMED_RUN: true` on resume runs).

**Important:** opencode in `--pure` mode cannot read files outside the project directory (`~/.claude/agents/` is auto-rejected). Use the symlink `.claude/agents-global/spec-critic-arch.md` instead. If the symlink doesn't exist, inline the agent instructions directly in the prompt.

The GPT-5.4 critic runs non-interactively — it cannot participate in the `QUESTION FOR USER` message loop. Add to its prompt: "Do not emit SPEC ARCH CRITIC QUESTION FOR USER. If you encounter ambiguity, record it as a finding instead." Also add: "Read `## Decisions` in the draft file and verify EVERY numbered decision is reflected in the spec. Any mismatch = CRITICAL finding." The teammate critics handle all interactive escalation.

Wait for all critics (both teammates + GPT-5.4 if launched) to complete before proceeding.

### 2d. Apply findings

Merge findings from all critics (arch critic, business critic, and GPT-5.4 critic if launched). This includes `EMERGENT QUESTIONS FOR USER` from all sources — they all feed into Phase 3. Deduplicate findings that flag the same issue — keep the more specific description. If the GPT-5.4 critic was not launched or failed — proceed with the two teammate reports only.

After reports are collected:

- **Business findings** (`route: analyst`) → `SendMessage(to: "spec-analyst", ...)` with the specific findings, request fixes. Run the Analyst message loop again until `SPEC ANALYST FIX ROUND DONE.`.
- **Architecture findings** (`route: architect`) → `SendMessage(to: "spec-architect", ...)`. Run the Architect message loop until `SPEC ARCHITECT FIX ROUND DONE.`.
- **After fixes**: optionally re-check with the appropriate critic:
  - Business findings: `SendMessage(to: "spec-critic-business", "RE-CHECK OF: [f-1, f-3]")` → wait for `SPEC BUSINESS CRITIC RE-CHECK DONE.`
  - Architecture findings: `SendMessage(to: "spec-critic-arch", "RE-CHECK OF: [f-2, f-4]")` → wait for `SPEC ARCH CRITIC RE-CHECK DONE.`
- **Maximum 2 fix rounds per teammate.** After two rounds, unresolved business concerns stay in `Edge Cases & Risks`, unresolved architectural concerns stay in `Open architectural questions`. Phase 3 picks them up if they need user input.
- **Tiny edits** (typo, missing bullet): Lead may Edit the spec file directly instead of round-tripping through a teammate.
- **`EMERGENT QUESTIONS FOR USER`**: deferred to Phase 3, do not resolve here.

## 3. Phase 3 — Post-spec clarification

Many open questions only become visible after Analyst describes behavior, Architect lays out files, and Critic hunts gaps. Phase 1 catches what's askable upfront; Phase 3 catches what emerges from the team's work.

### Collect open questions

Gather from:
- `Edge Cases & Risks` — table rows with `Status: OPEN` that still need clarification
- `Architecture & Implementation Plan → Open architectural questions`
- Critic's `EMERGENT QUESTIONS FOR USER` (each carries an expertise tag)

### Classify

Tag each question as **user-required** or **auto-resolvable**:

- **user-required** — business decisions, domain context, trade-offs, unknowns about production data, UX decisions, anything where the wrong answer creates rework downstream.
- **auto-resolvable** — pure technical defaults (e.g. `index=True` on a foreign key), project conventions documented in `CLAUDE.md`, safe-by-default choices where one option is clearly safer than the other.

**Rule of doubt:** if you are not sure which category, ask the user. A 30-second question is cheaper than a wrong default that surfaces during `/implement`. Per the project's requirement, no silent auto-resolution: even when you pick a safe default for an auto-resolvable question, present it to the user as one option among others and let them accept or override.

### Ask

Use the defer-aware prompt format, one question at a time. Each question carries the full context: what was found in the code, what the spec says, why this question matters.

### Apply answers

Each answer is reflected in the spec immediately:
- Mark the matching item `RESOLVED (user: <answer>)`
- If the answer changes the Architecture section: `SendMessage` to spec-architect, or Edit directly for small fixes
- If the answer adds new ACs: append to Acceptance Criteria and ask Architect to extend the AC → Implementation map
- If the answer changes scope: update In Scope / Out of Scope and ripple the architectural consequences

### On defer in Phase 3

Create a new `### b-N` entry in `## Blockers` following the same format. Continue with the next question.

## Progress commits

Commit work-in-progress at these checkpoints to avoid losing progress:

1. **After Phase 1** — draft with `## Decisions` and `## Codebase Observations`. Message: `spec({ID}): Phase 1 decisions and codebase observations`
2. **After Analyst** — spec with business sections. Message: `spec({ID}): business sections (Analyst)`
3. **After Architect** — spec with architecture. Message: `spec({ID}): architecture plan (Architect)`
4. **After fix rounds** — spec with critic fixes. Message: `spec({ID}): apply critic findings`
5. **After finalization** — final spec + archived draft. Message: `spec({ID}): finalize spec`

Use `git add` on specific files only (draft, spec). Run commits with `run_in_background: true` (pre-commit hook may take time).

## 4. Finalization

1. Read the spec file.
2. Parse `## Blockers`. Count level-3 entries with `status: open`.
3. Verify the AC → Implementation map covers every AC in Acceptance Criteria.
4. Verify `## Examples` has entries for non-trivial Behavior rules.
5. Verify `## Definition of Done` has been populated (items either checked, left unchecked for the human, or marked `N/A — <reason>`).
6. Verify `## Key Constraints` has 3-7 items, each tracing to Behavior or AC.
7. Verify `## Assumptions` is populated (not just the template placeholder).
8. Verify exactly one `[SENTINEL]` marker exists in the Behavior section.

### If open blockers > 0

- Move the draft to `tasks/archive/drafts/` (the draft is consumed either way).
- Leave the spec in `tasks/2-spec/` with `status: awaiting-approval` unchanged.
- Output:

  ```
  Spec {ID} saved with {N} open blockers in ## Blockers section.

  Open blockers:
    - b-1 (expertise: architecture): {short question}
    - b-2 (expertise: business): {short question}

  Run /spec {ID} again when a person with matching expertise can answer them.
  /task-approve will refuse to approve until Blockers is clean.
  ```

Stop.

### If open blockers == 0

- Move the draft to `tasks/archive/drafts/`.
- Output:
  - Brief spec summary (3-5 sentences)
  - Number of acceptance criteria
  - Number of files in Work breakdown and number of Coders
  - Key risks if any
  - Next step: `Review the spec, make edits if needed, then run /task-approve {ID}.`
