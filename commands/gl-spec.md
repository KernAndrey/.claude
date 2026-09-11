Generate a specification that follows a business analyst's specification — the `/spec` sibling for tasks that arrive with a BA document. Research, architecture, review, blockers and commits run as in `/spec`; the business half is rebuilt around the BA document. This command is orchestration-only — the Analyst, Architect, and Critic bodies live in `~/.claude/agents/spec-{analyst,architect,critic}.md`.

<!-- Sibling of ~/.claude/commands/spec.md. Every section this file does not change is a copy of spec.md — carry spec.md edits over. -->

## Research has no budget

A mistake in the spec is the most expensive mistake in the pipeline. A wrong premise here becomes a numbered Decision, then an architecture, then code and tests built on it — and surfaces only during `/implement` or in production, when every layer above it has to be redone. Most spec mistakes trace back to research that stopped one pass too early.

- Research as much as it takes: tool calls, files read, researchers spawned, follow-up passes — none of these is capped. The watchdogs are liveness timers, not deadlines; a healthy researcher that is still reading is doing its job.
- At every transition — before merging observations, before each question, before spawning the Analyst, before each Phase 3 question — ask yourself two things: **"Do I have enough context to be right about this?"** and **"Would one more pass change the answer?"** A "no" or "not sure" on either means research more, not proceed.
- A minute of extra research is cheaper than any question it makes unnecessary, and far cheaper than any Decision it prevents from being wrong.

<bad_pattern>
❌ BAD THOUGHT: "Three reports are in and the picture is mostly clear — time to move to questions."
✅ REALITY: "Mostly clear" is where the wrong premise lives. The unclear part is exactly what the user will be asked about, and they will answer it as posed.
⚠️ DETECTION: Moving to the next phase while you can name a part of the draft you have not seen the code for? → spawn a follow-up researcher or read it yourself first.
</bad_pattern>

## The BA document is the source of truth

On the task that motivated this command, the code matched the spec exactly; it was the spec that had drifted from the BA document. 79 lines of ticket became 1754 lines of business analysis carrying 36 recorded deviations, each numbered and justified — agreed 36 times, by us, with ourselves. Ten reviewers and 444 tests passed. The user then found five defects in an hour of using the result, all five in the business layer and none in the architecture.

- **The BA document is copied once and retold nowhere.** It lives verbatim in `## Original BA Specification`, which the BA owns and nobody edits. Every other section points at it by anchor — `FR 3`, `Flow 4`, `AC 7` — instead of paraphrasing it. A paraphrase is where divergence gets in unnoticed; a pointer has nothing to diverge from.
- **The BA document is read-only.** Auditing it produces questions for the BA, never an edited text.
- **Expansion is expected; substitution is not.** One BA acceptance criterion that becomes thirty of ours is correct — tests need statements of behavior, and the ticket states intent. One that becomes a different rule is a deviation.
- **A deviation that replaces the BA's judgment waits for the BA** (§ *Deviation ledger*). A written justification records a departure; it does not approve one.

<bad_pattern>
❌ BAD THOUGHT: "The deviation is numbered, justified and cites a decision — it's handled."
✅ REALITY: That is bookkeeping, and bookkeeping is exactly what let the ZIP-splitting deviation through: the ticket's second sentence required the system to mirror the shape of the document it replaces, and a flawlessly documented deviation normalized that shape away while a fidelity lens confirmed it was documented.
⚠️ DETECTION: About to write a `judgment` ledger line with no `→ b-N` blocker? → create the blocker; the BA decides.
</bad_pattern>

## 0. Setup

1. Read `~/.claude/templates/sdd/board-root.md` and follow it to resolve `{main_root}`, discover the SDD configs, and define `{board}`. The board lives in the main worktree — the draft and the spec are read and written there even when you are standing in a linked worktree. Select the root whose `id_prefix` matches the task ID. A BA-backed ID comes from the tracker and may carry a prefix no config declares: then select the root whose board already holds `{ID}` — a draft, a spec, or `context/{ID}/`. One config → use it; still ambiguous → ask.

   `{board}` covers the board file only. `{project root}` and `Working directory` in every agent prompt below stay the **current** checkout: agents research the code where you are standing, and only the spec file lives elsewhere.
2. **Pick the reviewer backend.** Read `~/.claude/templates/codex-reviewer.md` and follow its §1 to resolve `REVIEW_BACKEND` and `REVIEW_MODEL` — from a `--reviewers=` / `--reviewer-model=` pre-answer in `$ARGUMENTS`, otherwise from one `AskUserQuestion`. Resolve it here, before anything else consumes `$ARGUMENTS`: §1 strips those flags; strip `--ba=<path>` the same way (step 4 reads it), and what remains is the task identifier. On `codex`, run §2 (companion path, model check), §3 (sandbox probe) and §4's `CODEX_DIR` now too — a failed probe flips the run back to `claude` while no agent has been spawned yet, instead of after the critic batch is already in flight.
3. Locate the target by the remaining `$ARGUMENTS` (ID, slug, or full path):
   - Match in `{board}/1-draft/` → `RUN_MODE = new`.
   - Match in `{board}/2-spec/` → `RUN_MODE = resume` (the spec already exists; you are re-entering it to resolve open blockers or apply late findings).
   - Found in neither → `RUN_MODE = new` with no draft yet; step 5 creates it. That needs the ticket ID — given only a slug or a path that matches nothing, report it and stop.
4. **Locate the BA document** — set `BA_SPEC_PATH` from the first source that exists:
   - `--ba=<path>` from `$ARGUMENTS`;
   - `{board}/context/{ID}/` — its one top-level `.md` file (several → ask the user which one is the BA document);
   - the draft's or spec's `## Original BA Specification`, when it holds more than its placeholder comment — `/task` may already have copied the document there, with its source named on the first line.

   When a file and a filled section both exist and differ, ask the user which one is current.

   Set `MEET_PATH` to `{board}/context/{ID}/meet/transcript.md` when that file exists. A transcript is a secondary source, cited as `meeting MM:SS`; where it disagrees with the BA document, that is an audit finding, not a correction.

   Nothing found → one `AskUserQuestion` for the path. The task has no BA document → print "`/gl-spec` follows a BA document, and {ID} has none — use `/spec {ID}`." and stop.
5. **No draft and no spec → create the draft.** This takes the place of a `/task` run:
   - `{ID}` is the tracker number from `$ARGUMENTS`. A BA-backed task keeps its ticket's number: leave `{counter_file}` untouched, and use `board-root.md` §5 only to confirm that no `{board}/**/{ID}-*.md` exists.
   - Title: the BA document's first heading, without a leading `[{ID}]`. Slug: kebab-case from the title, max 5 words, ASCII.
   - Copy `~/.claude/templates/sdd/draft.md` (a project `.claude/templates/draft.md` wins) and fill `{{ID}}`, `{{TITLE}}`, `{{DATE}}`. Set `{{DESCRIPTION}}` to one line — `Implement {ID} as the BA document in ## Original BA Specification states it.` — so the Idea points at the document instead of summarising it.
   - Fill `## Original BA Specification` the way `/task` step 8 does: the document verbatim, its source path on the first line.
   - Save to `{board}/1-draft/{ID}-{slug}.md`. Commit 1 of `## Commits` records it; it gets no commit of its own.

   A draft that already exists with an empty `## Original BA Specification` gets that section filled the same way before Phase 1.
6. Read the draft (new) or the existing spec (resume), and the BA document in full. On a resume run the draft is already archived at `{board}/archive/drafts/{ID}-{slug}.md`: every mention of "the draft" and `{draft path}` below means that file, which still carries `## Decisions`, `## Codebase Observations` and `## BA Analysis`. When `MEET_PATH` is set, read the transcript too — decisions made in the meeting are cited by timestamp.
7. Read the project `CLAUDE.md` for stack and conventions.

