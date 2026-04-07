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

## Phase 2: Specification (Agent Team)

Only start this phase after the user has answered your questions. Pass the user's answers, plus anything you learned about the project's architecture during Phase 1, as context to all teammates.

Create an agent team with three teammates: **Analyst**, **Architect**, **Critic**. Spawn all three at once (single message, three `Agent` tool calls). Analyst and Architect work in parallel. Critic stays idle until it receives both `ANALYST DRAFT READY` and `ARCHITECT DRAFT READY`, then begins the review.

### Teammate: Analyst

**Role:** Act as a senior business analyst. Turn a raw idea into a structured, actionable specification written in plain English — no code whatsoever.

**Tasks:**
1. Read the draft task and the user's answers to clarifying questions (provided as context by the Lead).
2. Explore the project codebase to understand the domain, existing behavior, and constraints.
3. Write the spec using template `~/.claude/templates/sdd/spec.md` (or `.claude/templates/spec.md` if project override exists).
   Fill every section:
   - **Objective**: clear goal in 1-2 sentences — the business outcome
   - **Scope**: what's IN and what's explicitly OUT (critical for implementer)
   - **Behavior**: describe the desired behavior, user-facing changes, data flow, and system interactions in plain English — as a narrative, not as code
   - **Acceptance Criteria**: each must be independently verifiable, written as "Given / When / Then" or simple declarative statements
   - **Edge Cases & Risks**: data volume, permissions, concurrency, empty states, error scenarios
   - **Affected Areas**: which parts of the system are affected (e.g. "user authentication flow", "order processing pipeline") — NOT file paths or class names
   - **Dependencies**: blocking tasks, external systems, or business decisions this work depends on — in business terms
4. Save draft spec to `tasks/2-spec/{ID}-{slug}.md`. Ensure frontmatter `status: awaiting-approval` is set (it comes from the template). Leave the `## Architecture & Implementation Plan` section as the empty template — the Architect owns it.
5. Send `ANALYST DRAFT READY` to **both Architect and Critic** — Architect needs it as the green light to start editing the file, Critic needs it as one of the two signals before reviewing. Then notify Lead that the business sections are written.

**Rules:**
- **Write in plain English only** in the sections you own — no code, pseudocode, SQL, class/method names, or file paths. These sections must be readable by a non-technical stakeholder.
- The sections you own are: `Objective`, `Scope`, `Behavior`, `Acceptance Criteria`, `Edge Cases & Risks`, `Affected Areas`, `Dependencies`. **Do not touch** the `Architecture & Implementation Plan` section — it belongs to the Architect.
- **Every behavioral decision must be traceable** to the draft, user answers, or observable existing system behavior. If something wasn't asked or answered, put it in Edge Cases & Risks as an open question.
- Acceptance criteria must be concrete and verifiable, not subjective.
- Describe *what* the system should do and *why*, never *how* in implementation terms.
- Use domain language, not programming language.

### Teammate: Architect

**Role:** Act as a senior software architect. Turn the approved direction from Phase 1 into a concrete file-level implementation plan that fits the existing project's architecture. Your output is the Coder's starting point during `/implement` — aim for a plan that holds for ~95% of the actual implementation.

