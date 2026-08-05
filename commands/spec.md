Generate a specification from a draft task using addressable background agents. This command is orchestration-only — the Analyst, Architect, and Critic bodies live in `~/.claude/agents/spec-{analyst,architect,critic}.md`.

## 0. Setup

1. Read `.tasks.toml` for `id_prefix` and `dir`. Several `.tasks.toml` in the repo (root plus `*/.tasks.toml`, `*/*/.tasks.toml`, skipping `node_modules/`, `.git/`, `vendor/` and plugin/cache directories) means several SDD roots — use the one whose `id_prefix` matches the task ID. `{dir}` below is that config's `dir`, resolved relative to the config's own directory. None found → tell user to run `/task-init` and stop.
2. Locate the target by `$ARGUMENTS` (ID, slug, or full path):
   - Match in `{dir}/1-draft/` → `RUN_MODE = new`.
   - Match in `{dir}/2-spec/` → `RUN_MODE = resume` (the spec already exists; you are re-entering it to resolve open blockers or apply late findings).
   - Not found → error and stop.
3. Read the draft file content (new) or the existing spec file (resume).
4. Read the project `CLAUDE.md` for stack and conventions.

## 1. Phase 1 — Discovery (new runs only)

Skip this section on resume runs; jump to Phase 1.5.

This phase is **mandatory** for new runs and cannot be skipped.

1. Read the draft task carefully.
2. **Research the codebase — fan out, and research yourself in parallel.** Spawn 2–3 `Spec-Researcher` agents in one batch, record their `agentId`s in the registry, and arm `WATCHDOG_RESEARCH` (`Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_RESEARCH")`). Use 2 angles for a draft inside one module, 3 when it spans modules:

   | `RESEARCH_ANGLE` | Covers |
   |---|---|
   | `domain and data model` | entities, fields, states, relationships the draft touches |
   | `existing behavior and call-sites` | what happens today, who calls it, where the change lands |
   | `conventions and analogous features` | ≥2 similar features already built, how they are structured and tested |

   Each spawn is `Agent(subagent_type: "Spec-Researcher", name: "researcher-{angle-slug}", run_in_background: true, prompt: "...")` with:

   > Read your instructions: `~/.claude/agents/spec-researcher.md`
   > RESEARCH_ANGLE: {angle}
   > Draft path: `{draft path}`
   > Working directory: `{project root}`
   > Project CLAUDE.md: `{path}`
   > Signal `SPEC RESEARCH REPORT [{angle}]` when done.

   **Research yourself while they run** — do not wait idle. Delegating every angle leaves you judging questions from other agents' summaries, and a summary is exactly where an unverified premise slips through unnoticed.

   Reject any report missing its DEPTH block, or carrying observations without `path:line`, and re-request a deeper pass — the same rule the critics get.

3. **Merge into `## Codebase Observations`.** Fold every researcher report and your own findings into the draft's `## Codebase Observations` section via `Edit` — one line per fact, **each carrying `path:line`**. Copy `CONTRADICTIONS` across verbatim: a draft assumption the code disproves outranks anything you were about to ask the user. Then run a **gap check** — name the parts of the draft nobody covered, and spawn one follow-up researcher on any material dark area before moving to questions.

4. Compile a list of clarifying questions. Topics to cover:
   - **Цель**: Какая бизнес-задача решается? Кому и как это поможет?
   - **Границы**: Что явно НЕ входит в задачу? Есть ли смежные фичи, которые трогать не нужно?
   - **Поведение**: Любые неоднозначные сценарии — спроси, не додумывай.
   - **Крайние случаи**: Что происходит при пустых данных, ошибках, нехватке прав, больших объёмах?
   - **Приоритет и ограничения**: Есть ли дедлайны, требования к производительности, зависимости от другой работы?
   - **Существующее поведение**: Если драфт меняет существующую функциональность — уточни, что сейчас и что именно должно измениться.
   - **Архитектура и интеграция**: Новый модуль или расширение существующего? Есть ли конвенция для похожих фич? Спрашивай ТОЛЬКО когда ответ не очевиден из кодовой базы.
5. Ask questions **one at a time** using the defer-aware prompt format below. Every question passes the gate first.
6. **After each answer**, immediately append the decision to the draft file under a `## Decisions` section using `Edit`. Number each decision sequentially. Format: `N. **Short label**: decision text`. This section becomes the authoritative source of user decisions for all agents — inline prompt text is supplementary.
7. After each answer, if it reveals new ambiguities, add follow-up questions to the queue. Continue until no questions remain — ask as many as genuinely matter, no padding to a count.
8. When the queue is empty the draft is finished — commit it once, as commit 1 of `## Commits`. The `Edit` in step 6 is what persists each answer; the single commit lands them all as one history entry.