## 1. Phase 1 — Discovery (new runs only)

Skip this section on resume runs; jump to Phase 1.5.

This phase is **mandatory** for new runs and cannot be skipped.

Phase 1 starts with three passes over the BA document (§1.0–§1.2) and records them in the draft under a new `## BA Analysis` section, placed after `## Original BA Specification` — the draft is the channel every agent reads, so what is not written there does not reach them.

### 1.0. Anchor index

The spec points at the BA document instead of retelling it, so every pointer has to resolve to exactly one place. Build the index before anything else reads the document.

1. **Find sections by their heading text, not their markdown level.** Tracker exports mark sections as bold lines (`**Functional Requirements**`) rather than `##` headings — `grep '^## '` over the CFT-322 ticket finds nothing. Look for `Business Need`, `User Story`, `Scope`, `Flow`, `Functional Requirements`, `Acceptance Criteria`, and whatever other headings the document uses.
2. **Number items yourself, flat, in document order — ignore the list markers.** In CFT-322 an image between two items ended the Functional Requirements list, so the source numbers its groups `1, 2, 1, 2, 3, 4, 5`: the group a reader calls `FR 3` carries `1.` in the file. Its 14 acceptance criteria carry no numbers at all. Pointers built from the literal markers would have been wrong on the only ticket available.
3. **Anchor classes:** `FR n` (one per requirement group), `Flow n` (one per step), `AC n` (one per acceptance criterion), and one named anchor per prose section (`Business Need`, `User Story`, `Scope`). A transcript decision is cited as `meeting MM:SS` and needs no index row.
4. **Record the index** under `## BA Analysis → ### Anchor index`:

   | anchor | heading in the document | source line | first words, verbatim |
   |---|---|---|---|
   | FR 3 | Agreement Lines | 33 | "1. **Agreement Lines**" |

   The first-words column is what keeps a row resolvable inside `## Original BA Specification` as well, where the line numbers differ.

### 1.1. Audit the BA document — questions, not edits

Read the document the way a reviewer reads a contract before signing it: for what it contradicts, what it leaves unsaid, and what cannot be built as written. Each finding becomes a question; the document stays exactly as the BA wrote it.

Record each finding under `## BA Analysis → ### Audit`, one line each: `{kind} — {anchor}: "{quoted words}" — {what is wrong}`. Four kinds, defined by the CFT-322 cases:

| kind | CFT-322 case |
|---|---|
| `contradiction` — the document disagrees with itself | FR 5 makes Set to Draft available on Running and Expired records; an acceptance criterion of the same ticket makes Expired terminal |
| `gap` — a rule with no value, no boundary or an open list, or a field this scope gives nothing to choose | Scope introduces a Rate Method field "so these can be added later without restructuring", yet in this scope it has exactly one possible value |
| `infeasible` — names something the platform does not have | Flow 1 has an "Account Coordination user" create the price list — a job title, not a security group — while FR 1 grants creation to Sales / Administrator only |
| `reality` — the document's model disagrees with the real documents it describes | FR 4 lists Area among the zone's fields; the real price lists carry several areas per zone |

`infeasible` and `reality` findings are hypotheses until the code or the source documents confirm them — step 2 hands them to the researchers.

### 1.2. Goal phrases become checkable constraints

Some sentences describe a property of the result rather than a feature: "mirrors the shape of the document it replaces". They read as context, so nothing turns them into an acceptance criterion — and a constraint that is no criterion cannot fail on paper. On CFT-322 that exact sentence, the ticket's second, was lost this way: the spec stored one ZIP row per range instead of one per area, ten rows in the system against six on paper, and no check anywhere failed.

1. Find them: sentences in `Business Need`, `User Story` and `Scope` that compare the result to something — "mirrors", "same as", "matches", "replaces", "as on the document", "without changing".
2. Rewrite each as a statement a reviewer can check against one concrete case: "one row in the system per row on the source document".
3. Record them under `## BA Analysis → ### Goal constraints`, each with its anchor. The Analyst carries every one into `## Key Constraints`, and a spec that normalizes away from one needs a `judgment` deviation.

### 1.3. Research and questions

1. Read the draft and the BA document carefully, and finish §1.0–§1.2 before spawning researchers — the index gives every researcher the same vocabulary, and the audit hands them hypotheses to confirm.
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
   > BA document: `{BA_SPEC_PATH}` — read it, and the draft's `## BA Analysis`. Confirm or refute, with `path:line`, every `infeasible` and `reality` finding in `### Audit` that touches your angle.
   > Working directory: `{project root}`
   > Project CLAUDE.md: `{path}`
   > Signal `SPEC RESEARCH REPORT [{angle}]` when done.

   **Research yourself while they run** — do not wait idle. Delegating every angle leaves you judging questions from other agents' summaries, and a summary is exactly where an unverified premise slips through unnoticed. Keep going after the reports arrive when anything is still unclear — the reports are a floor for your own reading, not a ceiling.

   Reject any report missing its DEPTH block, or carrying observations without `path:line`, and re-request a deeper pass — the same rule the critics get.

3. **Merge into `## Codebase Observations`.** Fold every researcher report and your own findings into the draft's `## Codebase Observations` section via `Edit` — one line per fact, **each carrying `path:line`**. Copy `CONTRADICTIONS` across verbatim: a draft assumption the code disproves outranks anything you were about to ask the user. Then run a **gap check** — name the parts of the draft nobody covered, and spawn one follow-up researcher on any material dark area before moving to questions. Run the two self-check questions from *Research has no budget* here — a second follow-up pass is normal, not a failure of the first. Then update `### Audit` in `## BA Analysis`: strike each finding the code refutes, with the evidence, and add the `path:line` to each one it confirms.

4. Compile a list of clarifying questions. A topic the BA document already answers is not a question — note its anchor instead; asking the user what the BA already wrote invites a second, different answer. Topics to cover:
   - **Цель**: Какая бизнес-задача решается? Кому и как это поможет?
   - **Границы**: Что явно НЕ входит в задачу? Есть ли смежные фичи, которые трогать не нужно?
   - **Поведение**: Любые неоднозначные сценарии — спроси, не додумывай.
   - **Крайние случаи**: Что происходит при пустых данных, ошибках, нехватке прав, больших объёмах?
   - **Приоритет и ограничения**: Есть ли дедлайны, требования к производительности, зависимости от другой работы?
   - **Существующее поведение**: Если драфт меняет существующую функциональность — уточни, что сейчас и что именно должно измениться.
   - **Архитектура и интеграция**: Новый модуль или расширение существующего? Есть ли конвенция для похожих фич? Спрашивай ТОЛЬКО когда ответ не очевиден из кодовой базы.
   - **БА-документ**: каждая находка из `### Audit`, которую не закрыло исследование, — противоречие, пробел, невыполнимое, расхождение с реальными документами. Что тикет недосказал или сказал двумя разными способами, решается здесь; где мы хотим сделать не так, как написал БА, — это вопрос `for-BA`.
