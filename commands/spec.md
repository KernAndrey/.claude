Generate a specification from a draft task using an agent team.

## Instructions

1. Read `.tasks.toml`. If missing → tell user to run `/task-init` and stop.
2. Locate the draft by `$ARGUMENTS` — match by ID, slug, or full path in `tasks/1-draft/`.
3. Read the draft file content.
4. Read `CLAUDE.md` for project context.

## Phase 1: Discovery & Clarification (Lead — before spawning the team)

This phase is **mandatory** and cannot be skipped.

1. Read the draft task carefully.
2. Explore the project codebase: domain structure, existing behavior related to the draft, constraints, conventions.
3. Compile a list of clarifying questions for the user. Topics to cover:
   - **Цель**: Какая бизнес-задача решается? Кому и как это поможет?
   - **Границы**: Что явно НЕ входит в задачу? Есть ли смежные фичи, которые трогать не нужно?
   - **Поведение**: Любые неоднозначные сценарии "как оно должно работать" — спроси, не додумывай.
   - **Крайние случаи**: Что происходит при пустых данных, ошибках, нехватке прав, больших объёмах?
   - **Приоритет и ограничения**: Есть ли дедлайны, требования к производительности, зависимости от другой работы?
   - **Существующее поведение**: Если драфт меняет существующую функциональность — уточни, что сейчас и что именно должно измениться.
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
- ALL questions and options must be in **Russian**.
- NEVER assume an answer. If something is unclear — ask.
- NEVER skip this phase. Even a detailed draft has gaps that only the user can fill.
- Frame questions in business/domain terms, not technical terms.
- Include what you learned from exploring the codebase as context (e.g. "Я вижу, что сейчас система делает X — Y должен заменить это или работать параллельно?").
- ONE question at a time. Do NOT dump all questions at once.

## Phase 2: Specification (Agent Team)

Only start this phase after the user has answered your questions. Pass the user's answers as context to both teammates.

Create an agent team with two teammates:

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
4. Save draft spec to `tasks/2-spec/{ID}-{slug}.md`. Ensure frontmatter `status: awaiting-approval` is set (it comes from the template).
5. Message Critic that the draft spec is ready for review.

**Rules:**
- **NEVER include code** — no snippets, no pseudocode, no SQL, no class/method names, no file paths. The spec must be readable by a non-technical stakeholder.
- **NEVER invent requirements or assume answers** — every behavioral decision in the spec must be traceable to either the draft, the user's answers, or observable existing system behavior. If something wasn't asked or answered — put it in Edge Cases & Risks as an open question, do NOT fill in a guess.
- Acceptance criteria must be concrete and verifiable, not subjective.
- Describe *what* the system should do and *why*, never *how* in implementation terms.
- Use domain language, not programming language.

### Teammate: Critic

**Role:** Find weaknesses, gaps, and risks in the specification.

**Tasks:**
1. Wait for Analyst's draft spec.
2. Review against this checklist:
   - [ ] Is scope clearly defined? No gray areas?
   - [ ] Is the described behavior complete and unambiguous?
   - [ ] Are acceptance criteria verifiable? No subjective criteria?
   - [ ] Edge cases covered? (empty data, large volumes, permissions, concurrency)
   - [ ] Security implications? (if applicable)
   - [ ] Performance implications? (at scale)
   - [ ] Any overlap with other tasks in `tasks/`?
   - [ ] **Is the spec free of code, pseudocode, file paths, and class names?** If not — send back for rewrite.
   - [ ] **Are all behavioral decisions traceable to the draft or user's answers?** Flag anything that looks assumed or invented.
3. Send specific findings directly to Analyst (via team messaging).
4. Analyst incorporates fixes → Critic does a final pass.
5. If satisfactory — confirm to the lead.

**Rules:**
- Be specific: not "think about performance" but "this operation may degrade at >1000 records due to repeated lookups".
- Reject any spec that contains code, pseudocode, or implementation-level references — this is a hard requirement.
- If a critical issue is found — do not let it pass, even if Analyst disagrees.
- Maximum 2 review rounds. Then finalize, noting unresolved concerns in Edge Cases & Risks.

## Finalization (Lead)

After the team finishes:
1. Verify the spec file in `tasks/2-spec/` has `status: awaiting-approval`.
2. Move the draft to `tasks/archive/drafts/`.
3. Output:
   - Brief spec summary (3-5 sentences)
   - Number of acceptance criteria
   - Key risks if any
   - Instruction: "Review the spec, make edits if needed, then run `/task-approve {ID}`"