**Rules for this phase:**
- Frame questions in business/domain terms, except architectural topics which are technical by nature.
- Architectural questions only when the codebase doesn't give a clear answer; if there's an obvious convention, note it as context for the Architect, don't ask.
- Include what you learned from exploring the codebase as context ("Я вижу, что сейчас система делает X — Y должен заменить это или работать параллельно?").
- One question at a time.

## Before any question — the gate

Run these five steps on **each** candidate question before it reaches the user. Phase 1 research tells you what the codebase contains; the gate confirms that *this specific question* is worth asking and correctly posed.

1. **Locate.** Grep for where the answer would live — models, call-sites, existing conventions.
2. **Verify the premise.** Confirm that everything the question presupposes actually exists. "Should X replace Y?" is void when `Y` does not exist. Cite what you found.
3. **Verify your understanding.** State in one line what the code does today, with `path:line`. Being unable to state it means you are not ready to ask — go back to step 1.
4. **Re-judge the question.** Three outcomes:
   - The code answers it → resolve it yourself, record it in `## Codebase Observations` as context, and drop the question.
   - The premise was wrong → rewrite the question against what the code actually does, or drop it. When the draft itself carried the wrong premise, tell the user that finding instead — it is worth more than the question was.
   - It is a genuine decision, with downstream consequences where a wrong guess causes rework → ask it, carrying the evidence into the question's context slot.
5. **Evidence requirement.** Every question you put to the user carries either ≥1 `path:line` citation, or an explicit "nothing in the codebase covers this — greped X, Y, Z". A question with neither is not ready.

Ask as many as genuinely matter — never pad to a count, and never trim a real decision to look efficient. When you are unsure whether something is a genuine decision, ask: a 30-second question beats a silent wrong default.

**Scope.** The gate applies to every question in every phase — Phase 1, Phase 1.5 blocker re-asks, agent escalations, and Phase 3.

<bad_pattern>
❌ BAD THOUGHT: "I broadly get what this module does — I'll ask the user and pick up the details from their answer."
✅ REALITY: A question built on an unverified premise teaches the user nothing and costs a round trip. Worse, they answer it as posed — and now the wrong premise is a numbered Decision that every downstream agent treats as authoritative.
⚠️ DETECTION: About to ask a question with no `path:line` in its context and no explicit "not in the codebase" note? → research it first.
</bad_pattern>

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
- **raised-by**: lead (Phase 1 / Phase 3) | spec-analyst | spec-architect | spec-critic-arch | spec-critic-business | spec-critic-premise | spec-critic-adaptive:{lens-id}
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
   - **Re-verify the stored premise before re-asking.** One cheap grep: does everything the blocker references still exist under that name? A blocker recorded weeks ago may name a model that has since been renamed or deleted, and re-asking it verbatim wastes the user's answer. When the premise still holds, reuse the stored context as-is — this is a premise check, not a fresh exploration. When it no longer holds, rewrite the question against current code and note the change in `deferred-history`.
   - Ask the user using the defer-aware prompt format.
   - On answer: update the blocker entry (status → `resolved-by-user`, append deferred-history line, fill resolution). Remember which agent to re-invoke based on who raised the blocker and the expertise-needed tag (`business` → Analyst, `architecture` → Architect, sometimes both).
   - On defer: append a new `deferred-history` line `{TODAY}: deferred again`. Keep `status: open`. Move to the next.
5. Build a set of affected agents from the resolved blockers. If zero blockers got resolved, tell the user "No blockers were resolved this run. Spec unchanged; come back later." and stop.

## 2. Phase 2 — Background agents

Spawn each agent via `Agent(subagent_type: "...", name: "...", run_in_background: true, prompt: "...")`. The call returns an `agentId` (format `a...-...`); the agent runs asynchronously and notifies you when it completes, its final message being the result.

<critical>
Record the `agentId` from every spawn into a small registry (`name | agentId | role`). A `name` reaches an agent **only while it is running**; once it completes, resume it only by `agentId` — and resuming preserves its context, so it remembers its prior work. This matters for fix rounds and re-checks, which happen after the agent has completed.
</critical>

**Addressing:** running agent → `SendMessage(to: "spec-analyst")`; completed agent → `SendMessage(to: "{agentId}")`. The completion notification is your done signal — do not poll for it.