5. Ask questions **one at a time** using the defer-aware prompt format below. Every question passes the gate first. Tag each one before asking: `answerable-here` when the BA document left it unsaid or said it two ways and the user can settle it now; `for-BA` when the answer would replace what the BA wrote (§ *Deviation ledger*). A `for-BA` question still goes to the user — they may know the BA's answer, or take the call — but its context quotes the BA text at stake with its anchor, and it offers option 5.
6. **After each answer**, immediately append the decision to the draft file under a `## Decisions` section using `Edit`. Number each decision sequentially. Format: `N. **Short label**: decision text`. A decision that departs from the BA document ends with its anchor and ledger class: `N. **Short label**: decision text (FR 4, judgment)`. This section becomes the authoritative source of user decisions for all agents — inline prompt text is supplementary.
7. After each answer, if it reveals new ambiguities, add follow-up questions to the queue. Continue until no questions remain — ask as many as genuinely matter, no padding to a count.
8. When the queue is empty the draft is finished — commit it once, as commit 1 of `## Commits`. The `Edit` in step 6 is what persists each answer; the single commit lands them all as one history entry.

**Rules for this phase:**
- Frame questions in business/domain terms, except architectural topics which are technical by nature.
- Architectural questions only when the codebase doesn't give a clear answer; if there's an obvious convention, note it as context for the Architect, don't ask.
- Include what you learned from exploring the codebase as context ("Я вижу, что сейчас система делает X — Y должен заменить это или работать параллельно?").
- One question at a time.
- Every question is preceded by the step-0 self-check of the gate — a question you could not defend with citations is one you have not finished researching.

## Before any question — the gate

Run these six steps on **each** candidate question before it reaches the user. Phase 1 research tells you what the codebase contains; the gate confirms that *this specific question* is worth asking and correctly posed.

0. **Check yourself before checking the question.** Answer these three honestly for every candidate question:
   - **"Do I have enough context to ask this?"** — can I describe the area the question touches from code I have opened, not from file names or another agent's summary?
   - **"Is this question built on facts or on my guesses?"** — for each thing the question presupposes, name the `path:line` that establishes it. A presupposition with no citation is a guess.
   - **"Would more research remove the question?"** — often the code answers it, and a resolved fact in `## Codebase Observations` is worth more than a question.

   A guess anywhere means the question is not ready: go back to the code and keep reading until every presupposition is a cited fact or an explicit "the codebase does not cover this — greped X, Y, Z". Only then run steps 1–5.
1. **Locate.** Grep for where the answer would live — models, call-sites, existing conventions. Stay on this step as long as the answer might still be in the code — research here has no budget.
2. **Verify the premise.** Take the presuppositions you listed in step 0 and confirm that each actually exists. "Should X replace Y?" is void when `Y` does not exist. Cite what you found.
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

