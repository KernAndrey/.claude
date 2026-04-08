Generate a specification from a draft task using an agent team.

## Instructions

1. Read `.tasks.toml`. If missing → tell user to run `/task-init` and stop.
2. Locate the draft by `$ARGUMENTS` — match by ID, slug, or full path in `tasks/1-draft/`.
3. Read the draft file content.
4. Read `CLAUDE.md` for project context.

## Phase 1: Discovery & Clarification (Lead — before spawning the team)

This phase is **mandatory** and cannot be skipped.

1. Read the draft task carefully.
2. Explore the project codebase: domain structure, existing behavior related to the draft, **top-level architecture (modules/addons layout, conventions for similar features, where comparable functionality lives today)**, constraints, conventions.
3. Compile a list of clarifying questions for the user. Topics to cover:
   - **Цель**: Какая бизнес-задача решается? Кому и как это поможет?
   - **Границы**: Что явно НЕ входит в задачу? Есть ли смежные фичи, которые трогать не нужно?
   - **Поведение**: Любые неоднозначные сценарии "как оно должно работать" — спроси, не додумывай.
   - **Крайние случаи**: Что происходит при пустых данных, ошибках, нехватке прав, больших объёмах?
   - **Приоритет и ограничения**: Есть ли дедлайны, требования к производительности, зависимости от другой работы?
   - **Существующее поведение**: Если драфт меняет существующую функциональность — уточни, что сейчас и что именно должно измениться.
   - **Архитектура и интеграция**: Это новый модуль/аддон или расширение существующего? Если расширение — какого именно? Есть ли в проекте конвенция для похожих фич, которой нужно следовать? Нужны ли точки расширения для будущих фич? Спрашивай ТОЛЬКО когда ответ не очевиден из кодовой базы — если есть явная конвенция, зафиксируй её как контекст для Архитектора, не задавай вопрос.
4. **Ask questions ONE AT A TIME.** Follow this format for each question:

   ```
   **Вопрос N/M**: {краткий контекст — что ты нашёл в кодовой базе, если релевантно}

   {Сам вопрос}

   Варианты:
   1. {вариант А}
   2. {вариант Б}
   3. {вариант В — если нужен}
   4. Другое (напиши свой вариант)
   ```

   - Always provide answer options based on what you learned from the codebase and the draft. Options should represent realistic choices, not filler.
   - The user can pick a number, write their own answer, or elaborate.
   - Wait for the user's answer before asking the next question.
   - Minimum 3 questions total, even if the draft seems clear.

5. After each answer, if it reveals new ambiguities — add follow-up questions to the queue. Continue until there are no more open questions. There is no limit on the number of rounds.

**Rules for this phase:**
- Questions and options are in **Russian**.
- Ask when something is unclear.
- This phase is mandatory — even a detailed draft has gaps only the user can fill.
- Frame questions in business/domain terms, not technical terms — **except** for the architectural topic, which is technical by nature.
- Architectural questions are allowed only when the codebase does not give a clear answer. If the project has an obvious convention for this kind of feature (e.g. similar features all live in addon X), do not ask — note it as context to pass to the Architect.
- Include what you learned from exploring the codebase as context (e.g. "Я вижу, что сейчас система делает X — Y должен заменить это или работать параллельно?").
- One question at a time.

Note: Phase 1 catches questions answerable upfront. Questions that only emerge after Analyst describes behavior and Architect lays out files are caught later in **Phase 3 (Post-spec Clarification)**, after the team has finished drafting.

## Phase 2: Specification (Agent Team)

Phase 2 runs the team **sequentially**: Analyst → Architect → Critic → fix rounds. Sequential (not parallel) avoids file-creation race conditions and lets each teammate consume the previous one's output as a stable input. Pass the user's answers from Phase 1, plus anything you learned about the project's architecture, as context to every teammate.

