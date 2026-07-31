Generate a specification from a draft task using addressable background agents. This command is orchestration-only — the Analyst, Architect, and Critic bodies live in `~/.claude/agents/spec-{analyst,architect,critic}.md`.

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
6. After each answer, if it reveals new ambiguities, add follow-up questions to the queue. Continue until no questions remain — ask as many as genuinely matter, no padding to a count.
7. When the queue is empty the draft is finished — commit it once, as commit 1 of `## Commits`. The `Edit` in step 5 is what persists each answer; the single commit lands them all as one history entry.

**Rules for this phase:**
- Frame questions in business/domain terms, except architectural topics which are technical by nature.
- Architectural questions only when the codebase doesn't give a clear answer; if there's an obvious convention, note it as context for the Architect, don't ask.
- Include what you learned from exploring the codebase as context ("Я вижу, что сейчас система делает X — Y должен заменить это или работать параллельно?").
- One question at a time.

## Before any question — the gate

1. Dig the code first. Find the answer where it lives — models, call-sites, existing conventions — before forming a question.
2. Resolve it yourself when you can. A purely technical point the code answers, or an obvious yes (e.g. "should I do the task at all?"), needs no question — decide and record it as context.
3. Ask only genuine decisions — ones with downstream consequences where a wrong guess causes rework. When unsure which kind it is, ask: a 30-second question beats a silent wrong default.
4. Ask as many as genuinely matter — never pad to a count.