**Language.** Run the QA session in Russian — questions, options, and the +/− trade-offs the user reads and answers. Everything that persists is English — spec sections, plan, code, commit messages, and recorded Decisions/Blockers (translate the gist of the user's Russian answer). The BA question sheet is English as well — it goes to the analyst.

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
5. Отложить к БА — уйдёт в лист вопросов к аналитику и в Blockers

(можешь ответить или отложить вопрос — напиши "пропустить" / "позже" / "не знаю", и вопрос уйдёт в Blockers)
```

Option 5 appears on `for-BA` questions only.

### Understanding the reply

- Option 5, or a reply that sends the question to the analyst ("спрошу у БА", "это к аналитику") → DEFER with `expertise-needed: ba`, and skip the "Кому это может быть известно?" follow-up — the answer is already known. Check this case and the next one before the general DEFER rule below.
- A `for-BA` question the user answers on the BA's behalf ("БА подтвердил", "беру на себя") is an answer. Record who decided, in the Decision and in the blocker's `resolution`.
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
- **raised-by**: lead (Phase 1 / Phase 3) | spec-analyst | spec-architect | spec-critic-arch | spec-critic-business | spec-critic-premise | spec-critic-testing | spec-critic-adaptive:{lens-id}
- **raised-on**: {TODAY}
- **expertise-needed**: ba | business | architecture | testing | security | ux | unknown
- **context**: <what was found in the code or spec, what's ambiguous, what each option would imply; a `ba` blocker quotes the BA text with its anchor and names the ledger line it guards>
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
- Fill `resolution:` with the user's answer — for a `ba` blocker, say who decided: `BA confirmed: …` or `decided by the user on the BA's behalf: …`

## Deviation ledger

Every place where the spec does something other than what the BA document says is one line in the spec's `## BA Traceability → ### Deviations from the BA spec`. The ledger makes each departure visible and classified, and the class decides who has to agree to it.

### Where it lives

`## BA Traceability` is a top-level spec section directly after `## Scope`, holding `### BA Anchor Index` (the index from §1.0) and `### Deviations from the BA spec`. It sits outside `## Scope` deliberately: `Spec-Auditor` turns every In Scope entry into a queue item and traces it to code, and ledger lines describe the spec rather than require anything. The section opens with this italic line, which keeps the Auditor off it the same way the marker in `## Review Lenses` does:

*Traceability metadata — not requirements. Every rule referenced here is also stated in Behavior and Acceptance Criteria; nothing traces to code from this section.*

### Entry format

```
- **{class}** Ticket {anchor}: "{verbatim quote}". Spec: {what the spec does instead}. Why: {one sentence}. (D{N}{, meeting MM:SS}){ → b-N}
```

### Three classes — one needs the BA

| class | what it is | BA agreement | CFT-322 case |
|---|---|---|---|
| `clarification` | the document left a value unsaid, and the spec fills it with one nobody would dispute | not needed | a ZIP prefix is exactly three digits; Name is required from creation |
| `contradiction` | the document disagrees with itself, and the spec picks a side, naming which and why | not needed — recorded | FR 5 against the acceptance criterion that makes Expired terminal |
| `judgment` | the spec replaces what the BA decided with what we think is better | **required** | one ZIP row per range instead of per area; `price_type` as a user choice; Rate Method dropped |

When unsure between classes, it is `judgment`. On CFT-322 the judgment class was three or four of the 36 entries — and those were exactly the ones rebuilt after the user tried the result.

<critical>
A `judgment` deviation always gets a `### b-N` blocker with `expertise-needed: ba`, and its ledger line ends with `→ b-N`. `/task-approve` refuses while that blocker is open. That is intended: the only party entitled to replace the BA's decision is the BA.
</critical>

### The count is a signal

Count the ledger by class whenever it changes. **More than 5 `judgment` entries, or more than 15 entries in total,** means the spec has stopped clarifying the ticket and started rewriting it. Stop and ask with `AskUserQuestion`: continue, or take the document back to the analyst first. Three to five deviations refine a ticket; thirty-six make a different document.

## 1.5. Blocker re-ask (resume runs only)

1. Read the existing spec. Parse `## Blockers` for level-3 headings; collect entries with `status: open`.
   Then compare the BA document at `BA_SPEC_PATH` with the spec's `## Original BA Specification`. They differ → the BA changed the document since the last run: say so, replace the section with the new text verbatim, redo §1.0–§1.2 into the archived draft's `## BA Analysis`, copy the new index into the spec's `### BA Anchor Index`, and send the Analyst a fix round covering every ledger line and `source:` line whose anchor or quote no longer matches. An answer the BA gave by editing the document is still an answer.
2. If zero open blockers and the BA document is unchanged → tell the user "Spec {ID} has no open blockers. Did you mean `/task-approve {ID}`?" and stop.
3. Announce: "Resuming spec {ID}. {N} open blockers from previous runs. I'll go through them — you can answer or defer again."
4. For each open blocker, in order:
   - Print the stored `topic`, `question`, `context`, `expertise-needed`, and `deferred-history`.
   - **Re-verify the stored premise before re-asking.** One cheap grep: does everything the blocker references still exist under that name? A blocker recorded weeks ago may name a model that has since been renamed or deleted, and re-asking it verbatim wastes the user's answer. When the premise still holds, reuse the stored context as-is — this is a premise check, not a fresh exploration. When it no longer holds, rewrite the question against current code and note the change in `deferred-history`.
   - Ask the user using the defer-aware prompt format.
   - On answer: update the blocker entry (status → `resolved-by-user`, append deferred-history line, fill resolution). Remember which agent to re-invoke based on who raised the blocker and the expertise-needed tag (`business` → Analyst, `architecture` → Architect, sometimes both; `ba` → Analyst, who also updates the ledger line the blocker guards).
   - On defer: append a new `deferred-history` line `{TODAY}: deferred again`. Keep `status: open`. Move to the next.
5. Build a set of affected agents from the resolved blockers. If zero blockers got resolved and the BA document is unchanged, tell the user "No blockers were resolved this run. Spec unchanged; come back later." and stop.

## 2. Phase 2 — Background agents

Spawn each agent via `Agent(subagent_type: "...", name: "...", run_in_background: true, prompt: "...")`. The call returns an `agentId` (format `a...-...`); the agent runs asynchronously and notifies you when it completes, its final message being the result.

<critical>
Record the `agentId` from every spawn into a small registry (`name | agentId | role`). A `name` reaches an agent **only while it is running**; once it completes, resume it only by `agentId` — and resuming preserves its context, so it remembers its prior work. This matters for fix rounds and re-checks, which happen after the agent has completed.
</critical>

**Addressing:** running agent → `SendMessage(to: "spec-analyst")`; completed agent → `SendMessage(to: "{agentId}")`. The completion notification is your done signal — do not poll for it.

This applies to every agent spawned with the `Agent` tool. On `REVIEW_BACKEND = codex` the §2c critics are background Bash jobs instead: they have no `name` and no `agentId`, their registry row carries a report path, and `codex-reviewer.md` §5–§7 covers how to address them.

**Liveness:** a dead agent sends no completion notification and nothing else wakes you. Follow `~/.claude/templates/liveness-protocol.md`: arm a dead-man timer per phase (`Bash(run_in_background: true, command: "sleep 900; echo WATCHDOG_ANALYST")` — likewise `WATCHDOG_RESEARCH`, `WATCHDOG_ARCHITECT`, `WATCHDOG_CRITICS`, and `WATCHDOG_ADAPTIVE`), audit on every wake-up, recover via ping-by-`agentId` first, respawn as escalation.

The Phase 1 researchers are background agents too: the registry, the addressing rules, and the liveness protocol above cover them under `WATCHDOG_RESEARCH`.

Spawn the Analyst only after the self-check passes: if you can still name an unexplored part of the draft, Phase 1 is not finished.

Shared context to pass in every agent prompt:
- Draft path (or existing spec path on resume) — **instruct agents to read `## Decisions` (authoritative user decisions) and `## Codebase Observations` (verified facts about the codebase) from the draft file. These two sections are the persistent source of truth — inline prompt context is supplementary.**
- User's Phase 1 answers (new runs) or resolved-blocker answers (resume runs) — inline as supplementary context
- Project `CLAUDE.md` path
- BA document path (`BA_SPEC_PATH`) — **the source of truth**. Also the draft's `## BA Analysis` — the archived draft's on resume runs: the anchor index every agent cites, the audit findings, and the goal constraints.

### 2a. Analyst

**New runs:** Spawn `Agent(subagent_type: "Spec-Analyst", name: "spec-analyst", run_in_background: true, prompt: "...")` and record its `agentId`. The prompt:

> Read your instructions: `~/.claude/agents/spec-analyst.md`
> Spec output path: `{board}/2-spec/{ID}-{slug}.md`
> Spec template: `~/.claude/templates/sdd/spec.md`
> Draft path: `{draft path}` — **read `## Decisions` (authoritative user decisions) and `## Codebase Observations` (verified codebase facts). Every numbered decision MUST be reflected in the spec. Codebase observations inform your writing but don't need 1:1 mapping.**
> User Phase 1 answers: {inline all answers — supplementary context}
> Project CLAUDE.md: `{path}`
> BA document: `{BA_SPEC_PATH}` — **read it in full. It is the source of truth, and the spec follows it.** Read the draft's `## BA Analysis` as well.
> Rules for a BA-backed spec. They extend your instructions file:
> 1. **Point, do not retell.** Objective, Scope and the Behavior narrative cite anchors from the index (`per FR 3`, `Flow 4`) instead of paraphrasing the BA document. Behavior still states every rule with literal values — the pointer replaces the retelling, not the precision.
> 2. **Every AC ends with a `source:` line** naming the BA anchors it expands — `source: FR 3` or `source: Flow 4, AC 7` — or `source: none — <why>` when the AC is ours. For this command the line is part of the AC format.
> 3. **Expansion is expected, substitution is not.** One BA acceptance criterion becoming many of ours is correct. A rule that says something other than the BA document is a deviation.
> 4. **You own `## BA Traceability`.** Create it directly after `## Scope`: the italic metadata line, then `### BA Anchor Index` (the draft's index, copied) and `### Deviations from the BA spec` (one line per departure). Architecture, Change Control and Blockers stay off-limits.
> 5. **A `judgment` deviation is an escalation, never an entry you close yourself.** Send `SPEC ANALYST QUESTION FOR USER` with `Topic: ba-deviation` and `Expertise needed: ba`, quoting the BA text and what you would do instead; add the `→ b-N` from my reply to the ledger line.
> 6. **Every entry in the draft's `### Goal constraints` appears in `## Key Constraints`**, with its anchor.
> Ledger metadata line, entry format and classes: {paste them from § *Deviation ledger*}
> Write the business sections (including Key Constraints, Assumptions, and one `[SENTINEL]` marker in Behavior). Copy the draft's `## Original BA Specification` section into the spec **verbatim** — it is the source document the business sections point at, and the `ba-spec-compliance` review lens checks the implementation against it during `/implement`. Signal `SPEC ANALYST DONE.` when ready. Escalate ambiguities with `SPEC ANALYST QUESTION FOR USER` and wait for my reply.

**Resume runs, business blockers resolved:** Resume the Analyst by its `agentId` (context preserved):

> `FIX ROUND.` Blockers resolved since last run:
> - b-N: {question} → answer: {text}
> - b-M: {question} → answer: {text}
> Apply these to the business sections. Replace any `TBD (see Blockers → b-N)` placeholders with the answer. Update related AC, Examples, Testing Strategy as needed. For a `ba` blocker, update its ledger line as well: an accepted `judgment` keeps its line; a rejected one is deleted and the spec returns to the BA's wording. Signal `SPEC ANALYST FIX ROUND DONE.` when ready.

**Resume runs, no business blockers resolved:** skip this sub-phase.

**Message loop** (runs during both new runs and fix rounds):

Loop until `SPEC ANALYST DONE.` or `SPEC ANALYST FIX ROUND DONE.`:
- On `SPEC ANALYST QUESTION FOR USER`: extract topic, context, question, options, expertise. Format for the user using the defer-aware prompt (embed context as the "Вопрос N/M" background). On answer → `SendMessage(to: "spec-analyst", "ANSWER: <text>")`. On defer → create a `### b-N` entry in the spec's Blockers section via Edit, then `SendMessage(to: "spec-analyst", "DEFERRED: b-N")`.
- A question with `Topic: ba-deviation` is a `for-BA` question: offer option 5. It ends as a `### b-N` with `expertise-needed: ba` either way — resolved on the spot when the user answers (the `resolution` names who decided), open when deferred — and your reply carries the id: `ANSWER: <text> (b-N)` or `DEFERRED: b-N`. When `## Decisions` already settles it — a decision ending in `judgment)` — record the resolved blocker from that decision and reply without asking again.
- On `SPEC ANALYST DONE.` or `SPEC ANALYST FIX ROUND DONE.`: break.
- On `WATCHDOG_ANALYST` firing with no completion: run the liveness check from the protocol — `TaskList` status, then ping by `agentId` (a dead agent is not reachable by `name`), respawn as escalation.

### 2b. Architect

**New runs:** Spawn `Agent(subagent_type: "Spec-Architect", name: "spec-architect", run_in_background: true, prompt: "...")` and record its `agentId`. The prompt:

> Read your instructions: `~/.claude/agents/spec-architect.md`
> Spec path: `{board}/2-spec/{ID}-{slug}.md` (business sections already populated)
> Draft path: `{draft path}` — **read `## Decisions` (authoritative user decisions) and `## Codebase Observations` (verified codebase facts — API signatures, model fields, file paths, patterns, gotchas). Every numbered decision MUST be reflected in the architecture. Codebase observations are your primary reference for integration points.**
> User Phase 1 answers: {inline — supplementary context}
> Project root: `{working directory}`
> Project CLAUDE.md: `{path}`
> Before writing the Architecture section, produce the three "Deep codebase exploration" artifacts (analogous features ≥2, vendor/base classes read, integration call-sites) from your instructions file, and attach them under "Exploration evidence" in your `SPEC ARCHITECT DONE.` message. Treat vendor code inside the repo as part of the project.
> BA document: `{BA_SPEC_PATH}` — read it. Its Flow steps and every screen, field and button it names are architecture inputs. An architecture choice that departs from the BA document — a different model shape, a dropped field — is a `judgment` deviation: escalate it with `SPEC ARCHITECT QUESTION FOR USER` and `Expertise needed: ba` before building on it.
> When this is an Odoo project (its addons carry `__manifest__.py`) and the task creates or changes any view, add `#### View schema` under `### Architecture Decisions (hard)`, directly after `#### Files to modify`. It binds the Coder like every hard decision. One block per view:
>
>     **{view type}: {model} ({where the user meets it})**
>     inherit: {parent view xml id | — (new)}
>
>     | # | field | label | widget | readonly | required | invisible | note |
>     |---|-------|-------|--------|----------|----------|-----------|------|
>
>     statusbar: {field, visible states | —}   buttons: {label → visible when | —}
>     pages / groups: {in on-screen order | —}
>     AC covered: {every AC whose observable lives on this view}
>
> Rows in on-screen order. Conditions literal — `readonly: state != 'draft'`, never "when appropriate". Three rules come from defects that shipped past every reviewer:
> - A field shown on one view and hidden on another with the same meaning is a decision to make, not a layout detail: state in `note` which one is right and why.
> - A field the spec says the system fills carries `computed at: onchange | compute | default` in `note`. A value that becomes right only after save is a defect.
> - A selection with exactly one valid value at a given position is a compute, not an input: note `compute, not a user choice` and plan it that way.
> Fill the `## Architecture & Implementation Plan` section in place. Signal `SPEC ARCHITECT DONE.` when ready. Escalate ambiguities with `SPEC ARCHITECT QUESTION FOR USER` and wait.

**Resume runs, architecture blockers resolved:** Resume the Architect by its `agentId` with `FIX ROUND.` and the resolved blocker answers.

**Message loop:** same shape as 2a, but with `spec-architect` and the Architect signal names.

### 2c. Critics (4 fixed + 4–6 adaptive lenses, in parallel)

Four fixed critics read every spec. On top of them run **4–6 adaptive lenses**: the ones this command seeds (2c-0a — `ba-fidelity` always, `odoo-ui-ux` on an Odoo project with views) and the rest, which you design for this specific spec (2c-0). One critic per lens.

Spawn everything — fixed critics and adaptive lenses — as background agents in **one batch** (all `Agent` calls in a single response). They run in parallel; there are no dependencies. Record every `agentId`.

**Backend.** This section is where `REVIEW_BACKEND` from Setup step 2 takes effect. On `claude`, spawn the `Agent` calls exactly as written in 2c-i…2c-v. On `codex`, every critic here — the four fixed ones and every adaptive lens — becomes a codex run instead: the prompt bodies below are unchanged, and `~/.claude/templates/codex-reviewer.md` supplies the launch form (§4), the registry row (§5), the collection rules (§6) and the "put escalations in the final report" line every codex prompt carries (§8). The Analyst and Architect stay native either way — they `Edit` the spec, and the codex sandbox is read-only. Arm the two watchdogs below on a codex batch as well: an exiting codex job wakes you on its own, but a hung one never exits and nothing else would. On a WATCHDOG wake-up, audit codex rows by report file rather than by `agentId` (`codex-reviewer.md` §6).

Arm two watchdogs: `Bash(run_in_background: true, command: "sleep 1200; echo WATCHDOG_CRITICS")` for the four fixed critics — the testing critic walks every AC, edge case, and example into a matrix, so it runs longer than the other three — and `Bash(run_in_background: true, command: "sleep 1500; echo WATCHDOG_ADAPTIVE")` for the adaptive ones. The adaptive rows can push the batch past the concurrency cap, so later spawns queue for slots — a single 900s timer over the whole batch fires on healthy-but-queued agents, and the respawns it triggers make the contention worse.

The four fixed critic spawns are 2c-i / 2c-ii / 2c-iii / 2c-iv below; the adaptive lenses spawn via **2c-v**.

#### 2c-0. Lens design (think before you spawn)

The four fixed critics read the spec as a document. What they cannot do is cover the angle *this* spec needs — that depends on its domain, size, and complexity, and it is yours to work out. On the spec that motivated this phase, four self-designed angles found more than all the fixed passes combined, because they looked at the system rather than the text: through a tester's eyes, through an attacker's eyes, against existing production data, and against concurrent actions. The tester's-eyes angle earned its own fixed critic (2c-iv) and is covered now; the other three are the kind of angle still yours to design.

1. **Read the fixed lens inventories** in `~/.claude/agents/spec-critic-{arch,business,premise,testing}.md`. You are designing angles they do not cover, so you need to know what they do: arch Lens C already simulates state transitions, arch Lens D already covers data consistency after migration, and testing Lenses T1–T7 already own AC → test coverage down to failure paths and boundary variants. Restating one of those spends a slot on covered ground.
2. **Choose how many** — 4 for ≤5 ACs in a single module; 5 for 6–15 ACs; 6 for >15 ACs, or multi-module, or anything touching data migration, concurrent access, or external integrations. The seeded lenses from 2c-0a count toward that total; you design the rest, and never fewer than two of your own.
3. **Write each lens** as four fields:
   - `lens-id` — short slug, e.g. `concurrent-actions`
   - `angle` — the stance, in one line
   - `justification` — why this spec needs it, **citing something concrete in the spec**: an AC number, a file path, a named state transition, a model name
   - `hunt` — the failure classes this angle should surface
4. **Prefer system-level angles over text-level ones.** Non-exhaustive seeds: attacker's eyes, existing production data, concurrent actions, operations and observability, performance at real scale, permissions and multi-tenancy, failure and rollback, cost. Treat this as a starting point, not a menu — **at least one lens must be specific to this spec's domain and appear on no list.** The justification citation is what separates a designed angle from a picked one. Test coverage of the ACs is the fixed testing critic's ground, so a "tester's eyes" lens here duplicates it — the exception is a testing angle it cannot reach, such as how the feature behaves under a load profile or a data shape only production has.
5. **Announce** the chosen lenses to the user with one line of rationale each, then proceed. This is your call to make — do not wait for approval.
6. **Record them** — the seeded lenses too — in the spec's `## Review Lenses` section via `Edit`, before spawning — appending your entries below the section's italic *"Review metadata — not requirements"* line and leaving that line in place, since it is what keeps `Spec-Auditor` from tracing lenses to code during `/implement`. The briefs otherwise live only in a spawn prompt, and a resume run would lose them.

<bad_pattern>
❌ BAD THOUGHT: "Architecture, business, premise, testing — that's every angle there is. Spawn the batch."
✅ REALITY: Those four are the angles every spec gets. The ones that pay for themselves are the ones only this spec needs, and nobody but you is positioned to name them.
⚠️ DETECTION: About to spawn the critic batch with no `## Review Lenses` block in the spec? → design the lenses first.
</bad_pattern>

#### 2c-0a. Seeded lenses

Two angles recur on every BA-backed task, so this command writes their brief instead of leaving it to be designed. They spawn through 2c-v, are recorded in `## Review Lenses`, and flow through 2d like any other lens. Write each one's `justification` yourself, citing this spec — that is what shows the lens was aimed.

**`ba-fidelity` — always.**

- **angle**: Read the spec beside the BA document as the analyst who wrote that document, and judge whether each departure from it is right — not whether it is recorded.
- **hunt**: a ledger line whose `Why:` would not survive the analyst reading it; a `clarification` or `contradiction` that is really a `judgment`; a BA sentence that became no acceptance criterion and appears in neither the ledger nor Out of Scope; an AC with `source: none` that is a BA requirement in disguise; an AC whose `source:` anchor asks for something broader or different; a goal constraint the model or the Behavior normalizes away; a `judgment` line without `→ b-N`.
- **extra prompt lines**:

  > BA document: `{BA_SPEC_PATH}` — read it in full before the spec.
  > Build the coverage matrix defined in `~/.claude/templates/review-lenses/ba-spec-compliance.md` — its Procedure steps 1–3, its Citation rule and its five statuses. There is no diff yet: skip its steps 4–5 and its `Code` column. Then add one row per ledger line: `| ledger line | class | verdict: sound / wrong class / unjustified | why |`.
  > You are not checking that deviations are recorded — the ledger format and the finalization gate already guarantee that, and checking that bookkeeping is how a well-documented deviation once normalized a document's shape away and passed a fidelity lens. Check whether each one is right.
  > Tag questions only the BA can settle `expertise: ba`.
  > A `judgment` line whose `→ b-N` is still open is correctly parked at this stage: mark it `Deferred`, and judge its class and its `Why:`. Severities come from your agent file (CRITICAL / MAJOR / MINOR), not from the lens file.

**`odoo-ui-ux` — Odoo project with views.** The gate is mechanical: 2c-0 runs after the Architect, who writes `#### View schema` only for an Odoo project whose task touches a view — so the section either holds at least one view block or it does not. No block → skip this lens and say so in the lens announcement. The exception: an Odoo project whose BA document names a screen, button or field, yet has no block, means the Architect missed the schema — send it back before spawning the batch.

- **angle**: Walk every `Flow` step of the BA document through the `#### View schema` tables as the person doing the job, two keystrokes at a time — use the screens rather than read them.
- **hunt**: a field shown on one view and hidden on another with the same meaning; a value that is right only after save; a selection with one valid value at its position; an input the system could compute from position; an error text whose example is narrower than what the rule accepts; a sort that treats an empty field as a real zero; a Flow step the described views give no way to perform; a required field missing from the view that has to set it.
- **extra prompt line**:

  > BA document: `{BA_SPEC_PATH}` — read its Flow steps first; they are the walk.

On CFT-322 four of the five defects the user found were visible within five minutes of using the form and invisible to every agent that read the spec or the code. This lens is the nearest a spec review gets to using the form: the element tables make those defects legible on paper.

#### 2c-i. Architecture Critic

Spawn `Agent(subagent_type: "Spec-Critic-Arch", name: "spec-critic-arch", run_in_background: true, prompt: "...")`. The prompt:

> Read your instructions: `~/.claude/agents/spec-critic-arch.md`
> Spec path: `{board}/2-spec/{ID}-{slug}.md`
> Draft path: `{draft path}` — **read `## Decisions` and verify EVERY numbered decision is correctly reflected in the spec. Any mismatch = CRITICAL finding. Also read `## Codebase Observations` — verify spec's integration points and API claims match the recorded observations.**
> Working directory: `{project root}`
> Phase 1 context: {inline user answers and Lead observations}
> Project CLAUDE.md: `{path}`
> {On resume:} `RESUMED_RUN: true`
> Run your full verification and lens pass (Pass 1 + Lenses A–G). Signal `SPEC ARCH CRITIC REPORT` when done.

#### 2c-ii. Business Critic

Spawn `Agent(subagent_type: "Spec-Critic-Business", name: "spec-critic-business", run_in_background: true, prompt: "...")`. The prompt:

> Read your instructions: `~/.claude/agents/spec-critic-business.md`
> Spec path: `{board}/2-spec/{ID}-{slug}.md`
> Draft path: `{draft path}` — **read `## Decisions` and verify EVERY numbered decision is correctly reflected in the spec's business sections. Any mismatch = CRITICAL finding.**
> Working directory: `{project root}`
> Phase 1 context: {inline user answers and Lead observations}
> Project CLAUDE.md: `{path}`
> {On resume:} `RESUMED_RUN: true`
> This spec follows a BA document (`{BA_SPEC_PATH}`). Its ACs end with a `source:` line naming the BA anchors they expand — part of the AC format here, not a Lens I or Lens R finding. `## BA Traceability` after `## Scope` is traceability metadata the Analyst owns.
> Run your full business quality lens pass (Lenses G–R). Signal `SPEC BUSINESS CRITIC REPORT` when done.

#### 2c-iii. Premise Critic

Spawn `Agent(subagent_type: "Spec-Critic-Premise", name: "spec-critic-premise", run_in_background: true, prompt: "...")`. The prompt:

> Read your instructions: `~/.claude/agents/spec-critic-premise.md`
> Spec path: `{board}/2-spec/{ID}-{slug}.md`
> Draft path: `{draft path}` — **read `## Decisions` and `## Codebase Observations`. Unlike the other agents, you do NOT treat Decisions as authoritative — they are exactly what you scrutinize. Treat every claim, including every recorded user decision, as a hypothesis to disprove.**
> Working directory: `{project root}`
> Phase 1 context: {inline user answers and Lead observations}
> Project CLAUDE.md: `{path}`
> {On resume:} `RESUMED_RUN: true`
> Run your full premise pass (Lenses L1–L6). A sound foundation yields zero challenges — never manufacture one. Signal `SPEC PREMISE CRITIC REPORT` when done.

#### 2c-iv. Testing Critic

Spawn `Agent(subagent_type: "Spec-Critic-Testing", name: "spec-critic-testing", run_in_background: true, prompt: "...")`. The prompt:

> Read your instructions: `~/.claude/agents/spec-critic-testing.md`
> Spec path: `{board}/2-spec/{ID}-{slug}.md`
> Draft path: `{draft path}` — **read `## Decisions` and `## Codebase Observations`. A decision that changes behavior changes what must be tested: verify `## Testing Strategy` covers the behavior every numbered decision introduces.**
> Working directory: `{project root}`
> Phase 1 context: {inline user answers and Lead observations}
> Project CLAUDE.md: `{path}`
> {On resume:} `RESUMED_RUN: true`
> Build the AC coverage matrix, then run your full lens pass (T1–T7). Report the matrix in full — one row per AC. Signal `SPEC TESTING CRITIC REPORT` when done.

#### 2c-v. Adaptive lens critics (one per lens from 2c-0 and 2c-0a)

In the same batch, spawn one `Spec-Critic-Adaptive` per lens — seeded in 2c-0a or designed in 2c-0.

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
Spec path: {board}/2-spec/{ID}-{slug}.md
Draft path: {draft path} — read `## Decisions` and `## Codebase Observations` for the verified ground.
{Seeded lens only:} {its extra prompt lines from 2c-0a}
Working directory: {project root}
Phase 1 context: {inline user answers and Lead observations}
Project CLAUDE.md: {path}
{On resume:} RESUMED_RUN: true
Run your lens pass. Signal `SPEC ADAPTIVE CRITIC REPORT [{lens-id}]` when done."
)
```

**Message loops:** run the fixed critics and the adaptive lenses in parallel. Critics rarely escalate; if any (fixed or adaptive) does, handle it like any other `QUESTION FOR USER`. An adaptive report missing its DEPTH or VERIFIED OK block is rejected and re-requested, same as any critic report.

Wait for **every** agent in the batch — four fixed critics and all adaptive lenses — to complete before proceeding.

### 2d. Apply findings

You now hold four fixed critic reports and one report per adaptive lens. Merge them across all reports. **Dedup** — findings that flag the same issue (across critics, or between a fixed critic and an adaptive lens) collapse to one, keeping the more specific description. **Union of severity** — a finding raised by *any* source counts; a lens-only catch is still a catch. This includes `EMERGENT QUESTIONS FOR USER` from every source — they all feed into Phase 3.

Adaptive findings carry the same `route:` tags and flow through the fix rounds below exactly like critic findings. Where an adaptive lens and a fixed critic disagree, keep both and let the fix round resolve it — divergence is signal.

The premise critic is different in kind: it challenges decisions, not implementation. Its findings are mostly `route: user` and feed **Phase 3** directly — they are not applied through the analyst/architect fix loop below, because only the user can re-decide a decision. The one exception is a premise finding with `route: analyst` (an assumption factually contradicted by code), which joins the analyst fix round. The premise critic gets **no re-check** pass.

The adaptive lenses also get **no re-check** pass. Their findings route to Analyst and Architect normally and land inside the existing 2-fix-round cap; re-checking each lens as well would multiply the rounds without adding coverage. The one exception is `ba-fidelity`: re-check it — resumed by `agentId`, or a fresh codex run with its findings pasted in — whenever a fix round changed a ledger line or any AC's `source:` line. Those edits are the likeliest to reintroduce the drift it exists to catch.

The Analyst, Architect, and the three consistency Critics (arch, business, testing) have completed by now. Resume each Claude agent **by its `agentId`** (not by name) — its preserved context means it remembers its prior work. A critic whose registry row says `backend: codex` has no `agentId` and no preserved context: re-check it with a fresh codex run carrying its previous findings verbatim (`codex-reviewer.md` §7). The Analyst and Architect are always native, so their fix rounds are unaffected by the backend.

A testing critic report without its full COVERAGE MATRIX is rejected and re-requested, same as a missing DEPTH block — the matrix is what shows every AC was walked rather than sampled.

After reports are collected:

- **Business findings** (`route: analyst`) → resume the Analyst by `agentId` with the specific findings, request fixes. Run the Analyst message loop again until `SPEC ANALYST FIX ROUND DONE.`.
- **Architecture findings** (`route: architect`) → resume the Architect by `agentId`. Run the Architect message loop until `SPEC ARCHITECT FIX ROUND DONE.`.
- **Testing findings** route by what the fix touches: a missing case, failure path, or uncovered edge case goes to the Analyst (it is written in behavior terms); test file placement, fixture infrastructure, and unsupported test levels go to the Architect. Carry the suggested case verbatim — it is already written in literal values.
- **After fixes**: optionally re-check with the appropriate critic, resumed by `agentId`:
  - Business findings: `SendMessage(to: "{business-critic agentId}", "RE-CHECK OF: [f-1, f-3]")` → wait for `SPEC BUSINESS CRITIC RE-CHECK DONE.`
  - Architecture findings: `SendMessage(to: "{arch-critic agentId}", "RE-CHECK OF: [f-2, f-4]")` → wait for `SPEC ARCH CRITIC RE-CHECK DONE.`
  - Testing findings: `SendMessage(to: "{testing-critic agentId}", "RE-CHECK OF: [f-5, f-6]")` → wait for `SPEC TESTING CRITIC RE-CHECK DONE.` Re-check this one whenever any AC changed during the fix round — a new or reworded AC arrives with no test plan behind it.
  - On `REVIEW_BACKEND = codex`: launch a fresh codex run per re-checked critic with the findings pasted into the prompt (`codex-reviewer.md` §7). `SendMessage` reaches no codex job, and a summary of the findings gives the fresh run nothing to verify against.
- **Maximum 2 fix rounds per agent.** After two rounds, unresolved business concerns stay in `Edge Cases & Risks`, unresolved architectural concerns stay in `Open architectural questions`, and unresolved testing findings become an `Uncovered:` line at the end of `## Testing Strategy`, one per gap, naming the AC and the missing case. That line is what carries the gap into `/implement` — the Tester writes what it can and Test-Reviewer reports the rest, instead of the gap vanishing at the cap. Phase 3 picks up anything that needs user input.
- **Tiny edits** (typo, missing bullet): Lead may Edit the spec file directly instead of round-tripping through an agent.
- **`EMERGENT QUESTIONS FOR USER`**: deferred to Phase 3, do not resolve here.

## 3. Phase 3 — Post-spec clarification

Many open questions only become visible after Analyst describes behavior, Architect lays out files, and Critic hunts gaps. Phase 1 catches what's askable upfront; Phase 3 catches what emerges from the agents' work.

### Collect open questions

Gather from:
- `Edge Cases & Risks` — table rows with `Status: OPEN` that still need clarification
- `Architecture & Implementation Plan → Open architectural questions`
- `## BA Traceability → ### Deviations from the BA spec` — every `judgment` line still missing its `→ b-N`
- Every critic's `EMERGENT QUESTIONS FOR USER` — the four fixed ones (arch, business, premise, testing) and each adaptive lens; each carries an expertise tag. The testing critic's questions are `expertise: testing` and name a behavior nobody specified — usually "what should happen when this fails?", which the user answers once and the plan then covers. The premise critic's questions challenge decisions the user already made — present them as genuine reconsiderations, not as gaps. An adaptive lens's questions carry its `lens-id`, so the user can see which angle raised them.

### Classify

Tag each question as **user-required**, **auto-resolvable**, or **for-BA**:

- **user-required** — business decisions, domain context, trade-offs, unknowns about production data, UX decisions, anything where the wrong answer creates rework downstream.
- **auto-resolvable** — pure technical defaults (e.g. `index=True` on a foreign key), project conventions documented in `CLAUDE.md`, safe-by-default choices where one option is clearly safer than the other.
- **for-BA** — any question whose answer would replace what the BA document says, and every `expertise: ba` question from a critic. Ask it with option 5; it ends as a `### b-N` with `expertise-needed: ba` whichever way the user replies — resolved on an answer, open on a defer.

**Rule of doubt:** if you are not sure which category, ask the user. A 30-second question is cheaper than a wrong default that surfaces during `/implement`. Per the project's requirement, no silent auto-resolution: even when you pick a safe default for an auto-resolvable question, present it to the user as one option among others and let them accept or override.

### Ask

Every question passes **the gate** (§ *Before any question*) first — including the ones that arrived from a critic. These need it most: they come from another agent's summary rather than from code you read yourself, so their premises are the least verified in the run. Verify the premise against the codebase before putting a critic's question to the user. The self-check applies here with full force: research the critic's premise until you can either confirm it with `path:line` or drop the question.

Use the defer-aware prompt format, one question at a time. Each question carries the full context: what was found in the code, what the spec says, why this question matters.

### Apply answers

Each answer is reflected in the spec immediately:
- Mark the matching item `RESOLVED (user: <answer>)`
- If the answer changes the Architecture section: `SendMessage` to spec-architect, or Edit directly for small fixes
- If the answer adds new ACs: append to Acceptance Criteria and ask Architect to extend the AC → Implementation map
- If the answer changes scope: update In Scope / Out of Scope and ripple the architectural consequences

### On defer in Phase 3

Create a new `### b-N` entry in `## Blockers` following the same format. Continue with the next question.

## 3.5. Heuristic pass

Three CFT-322 defects were already visible in the spec text, as patterns anyone could search for, before any code existed. Run these over the finished spec yourself. Each hit goes to the Analyst or the Architect as a fix round — or, when the spec's choice is deliberate and departs from the BA document, becomes a `judgment` ledger line.

1. **A rule with exactly one passing value is a computation.** For every validation in Behavior and the ACs, count the values that pass it at each position. One → the field is computed from that position, not entered and then rejected. (`price_type` offered two values, and validation always refused one of them.)
2. **"The system fills" means at the moment of input.** For every field the spec says the system sets, find when. On save → the user watches a wrong value until then; move it to input time — in Odoo, an onchange, a compute or a default. (`From` showed 0 until the record was saved.)
3. **When the goal is to reproduce a document, normalization needs a reason.** Compare the model's rows with the source document's rows for one real example. They differ → the difference is a `judgment` deviation with its own `Why:`, or the model changes back. (One ZIP row per range gave ten rows in the system against six on paper, "Queens" three times.)

## Commits

A `/gl-spec` run produces exactly **two** commits: the finished draft, then the finished spec. Every intermediate state — each answer, each agent round, each fix round — lives in the working tree, where `Edit` already persisted it to disk. Two commits per task keep the history readable; a commit per answer or per agent buries real changes under a dozen work-in-progress entries.

Both run in `{main_root}` against absolute board paths, and both follow the two-step `add -- … && commit -- …` form from `board-root.md` §6: the commit records those paths only, so anything else staged in that checkout — which may be a checkout the user is working in right now — stays staged and uncommitted.

**Commit 1 — end of Phase 1.** The draft holds the BA document in `## Original BA Specification`, the index, audit and goal constraints in `## BA Analysis`, every decision in `## Decisions` and every finding in `## Codebase Observations`; the question queue is empty. When §0 created the draft during this run, this commit is also its first.

```
git -C "{main_root}" add -- "{board}/1-draft/{ID}-{slug}.md"
git -C "{main_root}" commit -m "spec({ID}): draft with decisions and codebase observations" \
  -- "{board}/1-draft/{ID}-{slug}.md"
```

**Commit 2 — finalization.** The spec is verified and the draft has moved to the archive (§4, step 17). On a **new** run the path list carries all three paths, which records the draft's move as a rename so the archived draft lands together with the spec:

```
PATHS=("{board}/2-spec/{ID}-{slug}.md" "{board}/archive/drafts/{ID}-{slug}.md" "{board}/1-draft/{ID}-{slug}.md" "{board}/context/{ID}/questions-for-ba.md")
git -C "{main_root}" add -- "${PATHS[@]}"
git -C "{main_root}" commit -m "spec({ID}): specification" -- "${PATHS[@]}"
```

On a **resume** run, drop `{board}/1-draft/{ID}-{slug}.md` from the list — an earlier run already archived it, so the path exists neither on disk nor in the index and `git add` would abort with `fatal: pathspec … did not match any files`, killing the final commit of a 20-minute pipeline. A resume run produces commit 2 only, since Phase 1 is skipped and there is no draft to commit. Keep `questions-for-ba.md` in the list on both kinds of run — §4 rewrites it every time, so it always exists.

When `{main_root}` differs from the current directory, apply the guards in `board-root.md` §6 before each commit: commit only while `{main_root}` is on `dev` with no merge or rebase in progress; otherwise leave the change on disk uncommitted and report the reason. The spec file is still complete either way.

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
4. Verify `## Testing Strategy` has one entry per AC, each carrying a success case and either a failure case or an explicit `no failure mode — <reason>` note, and that it closes with the `Edge Cases covered:` and `Examples covered:` lines. An AC accounted for by an `Uncovered:` line passes this gate — the fix rounds ran and recorded what they could not close. An AC with neither an entry nor an `Uncovered:` line means the section was never audited: send it to the Analyst rather than shipping the spec with a silent hole.
5. Verify `## Examples` has entries for non-trivial Behavior rules.
6. Verify `## Definition of Done` has been populated (items either checked, left unchecked for the human, or marked `N/A — <reason>`).
7. Verify `## Key Constraints` has 3-7 items, each tracing to Behavior or AC.
8. Verify `## Assumptions` is populated (not just the template placeholder).
9. Verify exactly one `[SENTINEL]` marker exists in the Behavior section.
10. Verify `## Original BA Specification` holds the BA document verbatim. `/implement`'s `ba-spec-compliance` lens reads it, and an empty section switches that lens off without a word.
11. **Every AC has a `source:` line.** Count the `source: none` lines; the count goes in the output, and each line carries its reason.
12. **Every anchor in `### BA Anchor Index` is used somewhere** — in an AC's `source:` line, in a ledger line, or named in `### Out of Scope`. An anchor used nowhere is a BA requirement that fell out silently: send it to the Analyst. This is the inverse of step 11, and both are greps.
13. Every entry in the draft's `### Goal constraints` appears in `## Key Constraints` with its anchor.
14. On an Odoo project whose Architecture creates or modifies a view, `#### View schema` has a block for each of those views. Then add a `## Definition of Done` item: `- [ ] UI-Reviewer ran on every view in #### View schema`. `/implement` spawns it on any view change, but it can be skipped by hand — and on CFT-322 skipping it removed the only agent whose job is to use the form.
15. Count the ledger by class and apply § *Deviation ledger → The count is a signal*. Every `judgment` line ends with `→ b-N`, and that blocker exists.
16. Write `{board}/context/{ID}/questions-for-ba.md` — creating `{board}/context/{ID}/` first when the BA document came from `--ba=` or from the draft and that folder does not exist — from `~/.claude/templates/sdd/ba-questions.md`: one entry per `### b-N` with `expertise-needed: ba` and `status: open`, rewritten from scratch on every run. With none open the file says so — it is still written, so its content never lags the spec.
17. Move the draft to `{board}/archive/drafts/` — it is consumed either way, blockers or not.
18. Commit the spec, the archived draft and the question sheet together, as commit 2 of `## Commits`. This is the run's last action; the branches below only produce output.

### If open blockers > 0

The spec stays in `{board}/2-spec/` with `status: awaiting-approval` unchanged. Output:

  ```
  Spec {ID} saved with {N} open blockers in ## Blockers section, {K} of them for the BA.

  Open blockers:
    - b-1 (expertise: ba): {short question}
    - b-2 (expertise: architecture): {short question}

  Questions for the BA: {board}/context/{ID}/questions-for-ba.md
  Deviation ledger: {a} clarification · {b} contradiction · {c} judgment

  Run /gl-spec {ID} again when a person with matching expertise can answer them.
  /task-approve will refuse to approve until Blockers is clean.
  ```

Stop.

### If open blockers == 0

- Output:
  - Brief spec summary (3-5 sentences)
  - Number of acceptance criteria
  - Deviation ledger by class, and the number of `source: none` ACs
  - Number of files in Work breakdown and number of Coders
  - Key risks if any
  - Next step: `Review the spec, make edits if needed, then run /task-approve {ID}.`