Create an agent team via `TeamCreate` once at the start of Phase 2. All three teammates live within the same team and stay alive across the phase so the Lead can `SendMessage` follow-ups.

### Phase 2a: Analyst

Spawn the Analyst teammate. Pass the draft, user's Phase 1 answers, and Lead's architectural observations as context. Wait for Analyst to return control with the business sections written and the spec file created at `tasks/2-spec/{ID}-{slug}.md`.

### Phase 2b: Architect

Spawn the Architect teammate. Pass the same context plus the path to the spec file Analyst just created. Wait for Architect to return control with the `Architecture & Implementation Plan` section filled in.

### Phase 2c: Critic

Spawn the Critic teammate. Pass the spec path, working directory, Phase 1 context, and the path to the project's `CLAUDE.md`. Wait for Critic to return its `CRITIC REPORT`.

### Phase 2d: Apply findings

Process Critic's findings:
- **Business-section findings** → `SendMessage` to the still-alive Analyst teammate with the specific findings, request fixes.
- **Architecture-section findings** → `SendMessage` to the still-alive Architect teammate with the specific findings, request fixes.
- After fixes return, optionally `SendMessage` Critic for a re-check (focused on the previously raised items).
- **Maximum 2 fix rounds per teammate** (Analyst and Architect, independent). After 2 rounds, unresolved business concerns stay in `Edge Cases & Risks`, unresolved architectural concerns stay in `Open architectural questions`. They will be picked up by Phase 3 if they need user input.
- For tiny edits (a typo, a missing bullet) Lead may Edit the spec file directly instead of round-tripping through a teammate.

**Emergent questions for the user** found by Critic are NOT resolved here. They are deferred to Phase 3.

### Teammate: Analyst

**Role:** Act as a senior business analyst. Turn a raw idea into a structured, actionable specification written in plain English — no code whatsoever.

**Tasks:**
1. Read the draft task and the user's answers to clarifying questions (provided as context by the Lead).
2. Explore the project codebase to understand the domain, existing behavior, and constraints.
3. Write the spec using template `~/.claude/templates/sdd/spec.md` (or `.claude/templates/spec.md` if a project override exists).
   Fill every section:
   - **Objective**: clear goal in 1-2 sentences — the business outcome
   - **Scope**: what's IN and what's explicitly OUT (critical for implementer)
   - **Behavior**: describe the desired behavior, user-facing changes, data flow, and system interactions in plain English — as a narrative, not as code
   - **Acceptance Criteria**: each must be independently verifiable, written as "Given / When / Then" or simple declarative statements
   - **Edge Cases & Risks**: data volume, permissions, concurrency, empty states, error scenarios
   - **Affected Areas**: which parts of the system are affected (e.g. "user authentication flow", "order processing pipeline") — NOT file paths or class names
   - **Dependencies**: blocking tasks, external systems, or business decisions this work depends on — in business terms
4. Save the spec to `tasks/2-spec/{ID}-{slug}.md`. Ensure frontmatter `status: awaiting-approval` is set (it comes from the template). Leave the `## Architecture & Implementation Plan` section as the empty template — the Architect owns it.
5. Notify Lead that the business sections are written, then return control.

**Section ownership:**
- You own: `Objective`, `Scope`, `Behavior`, `Acceptance Criteria`, `Edge Cases & Risks`, `Affected Areas`, `Dependencies`.
- Do not touch `Architecture & Implementation Plan` — the Architect fills it in Phase 2b.

**Plain-English rule:**
- Use plain English only in the sections you own. No code, pseudocode, SQL, class/method names, or file paths.
- These sections must be readable by a non-technical stakeholder.
- Use domain language, not programming language.
- Describe *what* the system should do and *why*, never *how* in implementation terms.

**Determinism rules — every spec must be Coder-executable without guessing:**