**Liveness:** a dead agent sends no completion notification and nothing else wakes you. Follow `~/.claude/templates/liveness-protocol.md`: arm a dead-man timer per phase (`Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_ANALYST")` — likewise `WATCHDOG_RESEARCH`, `WATCHDOG_ARCHITECT`, `WATCHDOG_CRITICS`, and `WATCHDOG_ADAPTIVE`), audit on every wake-up, recover via ping-by-`agentId` first, respawn as escalation.

The Phase 1 researchers are background agents too: the registry, the addressing rules, and the liveness protocol above cover them under `WATCHDOG_RESEARCH`.

Shared context to pass in every agent prompt:
- Draft path (or existing spec path on resume) — **instruct agents to read `## Decisions` (authoritative user decisions) and `## Codebase Observations` (verified facts about the codebase) from the draft file. These two sections are the persistent source of truth — inline prompt context is supplementary.**
- User's Phase 1 answers (new runs) or resolved-blocker answers (resume runs) — inline as supplementary context
- Project `CLAUDE.md` path

### 2a. Analyst

**New runs:** Spawn `Agent(subagent_type: "Spec-Analyst", name: "spec-analyst", run_in_background: true, prompt: "...")` and record its `agentId`. The prompt:

> Read your instructions: `~/.claude/agents/spec-analyst.md`
> Spec output path: `{dir}/2-spec/{ID}-{slug}.md`
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
> Spec path: `{dir}/2-spec/{ID}-{slug}.md` (business sections already populated)
> Draft path: `{draft path}` — **read `## Decisions` (authoritative user decisions) and `## Codebase Observations` (verified codebase facts — API signatures, model fields, file paths, patterns, gotchas). Every numbered decision MUST be reflected in the architecture. Codebase observations are your primary reference for integration points.**
> User Phase 1 answers: {inline — supplementary context}
> Project root: `{working directory}`
> Project CLAUDE.md: `{path}`
> Before writing the Architecture section, produce the three "Deep codebase exploration" artifacts (analogous features ≥2, vendor/base classes read, integration call-sites) from your instructions file, and attach them under "Exploration evidence" in your `SPEC ARCHITECT DONE.` message. Treat vendor code inside the repo as part of the project.
> Fill the `## Architecture & Implementation Plan` section in place. Signal `SPEC ARCHITECT DONE.` when ready. Escalate ambiguities with `SPEC ARCHITECT QUESTION FOR USER` and wait.

**Resume runs, architecture blockers resolved:** Resume the Architect by its `agentId` with `FIX ROUND.` and the resolved blocker answers.

**Message loop:** same shape as 2a, but with `spec-architect` and the Architect signal names.

### 2c. Critics (3 fixed + 3 Kimi mirrors + 3–5 adaptive lenses, in parallel)

Each fixed critic runs on **two engines**: the native Claude critic AND a `Kimi-Mirror` running the same lens pass on the Kimi CLI — two independent passes per critic, "ревью много не бывает". On top of those six, you design **3–5 adaptive lenses** for this specific spec (2c-0) and spawn one critic per lens.

Spawn everything — fixed critics, mirrors, and adaptive lenses — as background agents in **one batch** (all `Agent` calls in a single response). They run in parallel; there are no dependencies. Record every `agentId`.

Arm two watchdogs: `Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_CRITICS")` for the six fixed agents, and `Bash(run_in_background: true, command: "sleep 1500; echo WATCHDOG_ADAPTIVE")` for the adaptive ones. The batch now exceeds the concurrency cap, so the later spawns queue for slots — a single 900s timer over the whole batch fires on healthy-but-queued agents, and the respawns it triggers make the contention worse.

The three native critic spawns are 2c-i / 2c-ii / 2c-iii below; each gets a mirror via the template in **2c-iv**; the adaptive lenses spawn via **2c-v**.

#### 2c-0. Lens design (think before you spawn)

The three fixed critics read the spec as a document. What they cannot do is cover the angle *this* spec needs — that depends on its domain, size, and complexity, and it is yours to work out. On the spec that motivated this phase, four self-designed angles found more than all the fixed passes combined, because they looked at the system rather than the text: through a tester's eyes, through an attacker's eyes, against existing production data, and against concurrent actions.

1. **Read the fixed lens inventories** in `~/.claude/agents/spec-critic-{arch,business,premise}.md`. You are designing angles they do not cover, so you need to know what they do: arch Lens C already simulates state transitions and Lens D already covers data consistency after migration. Restating one of those spends a slot on covered ground.
2. **Choose how many** — 3 for ≤5 ACs in a single module; 4 for 6–15 ACs; 5 for >15 ACs, or multi-module, or anything touching data migration, concurrent access, or external integrations.
3. **Write each lens** as four fields:
   - `lens-id` — short slug, e.g. `concurrent-actions`
   - `angle` — the stance, in one line
   - `justification` — why this spec needs it, **citing something concrete in the spec**: an AC number, a file path, a named state transition, a model name
   - `hunt` — the failure classes this angle should surface