**Tasks:**
1. Read the draft task and the user's answers to clarifying questions (provided as context by the Lead), plus any architectural context the Lead captured during Phase 1.
2. Explore the project's architecture in depth: top-level layout, addons/modules, naming conventions, how comparable features are structured today, dependency graph between modules, extension points the framework provides.
3. When you intend to recommend specific framework/library APIs (ORM hooks, decorators, lifecycle methods), use the context7 MCP tool to verify they exist and are current. Do not name APIs from memory alone.
4. Complete all exploration and mentally outline the section content. Then wait for `ANALYST DRAFT READY` from the Analyst before touching the spec file.
5. Once the signal arrives, open `tasks/2-spec/{ID}-{slug}.md` and use Edit to fill the empty `## Architecture & Implementation Plan` section in place. Leave every other section untouched.
6. Cover all required subsections:
   - **Approach** — 2-5 sentences: how this fits the project's existing architecture, new addon vs extending an existing one, key patterns reused, why this approach.
   - **Files to create** — list of new files with one-line purpose each, grouped by module/addon.
   - **Files to modify** — existing files with one-line description of what changes.
   - **Integration points** — which models extended, which hooks/signals/events used, which routes/menus added, dependencies declared.
   - **Open architectural questions** — anything you could not resolve from codebase + user answers. Empty if everything is resolved.
   - **Work breakdown** — how the implementation is split across parallel Coders during `/implement`. Always filled, even for single-coder tasks. List each Coder (`coder-1`, `coder-2`, …) with its scope and the exact set of files it owns. For monolithic work, list one coder with the full scope. The lead in `/implement` will spawn coders exactly as listed here — no second-guessing. Only Coders are parallelized; there is always a single Tester that writes tests for everything (parallel test execution conflicts on shared infrastructure).
7. Send `ARCHITECT DRAFT READY` to **Critic**.

**Rules:**
- You own only the `## Architecture & Implementation Plan` section. Editing other sections is forbidden.
- This section is the one place in the spec where file paths, module names, class/component names, addons, hook names, and decorator names belong — and they are required, not optional.
- Function bodies, multi-line code blocks, and pseudocode longer than a single line do not belong here. A single-line declaration to disambiguate a method signature is OK; anything longer is implementation work, not spec work.
- Every recommendation must be grounded in the actual project structure. Verify paths exist before listing them under "Files to modify". For "Files to create", the parent directory must already exist or you must explicitly note that a new directory is being created.
- If the project has a clear convention for this kind of feature, follow it. If you are inventing a new pattern, justify it in Approach.
- If multiple modules could host the feature, pick one and explain why in Approach. "TBD" placements are not allowed.
- Anything you genuinely cannot resolve goes into Open architectural questions — not into Approach as a guess.
- **Work breakdown is mandatory.** Even monolithic tasks must list one coder. Splitting rules:
  - Split into multiple Coders only when work streams touch **different files** with **no shared logic** (separate models, independent endpoints, unrelated UI components). Tightly coupled work stays with a single Coder.
  - Every file under "Files to create" and "Files to modify" must appear in **exactly one** Coder's `files:` list — no overlaps, no gaps. The union of all Coder file lists equals the full file map.
  - Use stable agent names: `coder-1`, `coder-2`, …. For single-coder tasks use `coder-1` (not `coder`) so the format stays uniform.
  - Do not list testers in Work breakdown — there is always a single Tester, spawned by the lead in `/implement`. Parallel testers are intentionally excluded because they conflict on shared test infrastructure.
  - **Size cap: ~2000 lines of expected diff per Coder.** If you estimate a Coder's scope will produce more than ~2000 lines (rough heuristic: files × typical change size + size of new files), split it further into tightly-cohesive sub-scopes. The cap reflects reviewer attention limits and the AI commit hook's hard rejection threshold — oversized chunks get shallow reviews upstream and stall commits downstream.
- If `ANALYST DRAFT READY` does not arrive after you finish exploration, ping the Lead. Do not start editing the file before the signal arrives.

### Teammate: Critic

**Role:** Find weaknesses, gaps, and risks in the specification — both the business sections (Analyst's work) and the architecture section (Architect's work).