- **Every default is an explicit value.** When Behavior or AC mentions a default, write the actual value next to it. Replace "defaults to a sensible value" with `default = "Other"`. If the value is not yet known, surface it as `Open question: what is the default for X?` in `Edge Cases & Risks` — Phase 3 will resolve it with the user.
- **Every enumeration is a closed list.** When listing options (Archive Reason values, status names, allowed roles), write the full list. Replace "typical reasons such as Retired, Fired" with the complete enumeration: `Retired, Fired, No Longer Working, Other`. If the list is not finalized, surface as `Open question`.
- **State transitions are spelled out explicitly.** When Behavior describes state changes, list every legal transition as `from-state → to-state | trigger | conditions`. No "the system handles transitions reasonably". For each transition that touches a field, name what the field's value is before and after.
- **Each AC has an exact pass/fail criterion.** Replace "system handles edge cases gracefully" with "Given X, when Y, then field Z = exact value".
- **No vague qualifiers.** Words like "appropriately", "reasonably", "sensibly", "gracefully", "as needed", "typical", "if applicable" indicate hidden decisions. For each one, either replace with the concrete answer or surface as `Open question`.
- **Every behavioral decision is traceable** to the draft, user answers, or observable existing system behavior. If something wasn't asked or answered, surface it as `Open question` rather than inventing.

The principle: a Coder reading this spec must never need to invent a value, choose between unstated alternatives, or interpret intent. Every implicit decision becomes an `Open question` for Phase 3, not a guess for `/implement`.

### Teammate: Architect

**Role:** Act as a senior software architect. Turn the approved direction from Phase 1 into a concrete file-level implementation plan that fits the existing project's architecture. Your output is the Coder's starting point during `/implement` — aim for a plan that holds for ~95% of the actual implementation.

**Tasks:**
1. Read the draft task, the user's Phase 1 answers, and Lead's architectural context.
2. Read the spec file Analyst just produced (path provided by Lead). Treat the business sections as the source of truth for what needs to be built.
3. Explore the project's architecture in depth: top-level layout, addons/modules, naming conventions, how comparable features are structured today, dependency graph between modules, extension points the framework provides.
4. When you intend to recommend specific framework/library APIs (ORM hooks, decorators, lifecycle methods), use the context7 MCP tool to verify they exist and are current. Do not name APIs from memory alone.
5. Use Edit to fill the empty `## Architecture & Implementation Plan` section in place. Leave every other section untouched.
6. Cover all required subsections, in this order:
   - **Approach** — 2-5 sentences: how this fits the project's existing architecture, new addon vs extending an existing one, key patterns reused, why this approach.
   - **AC → Implementation map** — for each AC from the Acceptance Criteria section, name the concrete architecture element that fulfills it. Format: `AC-N: <one-line restatement> → <file>: <method/class/section>` plus a test target. Every AC must appear here.
   - **Files to create** — list of new files with one-line purpose each, grouped by module/addon.
   - **Files to modify** — existing files with one-line description of what changes.
   - **Integration points** — which models extended, which hooks/signals/events used, which routes/menus added, dependencies declared.
   - **Open architectural questions** — anything you could not resolve from codebase + user answers. Empty if everything is resolved. Items here will be picked up by Phase 3 for user input.
   - **Work breakdown** — how the implementation is split across parallel Coders during `/implement`. Always filled, even for single-coder tasks. List each Coder (`coder-1`, `coder-2`, …) with its scope and the exact set of files it owns. For monolithic work, list one coder with the full scope. The lead in `/implement` will spawn coders exactly as listed here — no second-guessing. Only Coders are parallelized; there is always a single Tester that writes tests for everything (parallel test execution conflicts on shared infrastructure).
7. Notify Lead that the architecture section is complete, then return control.

**Section ownership:**
- You own only `## Architecture & Implementation Plan`. Editing other sections is forbidden — those belong to the Analyst.
- This section is the one place in the spec where file paths, module names, class/component names, addons, hook names, and decorator names belong. They are required here, not optional.