4. **Prefer system-level angles over text-level ones.** Non-exhaustive seeds: tester's eyes, attacker's eyes, existing production data, concurrent actions, operations and observability, performance at real scale, permissions and multi-tenancy, failure and rollback, cost. Treat this as a starting point, not a menu — **at least one lens must be specific to this spec's domain and appear on no list.** The justification citation is what separates a designed angle from a picked one.
5. **Announce** the chosen lenses to the user with one line of rationale each, then proceed. This is your call to make — do not wait for approval.
6. **Record them** in the spec's `## Review Lenses` section via `Edit`, before spawning — appending your entries below the section's italic *"Review metadata — not requirements"* line and leaving that line in place, since it is what keeps `Spec-Auditor` from tracing lenses to code during `/implement`. The briefs otherwise live only in a spawn prompt, and a resume run would lose them.

<bad_pattern>
❌ BAD THOUGHT: "Architecture, business, premise — that's every angle there is. Spawn the batch."
✅ REALITY: Those three are the angles every spec gets. The ones that pay for themselves are the ones only this spec needs, and nobody but you is positioned to name them.
⚠️ DETECTION: About to spawn the critic batch with no `## Review Lenses` block in the spec? → design the lenses first.
</bad_pattern>

#### 2c-i. Architecture Critic

Spawn `Agent(subagent_type: "Spec-Critic-Arch", name: "spec-critic-arch", run_in_background: true, prompt: "...")`. The prompt:

> Read your instructions: `~/.claude/agents/spec-critic-arch.md`
> Spec path: `{dir}/2-spec/{ID}-{slug}.md`
> Draft path: `{draft path}` — **read `## Decisions` and verify EVERY numbered decision is correctly reflected in the spec. Any mismatch = CRITICAL finding. Also read `## Codebase Observations` — verify spec's integration points and API claims match the recorded observations.**
> Working directory: `{project root}`
> Phase 1 context: {inline user answers and Lead observations}
> Project CLAUDE.md: `{path}`
> {On resume:} `RESUMED_RUN: true`
> Run your full verification and lens pass (Pass 1 + Lenses A–G). Signal `SPEC ARCH CRITIC REPORT` when done.

#### 2c-ii. Business Critic

Spawn `Agent(subagent_type: "Spec-Critic-Business", name: "spec-critic-business", run_in_background: true, prompt: "...")`. The prompt:

> Read your instructions: `~/.claude/agents/spec-critic-business.md`
> Spec path: `{dir}/2-spec/{ID}-{slug}.md`
> Draft path: `{draft path}` — **read `## Decisions` and verify EVERY numbered decision is correctly reflected in the spec's business sections. Any mismatch = CRITICAL finding.**
> Working directory: `{project root}`
> Phase 1 context: {inline user answers and Lead observations}
> Project CLAUDE.md: `{path}`
> {On resume:} `RESUMED_RUN: true`
> Run your full business quality lens pass (Lenses G–R). Signal `SPEC BUSINESS CRITIC REPORT` when done.

#### 2c-iii. Premise Critic

Spawn `Agent(subagent_type: "Spec-Critic-Premise", name: "spec-critic-premise", run_in_background: true, prompt: "...")`. The prompt:

> Read your instructions: `~/.claude/agents/spec-critic-premise.md`
> Spec path: `{dir}/2-spec/{ID}-{slug}.md`
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
Spec path: {dir}/2-spec/{ID}-{slug}.md
Draft path: {draft path}
Mirror the native critic's lens pass exactly and relay Kimi's report verbatim, with ` (Kimi)` appended to the report's identifier line (e.g. `SPEC ARCH CRITIC REPORT (Kimi)`)."
)
```

#### 2c-v. Adaptive lens critics (one per lens from 2c-0)

In the same batch, spawn one `Spec-Critic-Adaptive` per lens designed in 2c-0. These run on Claude only — no Kimi mirrors. The payoff comes from a *new angle*, not a second engine on an angle already covered, and the batch is already at the concurrency cap.

```
Agent(
  subagent_type: "Spec-Critic-Adaptive",
  name: "adaptive-{lens-id}",
  run_in_background: true,
  prompt: "Read your instructions: ~/.claude/agents/spec-critic-adaptive.md
LENS_ID: {lens-id}
LENS_ANGLE: {angle}
LENS_JUSTIFICATION: {why this spec needs it}
LENS_HUNT: {failure classes to surface}
Spec path: {dir}/2-spec/{ID}-{slug}.md
Draft path: {draft path} — read `## Decisions` and `## Codebase Observations` for the verified ground.
Working directory: {project root}
Phase 1 context: {inline user answers and Lead observations}
Project CLAUDE.md: {path}
{On resume:} RESUMED_RUN: true
Run your lens pass. Signal `SPEC ADAPTIVE CRITIC REPORT [{lens-id}]` when done."
)
```

**Message loops:** run the fixed critics, their mirrors, and the adaptive lenses in parallel. Critics rarely escalate; if any (native, mirror, or adaptive) does, handle it like any other `QUESTION FOR USER`. A mirror returning `KIMI_SUSPICIOUS` / `KIMI_FAILED` instead of a report is re-run per `kimi-mirror.md`. An adaptive report missing its DEPTH or VERIFIED OK block is rejected and re-requested, same as any critic report.

Wait for **every** agent in the batch — three critics, three mirrors, and all adaptive lenses — to complete before proceeding.

### 2d. Apply findings

You now hold three native critic reports, their three Kimi mirrors, and one report per adaptive lens. Merge them: first fold each mirror into its native pair (arch native + `kimi-critic-arch`, business + `kimi-critic-business`, premise + `kimi-critic-premise`), then merge across all reports including the adaptive ones. **Dedup** — findings that flag the same issue (within a pair, across critics, or between a fixed critic and an adaptive lens) collapse to one, keeping the more specific description. **Union of severity** — a finding raised by *any* source counts; a mirror-only or lens-only catch is still a catch. This includes `EMERGENT QUESTIONS FOR USER` from every source — they all feed into Phase 3. The premise mirror's questions dedup against the native premise critic's before Phase 3.

Adaptive findings carry the same `route:` tags and flow through the fix rounds below exactly like critic findings. Where an adaptive lens and a fixed critic disagree, keep both and let the fix round resolve it — divergence is signal.

The premise critic is different in kind: it challenges decisions, not implementation. Its findings are mostly `route: user` and feed **Phase 3** directly — they are not applied through the analyst/architect fix loop below, because only the user can re-decide a decision. The one exception is a premise finding with `route: analyst` (an assumption factually contradicted by code), which joins the analyst fix round. The premise critic gets **no re-check** pass.

The adaptive lenses also get **no re-check** pass. Their findings route to Analyst and Architect normally and land inside the existing 2-fix-round cap; re-checking each lens as well would multiply the rounds without adding coverage.

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
- Every critic's `EMERGENT QUESTIONS FOR USER` — the three fixed ones (arch, business, premise), their mirrors, and each adaptive lens; each carries an expertise tag. The premise critic's questions challenge decisions the user already made — present them as genuine reconsiderations, not as gaps. An adaptive lens's questions carry its `lens-id`, so the user can see which angle raised them.

### Classify

Tag each question as **user-required** or **auto-resolvable**:

- **user-required** — business decisions, domain context, trade-offs, unknowns about production data, UX decisions, anything where the wrong answer creates rework downstream.
- **auto-resolvable** — pure technical defaults (e.g. `index=True` on a foreign key), project conventions documented in `CLAUDE.md`, safe-by-default choices where one option is clearly safer than the other.

**Rule of doubt:** if you are not sure which category, ask the user. A 30-second question is cheaper than a wrong default that surfaces during `/implement`. Per the project's requirement, no silent auto-resolution: even when you pick a safe default for an auto-resolvable question, present it to the user as one option among others and let them accept or override.

### Ask

Every question passes **the gate** (§ *Before any question*) first — including the ones that arrived from a critic. These need it most: they come from another agent's summary rather than from code you read yourself, so their premises are the least verified in the run. Verify the premise against the codebase before putting a critic's question to the user.

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
git add "{dir}/1-draft/{ID}-{slug}.md"
git commit -m "spec({ID}): draft with decisions and codebase observations"
```

**Commit 2 — finalization.** The spec is verified and the draft has moved to the archive (§4, step 9).

```
git add -A -- "{dir}/2-spec/{ID}-{slug}.md" "{dir}/archive/drafts/{ID}-{slug}.md"
git add -A -- "{dir}/1-draft/{ID}-{slug}.md" 2>/dev/null || true
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
9. Move the draft to `{dir}/archive/drafts/` — it is consumed either way, blockers or not.
10. Commit the spec and the archived draft together, as commit 2 of `## Commits`. This is the run's last action; the branches below only produce output.

### If open blockers > 0

The spec stays in `{dir}/2-spec/` with `status: awaiting-approval` unchanged. Output:

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