**Language.** Run the QA session in Russian — questions, options, and the +/− trade-offs the user reads and answers. Everything that persists is English — spec sections, plan, code, commit messages, and recorded Decisions/Blockers (translate the gist of the user's Russian answer).

## Defer-aware prompt format

Every question to the user — in Phase 1, Phase 1.5, agent escalations, or Phase 3 — uses this format:

```
**Вопрос N/M**: {контекст для человека ВНЕ задачи: о чём вопрос, что ты нашёл в коде/спеке, почему выбор важен и чем грозит ошибка}

{Сам вопрос}

Варианты:
1. {вариант А} — + {плюс}; − {минус}
2. {вариант Б} — + {плюс}; − {минус}
3. {вариант В — если нужен} — + {плюс}; − {минус}
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

Each blocker is a level-3 heading inside the spec's `## Blockers` section. Generate `b-N` by counting existing `### b-` headings and taking the next integer (first is `b-1`). The spec is an English artifact: record every field in English (translate the gist of the Russian Q&A — the question need not be verbatim).

```markdown
### b-N — <short title summarizing the question>
- **status**: open
- **raised-by**: lead (Phase 1 / Phase 3) | spec-analyst | spec-architect | spec-critic-arch | spec-critic-business | spec-critic-premise
- **raised-on**: {TODAY}
- **expertise-needed**: business | architecture | testing | security | ux | unknown
- **context**: <what was found in the code or spec, what's ambiguous, what each option would imply>
- **question**: <the question you asked the user, in English>
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
   - On answer: update the blocker entry (status → `resolved-by-user`, append deferred-history line, fill resolution). Remember which agent to re-invoke based on who raised the blocker and the expertise-needed tag (`business` → Analyst, `architecture` → Architect, sometimes both).
   - On defer: append a new `deferred-history` line `{TODAY}: deferred again`. Keep `status: open`. Move to the next.
5. Build a set of affected agents from the resolved blockers. If zero blockers got resolved, tell the user "No blockers were resolved this run. Spec unchanged; come back later." and stop.

## 2. Phase 2 — Background agents

Spawn each agent via `Agent(subagent_type: "...", name: "...", run_in_background: true, prompt: "...")`. The call returns an `agentId` (format `a...-...`); the agent runs asynchronously and notifies you when it completes, its final message being the result.

<critical>
Record the `agentId` from every spawn into a small registry (`name | agentId | role`). A `name` reaches an agent **only while it is running**; once it completes, resume it only by `agentId` — and resuming preserves its context, so it remembers its prior work. This matters for fix rounds and re-checks, which happen after the agent has completed.
</critical>

**Addressing:** running agent → `SendMessage(to: "spec-analyst")`; completed agent → `SendMessage(to: "{agentId}")`. The completion notification is your done signal — do not poll for it.

**Liveness:** a dead agent sends no completion notification and nothing else wakes you. Follow `~/.claude/templates/liveness-protocol.md`: arm a dead-man timer per phase (`Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_ANALYST")` — likewise `WATCHDOG_ARCHITECT`, `WATCHDOG_CRITICS`), audit on every wake-up, recover via ping-by-`agentId` first, respawn as escalation.

Shared context to pass in every agent prompt:
- Draft path (or existing spec path on resume) — **instruct agents to read `## Decisions` (authoritative user decisions) and `## Codebase Observations` (verified facts about the codebase) from the draft file. These two sections are the persistent source of truth — inline prompt context is supplementary.**
- User's Phase 1 answers (new runs) or resolved-blocker answers (resume runs) — inline as supplementary context
- Project `CLAUDE.md` path

### 2a. Analyst

**New runs:** Spawn `Agent(subagent_type: "Spec-Analyst", name: "spec-analyst", run_in_background: true, prompt: "...")` and record its `agentId`. The prompt:

> Read your instructions: `~/.claude/agents/spec-analyst.md`
> Spec output path: `tasks/2-spec/{ID}-{slug}.md`
> Spec template: `~/.claude/templates/sdd/spec.md`
> Draft path: `{draft path}` — **read `## Decisions` (authoritative user decisions) and `## Codebase Observations` (verified codebase facts). Every numbered decision MUST be reflected in the spec. Codebase observations inform your writing but don't need 1:1 mapping.**
> User Phase 1 answers: {inline all answers — supplementary context}
> Project CLAUDE.md: `{path}`
> Write the business sections (including Key Constraints, Assumptions, and one `[SENTINEL]` marker in Behavior). Signal `SPEC ANALYST DONE.` when ready. Escalate ambiguities with `SPEC ANALYST QUESTION FOR USER` and wait for my reply.

**Resume runs, business blockers resolved:** Resume the Analyst by its `agentId` (context preserved):

> `FIX ROUND.` Blockers resolved since last run:
> - b-N: {question} → answer: {text}
> - b-M: {question} → answer: {text}
> Apply these to the business sections. Replace any `TBD (see Blockers → b-N)` placeholders with the answer. Update related AC, Examples, Testing Strategy as needed. Signal `SPEC ANALYST FIX ROUND DONE.` when ready.

**Resume runs, no business blockers resolved:** skip this sub-phase.

**Message loop** (runs during both new runs and fix rounds):

Loop until `SPEC ANALYST DONE.` or `SPEC ANALYST FIX ROUND DONE.`:
- On `SPEC ANALYST QUESTION FOR USER`: extract topic, context, question, options, expertise. Format for the user using the defer-aware prompt (embed context as the "Вопрос N/M" background). On answer → `SendMessage(to: "spec-analyst", "ANSWER: <text>")`. On defer → create a `### b-N` entry in the spec's Blockers section via Edit, then `SendMessage(to: "spec-analyst", "DEFERRED: b-N")`.
- On `SPEC ANALYST DONE.` or `SPEC ANALYST FIX ROUND DONE.`: break.
- On `WATCHDOG_ANALYST` firing with no completion: run the liveness check from the protocol — `TaskList` status, then ping by `agentId` (a dead agent is not reachable by `name`), respawn as escalation.

### 2b. Architect

**New runs:** Spawn `Agent(subagent_type: "Spec-Architect", name: "spec-architect", run_in_background: true, prompt: "...")` and record its `agentId`. The prompt:

> Read your instructions: `~/.claude/agents/spec-architect.md`
> Spec path: `tasks/2-spec/{ID}-{slug}.md` (business sections already populated)
> Draft path: `{draft path}` — **read `## Decisions` (authoritative user decisions) and `## Codebase Observations` (verified codebase facts — API signatures, model fields, file paths, patterns, gotchas). Every numbered decision MUST be reflected in the architecture. Codebase observations are your primary reference for integration points.**
> User Phase 1 answers: {inline — supplementary context}
> Project root: `{working directory}`
> Project CLAUDE.md: `{path}`
> Before writing the Architecture section, produce the three "Deep codebase exploration" artifacts (analogous features ≥2, vendor/base classes read, integration call-sites) from your instructions file, and attach them under "Exploration evidence" in your `SPEC ARCHITECT DONE.` message. Treat vendor code inside the repo as part of the project.
> Fill the `## Architecture & Implementation Plan` section in place. Signal `SPEC ARCHITECT DONE.` when ready. Escalate ambiguities with `SPEC ARCHITECT QUESTION FOR USER` and wait.

**Resume runs, architecture blockers resolved:** Resume the Architect by its `agentId` with `FIX ROUND.` and the resolved blocker answers.

**Message loop:** same shape as 2a, but with `spec-architect` and the Architect signal names.

### 2c. Critics (three critics + three Kimi mirrors, in parallel)

Each critic runs on **two engines**: the native Claude critic AND a `Kimi-Mirror` running the same lens pass on the Kimi CLI — two independent passes per critic, "ревью много не бывает". Spawn all **six** as background agents in one batch (six `Agent` calls in a single response). They run in parallel — no dependencies. Record all six `agentId`s (three critics + three mirrors).

Arm one watchdog for the whole batch — critics and mirrors: `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_CRITICS")`.

The three native critic spawns are 2c-i / 2c-ii / 2c-iii below. Each also gets a mirror via the template in **2c-iv**.

#### 2c-i. Architecture Critic

Spawn `Agent(subagent_type: "Spec-Critic-Arch", name: "spec-critic-arch", run_in_background: true, prompt: "...")`. The prompt:

> Read your instructions: `~/.claude/agents/spec-critic-arch.md`
> Spec path: `tasks/2-spec/{ID}-{slug}.md`
> Draft path: `{draft path}` — **read `## Decisions` and verify EVERY numbered decision is correctly reflected in the spec. Any mismatch = CRITICAL finding. Also read `## Codebase Observations` — verify spec's integration points and API claims match the recorded observations.**
> Working directory: `{project root}`
> Phase 1 context: {inline user answers and Lead observations}
> Project CLAUDE.md: `{path}`
> {On resume:} `RESUMED_RUN: true`
> Run your full verification and lens pass (Pass 1 + Lenses A–G). Signal `SPEC ARCH CRITIC REPORT` when done.

#### 2c-ii. Business Critic

Spawn `Agent(subagent_type: "Spec-Critic-Business", name: "spec-critic-business", run_in_background: true, prompt: "...")`. The prompt:

> Read your instructions: `~/.claude/agents/spec-critic-business.md`
> Spec path: `tasks/2-spec/{ID}-{slug}.md`
> Draft path: `{draft path}` — **read `## Decisions` and verify EVERY numbered decision is correctly reflected in the spec's business sections. Any mismatch = CRITICAL finding.**
> Working directory: `{project root}`
> Phase 1 context: {inline user answers and Lead observations}
> Project CLAUDE.md: `{path}`
> {On resume:} `RESUMED_RUN: true`
> Run your full business quality lens pass (Lenses G–R). Signal `SPEC BUSINESS CRITIC REPORT` when done.

#### 2c-iii. Premise Critic

Spawn `Agent(subagent_type: "Spec-Critic-Premise", name: "spec-critic-premise", run_in_background: true, prompt: "...")`. The prompt:

> Read your instructions: `~/.claude/agents/spec-critic-premise.md`
> Spec path: `tasks/2-spec/{ID}-{slug}.md`
> Draft path: `{draft path}` — **read `## Decisions` and `## Codebase Observations`. Unlike the other agents, you do NOT treat Decisions as authoritative — they are exactly what you scrutinize. Treat every claim, including every recorded user decision, as a hypothesis to disprove.**
> Working directory: `{project root}`
> Phase 1 context: {inline user answers and Lead observations}
> Project CLAUDE.md: `{path}`
> {On resume:} `RESUMED_RUN: true`
> Run your full premise pass (Lenses L1–L6). A sound foundation yields zero challenges — never manufacture one. Signal `SPEC PREMISE CRITIC REPORT` when done.

#### 2c-iv. Kimi mirrors (one per critic)

In the same batch, spawn a `Kimi-Mirror` for each of the three critics. Each mirror reads its native critic file and re-runs the same lens pass on Kimi, relaying a report labelled `(Kimi)`:

| Mirrors critic | Kimi-Mirror name | `MIRROR_OF` | `PURPOSE` |
|---|---|---|---|
| Architecture Critic | `kimi-critic-arch` | `~/.claude/agents/spec-critic-arch.md` | `critic-arch` |
| Business Critic | `kimi-critic-business` | `~/.claude/agents/spec-critic-business.md` | `critic-business` |
| Premise Critic | `kimi-critic-premise` | `~/.claude/agents/spec-critic-premise.md` | `critic-premise` |

Each mirror spawn:

```
Agent(
  subagent_type: "Kimi-Mirror",
  name: "{kimi-critic-name}",
  run_in_background: true,
  prompt: "Read your instructions: ~/.claude/agents/kimi-mirror.md
MIRROR_OF: {native critic file}
PURPOSE: {critic-arch | critic-business | critic-premise}
Working directory (WORKTREE): {project root}
Spec path: tasks/2-spec/{ID}-{slug}.md
Draft path: {draft path}
Mirror the native critic's lens pass exactly and relay Kimi's report verbatim, with ` (Kimi)` appended to the report's identifier line (e.g. `SPEC ARCH CRITIC REPORT (Kimi)`)."
)
```

**Message loops:** run all three critics — and their mirrors — in parallel. The critics rarely escalate; if any (native or mirror) does, handle like any other `QUESTION FOR USER`. A mirror returning `KIMI_SUSPICIOUS` / `KIMI_FAILED` instead of a report is re-run per `kimi-mirror.md`.

Wait for all three critics **and all three mirrors** to complete before proceeding.

### 2d. Apply findings

You now hold **six** reports — three native critics and their three Kimi mirrors. Merge them: first fold each mirror into its native pair (arch native + `kimi-critic-arch`, business + `kimi-critic-business`, premise + `kimi-critic-premise`), then merge across critics. **Dedup** — findings that flag the same issue (within a pair or across critics) collapse to one, keeping the more specific description. **Union of severity** — a finding raised by *either* engine counts; a mirror-only catch is still a catch. This includes `EMERGENT QUESTIONS FOR USER` from all sources (native and mirror) — they all feed into Phase 3. The premise mirror's questions dedup against the native premise critic's before Phase 3.

The premise critic is different in kind: it challenges decisions, not implementation. Its findings are mostly `route: user` and feed **Phase 3** directly — they are not applied through the analyst/architect fix loop below, because only the user can re-decide a decision. The one exception is a premise finding with `route: analyst` (an assumption factually contradicted by code), which joins the analyst fix round. The premise critic gets **no re-check** pass.

The Analyst, Architect, and both consistency Critics (arch, business) have completed by now — resume each **by its `agentId`** (not by name). Their preserved context means they remember their prior work.

After reports are collected:

- **Business findings** (`route: analyst`) → resume the Analyst by `agentId` with the specific findings, request fixes. Run the Analyst message loop again until `SPEC ANALYST FIX ROUND DONE.`.
- **Architecture findings** (`route: architect`) → resume the Architect by `agentId`. Run the Architect message loop until `SPEC ARCHITECT FIX ROUND DONE.`.
- **After fixes**: optionally re-check with the appropriate critic, resumed by `agentId`:
  - Business findings: `SendMessage(to: "{business-critic agentId}", "RE-CHECK OF: [f-1, f-3]")` → wait for `SPEC BUSINESS CRITIC RE-CHECK DONE.`
  - Architecture findings: `SendMessage(to: "{arch-critic agentId}", "RE-CHECK OF: [f-2, f-4]")` → wait for `SPEC ARCH CRITIC RE-CHECK DONE.`
- **Maximum 2 fix rounds per agent.** After two rounds, unresolved business concerns stay in `Edge Cases & Risks`, unresolved architectural concerns stay in `Open architectural questions`. Phase 3 picks them up if they need user input.
- **Tiny edits** (typo, missing bullet): Lead may Edit the spec file directly instead of round-tripping through an agent.
- **`EMERGENT QUESTIONS FOR USER`**: deferred to Phase 3, do not resolve here.

## 3. Phase 3 — Post-spec clarification

Many open questions only become visible after Analyst describes behavior, Architect lays out files, and Critic hunts gaps. Phase 1 catches what's askable upfront; Phase 3 catches what emerges from the agents' work.

### Collect open questions

Gather from:
- `Edge Cases & Risks` — table rows with `Status: OPEN` that still need clarification
- `Architecture & Implementation Plan → Open architectural questions`
- All three critics' `EMERGENT QUESTIONS FOR USER` (arch, business, premise; each carries an expertise tag). The premise critic's questions challenge decisions the user already made — present them as genuine reconsiderations, not as gaps.

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

## Commits

A `/spec` run produces exactly **two** commits: the finished draft, then the finished spec. Every intermediate state — each answer, each agent round, each fix round — lives in the working tree, where `Edit` already persisted it to disk. Two commits per task keep the history readable; a commit per answer or per agent buries real changes under a dozen work-in-progress entries.

**Commit 1 — end of Phase 1.** The draft holds every decision in `## Decisions` and every finding in `## Codebase Observations`; the question queue is empty.

```
git add tasks/1-draft/{ID}-{slug}.md
git commit -m "spec({ID}): draft with decisions and codebase observations"
```

**Commit 2 — finalization.** The spec is verified and the draft has moved to the archive (§4, step 9).

```
git add -A -- tasks/2-spec/{ID}-{slug}.md tasks/archive/drafts/{ID}-{slug}.md
git add -A -- tasks/1-draft/{ID}-{slug}.md 2>/dev/null || true
git commit -m "spec({ID}): specification"
```

`-A` stages the draft's move as a rename (deletion + addition), so the archived draft lands together with the spec. The second `git add` is separate and error-tolerant on purpose: git aborts with exit 128 on a pathspec matching nothing, and the old draft path is absent on a resume run (the earlier run archived it) — a single combined command would kill the final commit of a 20-minute pipeline. A resume run produces commit 2 only, since Phase 1 is skipped and there is no draft to commit.

Run both commits with `run_in_background: true` — the pre-commit review hook can take up to 20 minutes.

<bad_pattern>
❌ BAD THOUGHT: "Phase 2 finished, better checkpoint the spec before the critics run."
✅ REALITY: `Edit` already wrote it to disk. A crash loses nothing; an extra commit permanently pollutes the history the user reads.
⚠️ DETECTION: About to run `git commit` anywhere other than end-of-Phase-1 or finalization? → record it with `Edit` instead.
</bad_pattern>

## 4. Finalization

1. Read the spec file.
2. Parse `## Blockers`. Count level-3 entries with `status: open`.
3. Verify the AC → Implementation map covers every AC in Acceptance Criteria.
4. Verify `## Examples` has entries for non-trivial Behavior rules.
5. Verify `## Definition of Done` has been populated (items either checked, left unchecked for the human, or marked `N/A — <reason>`).
6. Verify `## Key Constraints` has 3-7 items, each tracing to Behavior or AC.
7. Verify `## Assumptions` is populated (not just the template placeholder).
8. Verify exactly one `[SENTINEL]` marker exists in the Behavior section.
9. Move the draft to `tasks/archive/drafts/` — it is consumed either way, blockers or not.
10. Commit the spec and the archived draft together, as commit 2 of `## Commits`. This is the run's last action; the branches below only produce output.

### If open blockers > 0

The spec stays in `tasks/2-spec/` with `status: awaiting-approval` unchanged. Output:

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

- Output:
  - Brief spec summary (3-5 sentences)
  - Number of acceptance criteria
  - Number of files in Work breakdown and number of Coders
  - Key risks if any
  - Next step: `Review the spec, make edits if needed, then run /task-approve {ID}.`