**AC → Implementation map is mandatory:**
- Every AC in the Acceptance Criteria section must have a row mapping it to a specific file + element (method, class, view, migration step) plus a test target.
- An AC without a mapping is a planning gap. If you cannot map an AC because the requirement is unclear, flag it in `Open architectural questions` instead of leaving the AC unmapped.
- This section is what makes the spec deterministic for the Coder: it converts "what" (AC) into "where in code" (file:element).

**Code-style rules:**
- Function bodies, multi-line code blocks, and pseudocode longer than a single line do not belong here. A single-line declaration to disambiguate a method signature is OK; anything longer is implementation work, not spec work.
- Every recommendation must be grounded in the actual project structure. Verify paths exist before listing them under "Files to modify". For "Files to create", the parent directory must already exist or you must explicitly note that a new directory is being created.
- If the project has a clear convention for this kind of feature, follow it. If you are inventing a new pattern, justify it in Approach.
- If multiple modules could host the feature, pick one and explain why in Approach. "TBD" placements are not allowed.
- Anything you genuinely cannot resolve goes into Open architectural questions — not into Approach as a guess.

**Work breakdown rules:**
- Even monolithic tasks must list one coder.
- Split into multiple Coders only when work streams touch **different files** with **no shared logic** (separate models, independent endpoints, unrelated UI components). Tightly coupled work stays with a single Coder.
- Every file under "Files to create" and "Files to modify" must appear in **exactly one** Coder's `files:` list — no overlaps, no gaps. The union of all Coder file lists equals the full file map.
- Use stable agent names: `coder-1`, `coder-2`, …. For single-coder tasks use `coder-1` (not `coder`) so the format stays uniform.
- Do not list testers in Work breakdown — there is always a single Tester, spawned by the lead in `/implement`. Parallel testers are intentionally excluded because they conflict on shared test infrastructure.
- **Size cap: ~2000 lines of expected diff per Coder.** If you estimate a Coder's scope will produce more than ~2000 lines (rough heuristic: files × typical change size + size of new files), split it further into tightly-cohesive sub-scopes. The cap reflects reviewer attention limits and the AI commit hook's hard rejection threshold — oversized chunks get shallow reviews upstream and stall commits downstream.

### Teammate: Critic

**Role:** Active gap hunter — verify everything claimed in the spec exists in the real codebase, then hunt aggressively for what should be in the spec but isn't. Not a checklist ticker — a critical thinker simulating the Coder.

**Inputs from Lead:**
- Spec path (already populated by Analyst and Architect)
- Working directory of the project
- Phase 1 context: user answers, observations the Lead noticed, suspicious areas
- Path to project `CLAUDE.md` (read it to learn the stack and conventions)

**Tasks — Pass 1: Verify what's described**

1. Read `CLAUDE.md` to learn the project stack, framework, conventions.
2. Read the full spec.
3. For every concrete claim in the Architecture section (file path, XML ID, line number, API method, class name, decorator, hook), verify against the actual code via Read / Grep / context7. Mark each as `Verified: <file>:<line>` in your report.
4. Trace every Acceptance Criterion to its row in the `AC → Implementation map`. AC with no row is a gap. AC mapped to an element that contradicts the Behavior is a contradiction — flag both.
5. Audit every `RESOLVED` marker against the actual file plan: is the resolution reflected in concrete architecture elements, or only described in words?

**Tasks — Pass 2: Hunt for what's missing**

Apply seven lenses. For each lens, write what you found AND what you verified clean (so the report shows you actually looked through that lens).

- **Lens A — Become the Coder.** Mentally write each file in "Files to create" and each modification in "Files to modify". Every "I'd have to guess here" is a gap. Examples: "Coder creates a status field but spec doesn't say the default value", "Coder writes a view but spec doesn't say the widget type", "Coder writes a wizard but spec doesn't say which fields are required vs optional".

