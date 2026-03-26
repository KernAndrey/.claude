Decompose a big idea into multiple draft tasks.

## Instructions

1. Read `.tasks.toml` in project root. Get `id_prefix`, `dir`, and `counter_file`. If `.tasks.toml` is missing, tell the user to run `/task-init` first and stop.
2. Parse `$ARGUMENTS`:
   - If it looks like a file path (ends with `.md`, or starts with `/`, `./`, or `..`), read the file and use its content as the big idea description.
   - Otherwise, treat the entire `$ARGUMENTS` as a free-text description.
3. Read `CLAUDE.md` for project context.

## Phase 1: Context Loading

1. Explore the project codebase: domain structure, modules, existing conventions.
2. Scan all existing tasks in `tasks/` (all statuses except `archive`) — read their frontmatter (id, title, status, group) to understand what already exists. This avoids creating duplicate or overlapping tasks.
3. If tasks with a `group` field exist, note the active groups.

## Phase 2: Clarification (Light Q&A)

This phase is **mandatory** and cannot be skipped.

1. Based on the big idea and codebase exploration, compile 3-5 clarifying questions. Focus areas:
   - **Границы**: Что явно НЕ входит в эту идею? Есть ли смежные вещи, которые трогать не нужно?
   - **Гранулярность**: Насколько мелко бить? Одна фича = одна задача, или крупнее?
   - **Порядок**: Есть ли жёсткие зависимости между частями, или всё можно делать параллельно?
   - **Приоритет**: Что самое важное? Что можно отложить?
   - **Пересечения**: Если есть существующие задачи в том же домене — спросить, как новая работа соотносится с ними.

2. **Ask questions ONE AT A TIME.** Follow the same format as `/spec`:

   ```
   **Вопрос N/M**: {краткий контекст — что ты нашёл в кодовой базе, если релевантно}

   {Сам вопрос}

   Варианты:
   1. {вариант А}
   2. {вариант Б}
   3. {вариант В — если нужен}
   4. Другое (напиши свой вариант)
   ```

3. Wait for the user's answer before asking the next question.
4. If an answer reveals new ambiguities — add follow-up questions.

**Rules for this phase:**
- ALL questions and options must be in **Russian**.
- NEVER assume an answer. If something is unclear — ask.
- NEVER skip this phase.
- Focus on decomposition concerns (what are the pieces, how they relate), NOT implementation details (each task will get its own `/spec` Q&A later).
- ONE question at a time.

## Phase 3: Decomposition Proposal

After the user has answered all questions:

1. Propose a task breakdown. For each task:
   - **Title** (short, actionable, domain language)
   - **Summary** (2-3 sentences — what this task covers)
   - **Priority** (critical / high / medium / low)
   - **Dependencies** (which other proposed tasks must come first, by number)
   - **Complexity** (small / medium / large — to help gauge granularity)

2. Present the proposal as a numbered list:

```
## Предложение по декомпозиции: {название группы}

Задач: N | Зависимости: {краткое описание цепочки}

1. **{Title}** [priority] [complexity]
   {Summary}
   Зависит от: — (или: #2, #3)

2. **{Title}** [priority] [complexity]
   {Summary}
   Зависит от: #1

...
```

3. Ask: **"Подтверди декомпозицию, или скажи что изменить (добавить / убрать / разбить / объединить / переприоритизировать)."**

4. If the user requests changes — adjust the proposal and present again. Iterate until confirmed.

**Rules:**
- Propose 2-7 tasks. If the idea naturally requires more — ask the user if they want to split into two groups.
- Each task must be independently spec-able via `/spec`.
- Do not include implementation details — keep it at the business/domain level.
- Propose names in the domain language of the project.

## Phase 4: Batch Draft Creation

After user confirms the proposal:

1. Read the counter from `counter_file`. Calculate the range: `current + 1` through `current + N`.
2. Generate a **group slug** from the big idea (kebab-case, max 4 words, ASCII only). Example: `invoice-pipeline`, `test-isolation`.
3. For each confirmed task (in dependency order):
   a. Increment counter, generate ID: `{id_prefix}-{counter:03d}`.
   b. Generate task slug from the title (kebab-case, max 5 words, ASCII only).
   c. Copy template from `~/.claude/templates/sdd/draft.md`. If `.claude/templates/draft.md` exists in the project, use that instead (project override).
   d. Fill placeholders: `{{ID}}`, `{{TITLE}}`, `{{DATE}}`.
   e. Add extra frontmatter fields after `priority`:
      - `group: "{group-slug}"`
      - `depends_on: ["{ID-1}", "{ID-2}"]` (array of task IDs, empty `[]` if none)
   f. Fill `## Idea` with the task summary from the confirmed proposal.
   g. Fill `## Context` with:
      ```
      Decomposed from: "{big idea title}"
      Group: {group-slug}
      Sibling tasks: {ID-1} ({title-1}), {ID-2} ({title-2}), ...
      ```
   h. Save to `{dir}/1-draft/{ID}-{slug}.md`.

4. Write the counter back (to `current + N`).

## Output

Display a summary table and next steps:

```
## Создано {N} задач (группа: {group-slug})

| #  | ID       | Название                      | Приоритет | Зависит от |
|----|----------|-------------------------------|-----------|------------|
| 1  | TMS-008  | {title}                       | high      | —          |
| 2  | TMS-009  | {title}                       | high      | TMS-008    |
| 3  | TMS-010  | {title}                       | medium    | TMS-008    |

Следующий шаг: `/spec {first-ID}`
Рекомендуемый порядок: TMS-008 → TMS-009 → TMS-010
```

## Description

$ARGUMENTS