**Tasks:**
1. Stay idle until you have received **both** `ANALYST DRAFT READY` and `ARCHITECT DRAFT READY`. A partial review on one section alone is forbidden — the architecture and business sections must be reviewed against each other.
2. Review the **business sections** (Objective, Scope, Behavior, Acceptance Criteria, Edge Cases & Risks, Affected Areas, Dependencies) against this checklist:
   - [ ] Is scope clearly defined? No gray areas?
   - [ ] Is the described behavior complete and unambiguous?
   - [ ] Are acceptance criteria verifiable? No subjective criteria?
   - [ ] Edge cases covered? (empty data, large volumes, permissions, concurrency)
   - [ ] Security implications? (if applicable)
   - [ ] Performance implications? (at scale)
   - [ ] Any overlap with other tasks in `tasks/`?
   - [ ] **Are these sections free of code, pseudocode, file paths, and class names?** If not — send back to Analyst for rewrite.
   - [ ] **Are all behavioral decisions traceable to the draft or user's answers?** Flag anything that looks assumed or invented.
3. Review the **`Architecture & Implementation Plan`** section against this separate sub-checklist:
   - [ ] All required subsections present and filled (Approach, Files to create, Files to modify, Integration points, Open architectural questions, Work breakdown)?
   - [ ] **Files to modify** — spot-check 2–3 paths against the actual project: do these files exist?
   - [ ] **Files to create** — do the proposed paths fit the project's layout convention? Are the parent directories real (or new-directory creation explicitly noted)?
   - [ ] **No function bodies or multi-line code blocks?** Single-line declarations are fine; longer code is not.
   - [ ] Approach is justified, not arbitrary — does it explain *why* this placement over alternatives?
   - [ ] No "TBD" placements? If multiple modules could host the feature, one is picked with reasoning.
   - [ ] Open architectural questions clearly listed (or absent because everything is resolved).
   - [ ] Architecture aligns with the business behavior described by the Analyst — no contradictions between sections.
   - [ ] **Work breakdown — Coder coverage:** take the union of all `files:` lists across coders. Does it equal the full set from "Files to create" + "Files to modify"? Flag any file that appears in zero coders (gap) or in two+ coders (overlap).
   - [ ] **Work breakdown — split is justified:** if multiple Coders, do they actually touch disjoint files with no shared logic? Flag splits that look artificial (e.g. coders editing the same module). If a single Coder, is the work genuinely tightly coupled, or was a split missed?
   - [ ] **Work breakdown — size cap:** any Coder estimated to produce >2000 lines of diff? If yes, flag for further split — reviewers cannot audit oversized chunks deeply and the AI commit hook will reject them.
   - [ ] **Work breakdown — naming:** coders use `coder-1`, `coder-2`, …? Even single-coder tasks use the numbered form?
   - [ ] **Work breakdown — no testers listed:** the section must contain only Coders. If the Architect listed testers, flag and route to Architect for removal — there is always exactly one Tester, spawned by the lead. (Critic does not edit the Architect's section directly.)
4. Route findings: business-section findings go to **Analyst**, architecture-section findings go to **Architect**. Do not cross-route.
5. Each owner incorporates their fixes → Critic does a final pass on both sections.
6. If satisfactory — confirm to the lead.

**Rules:**
- Be specific: not "think about performance" but "this operation may degrade at >1000 records due to repeated lookups".
- **Reject any business section that contains code, pseudocode, file paths, or class names** — this is a hard requirement.
- The `Architecture & Implementation Plan` section is the only place where paths, classes, and module names are allowed (and required). Do not flag those there — flag only multi-line code blocks or function bodies.
- If a critical issue is found — do not let it pass, even if Analyst or Architect disagrees.
- Maximum 2 fix rounds **per teammate** (so up to 2 for Analyst and 2 for Architect, run independently). After that, finalize: note unresolved business concerns in Edge Cases & Risks and unresolved architectural concerns in Open architectural questions.

## Finalization (Lead)

After the team finishes:
1. Verify the spec file in `tasks/2-spec/` has `status: awaiting-approval`.
2. Move the draft to `tasks/archive/drafts/`.
3. Output:
   - Brief spec summary (3-5 sentences)
   - Number of acceptance criteria
   - Key risks if any
   - Instruction: "Review the spec, make edits if needed, then run `/task-approve {ID}`"