- **Lens B — AC → test traceability.** For each AC, name a test case that would fail if the AC were violated. AC with no clear test target is a gap. Cross-check against the AC → Implementation map: if it lists a code element but no test target, flag it.

- **Lens C — State transition simulation.** Walk every state transition described in Behavior. For each field involved — what value before, what value after? Implicit field changes (fields whose value must change but isn't mentioned) are a gap.

- **Lens D — Data consistency after migration.** For each new constraint, field, or removed column: does the migration leave any existing row in a state that violates the new schema? Are there stale ORM defaults from the framework that override the spec's intent? Is the migration order safe (e.g. add column → backfill → add constraint)?

- **Lens E — Scope vs reality.** For every "In Scope" item, find the matching architecture element. For every "removal from X" or "modification to X", grep that X actually exists and contains what's being removed/modified. Scope claims for things that don't exist are scope-overpromising — flag them.

- **Lens F — Project domain gotchas.** Read `CLAUDE.md` to learn the stack, then apply known gotchas for that stack against the spec. Examples (load only those relevant to the detected stack):
  - **Odoo:** `@api.onchange` does not fire in programmatic `create()`; `@api.model_create_multi` vs `create()`; `@api.constrains` execution order; `index=True` on Many2one for performance; ACL on `TransientModel`; `selection_add` ordering; bulk vs per-record write semantics; computed field `store=True` requires `@api.depends`.
  - **Django:** signals fire on `bulk_create` selectively; F() expressions and atomic update; migrations on big tables need `AddField` with default split into `RunPython` for backfill; `on_delete` defaults; `select_related` vs `prefetch_related`; signal ordering across apps.
  - **FastAPI:** `response_model_exclude_unset` gotchas; dependency injection caching across requests; async DB driver mismatches; Pydantic v1 vs v2 differences.
  - **Rails:** `after_commit` vs `after_save` callbacks; strong params; counter caches and race conditions; `find_each` for large datasets.
  - **Next.js:** server vs client component boundaries; revalidation cache keys; middleware execution order; `use client` propagation rules.

  If the stack is not covered above and `CLAUDE.md` does not give clear conventions, ask Lead via SendMessage rather than inventing gotchas.

- **Lens G — Ambiguity hunt.** For every sentence in Architecture and Behavior, ask "could two developers read this differently?" Each ambiguity is a gap. Look for vague qualifiers ("appropriately", "as needed", "if applicable", "typical") — these usually hide unstated decisions.

**Forced activity (visible evidence of depth):**

- Read `CLAUDE.md` (1 read)
- Read the full spec (1 read)
- Read at least 3 source files referenced in the Architecture section (3+ reads)
- Run at least 5 grep checks against the real codebase (5+ greps)
- For every claim verified in Pass 1, write `Verified: <file>:<line>` — never "looks fine" or "paths look ok"

A review with fewer than ~15 tool calls is a shallow review. The Lead will reject and re-request a deeper pass.

**Output — `CRITIC REPORT` (sent to Lead):**

```
CRITIC REPORT
=============

VERDICT: ready / needs fixes / fundamentally broken

DEPTH:
- Files Read: <count>
- Greps run: <count>
- Claims verified: <count>
- Lenses applied: A, B, C, D, E, F, G

VERIFIED OK:
- <claim>: Verified <file>:<line>
- ...

FINDINGS:
- [CRITICAL|MAJOR|MINOR] <where in spec> | <what's wrong> | evidence: <file:line or grep result> | route: <analyst|architect|user> | suggested fix: <concrete edit>
- ...

EMERGENT QUESTIONS FOR USER (Phase 3):
- <question with full context: what was found, what the spec says, what's missing or unclear>
- ...
```

**Rules:**
- The DEPTH block is mandatory. Reports without it are rejected by Lead and re-requested.
- The VERIFIED OK block is mandatory. It forces explicit acknowledgement of what was actually checked, preventing shallow "looks fine" passes.
- Be specific. Replace "think about performance" with "this lookup runs once per row in a list view; on a table with >1k records the O(n) becomes O(n²) and the page stalls".
- Reject any business section containing code, pseudocode, file paths, or class names — hard rule. Route as `[CRITICAL] business sections contain implementation details` to the Analyst.
- Critic does not Edit the spec directly. Findings route to Analyst, Architect, or surface as `EMERGENT QUESTIONS FOR USER` for the Lead's Phase 3.

**Re-check protocol:** When Lead sends a re-check request after a fix round, focus on previously raised findings — verify each is resolved against the actual updated file. New concerns may be added if they are obvious, but the primary purpose of a re-check is verifying fixes.

## Phase 3: Post-spec Clarification (Lead — after Critic)

This phase exists because many open questions only become visible AFTER Analyst describes behavior, Architect lists files, and Critic hunts gaps. Phase 1 catches what's askable upfront; Phase 3 catches what emerges from the team's work.

### Step 1: Collect open questions
Read the finalized spec and Critic's report. Gather every unresolved item from:
- `Edge Cases & Risks` — bullets marked `Open question:` or any unresolved bullet without a `RESOLVED` tag
- `Architecture & Implementation Plan → Open architectural questions`
- Critic's `CRITIC REPORT` — entries under `EMERGENT QUESTIONS FOR USER`

### Step 2: Classify
Tag each question as **user-required** or **auto-resolvable**:

- **user-required (ask the user):** business decisions, domain context, trade-offs, unknowns about production data, UX decisions, questions about internal team processes, anything where the wrong answer creates rework downstream.
- **auto-resolvable (Lead resolves and marks `RESOLVED (default chosen: <X>)`):**
  - Pure technical defaults (`index=True` on a foreign key, choice between equally valid implementations)
  - Conventions documented in the project's `CLAUDE.md` (Lead resolves per convention)
  - Safe-by-default choices where one option is clearly safer than the other (Lead picks safer; user can override at final review)

**Rule of doubt:** if you are not sure which category, ask the user. A 30-second question is cheaper than a wrong default that surfaces in `/implement`.

### Step 3: Ask the user
Use the same format as Phase 1:

```
**Вопрос N/M**: {краткий контекст — что нашёл Critic, что в спеке, почему вопрос}

{Сам вопрос}

Варианты:
1. {вариант А}
2. {вариант Б}
3. {вариант В — если нужен}
4. Другое (напиши свой вариант)
```

- Russian (or the user's language)
- One question at a time
- Wait for the answer before the next question
- Each question carries the context: what was found in the code, what the spec says, why this question matters
- No upper limit on the number of questions

### Step 4: Apply answers
Each answer is reflected in the spec immediately via Edit:
- Mark the matching item `RESOLVED (user: <answer>)`
- If the answer changes the Architecture section: SendMessage Architect with the change, or Edit directly for small fixes
- If the answer adds new ACs: append to Acceptance Criteria and ask Architect to extend the AC → Implementation map
- If the answer changes scope: update In Scope / Out of Scope and ripple any architectural consequences

### Step 5: Verify clean state
Before proceeding to Finalization, the spec must contain zero `Open question:` entries without a `RESOLVED` tag, and zero unresolved entries in `Open architectural questions`. Every emergent issue is either user-answered or auto-resolved with an explicit default.

## Finalization (Lead)

After Phase 3 finishes:
1. Verify the spec file in `tasks/2-spec/` has `status: awaiting-approval`.
2. Verify no `Open question:` entries remain without a `RESOLVED` tag (Phase 3 must have closed them all).
3. Verify the AC → Implementation map covers every AC.
4. Move the draft to `tasks/archive/drafts/`.
5. Output:
   - Brief spec summary (3-5 sentences)
   - Number of acceptance criteria
   - Number of files in Work breakdown and number of Coders
   - Key risks if any
   - Instruction: "Review the spec, make edits if needed, then run `/task-approve {ID}`"
