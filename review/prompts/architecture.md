## Lens: Architecture, duplication, simplicity

Your job for this call is to **find every place where the diff could
have solved the problem more simply, duplicates existing logic, or
violates the project's architecture**. Other lenses cover bugs and
test coverage — ignore those here.

Walk the diff end-to-end. Check every added unit against all four
categories. Do not stop at the first finding.

### The four categories — find every instance

**1. Simplicity — could this task be solved more simply?** This is
the primary category. Ask at the task level (not just the code
level): what problem does this change solve, and is there a shorter
path?

Steps:
- If the user prompt has a `Developer's commit message draft` section,
  start from it (but verify it matches the code).
- Read the diff. Formulate: what does this change accomplish? What is
  the input, output, effect?
- Ask: is there a **concrete shorter alternative**?
  - Standard library already covers the use case: `csv`, `urllib.parse`,
    `pathlib`, `functools.lru_cache`, `itertools`, `contextlib`,
    `dataclasses`, `collections.Counter`, `dict.setdefault`, etc.
  - A helper / service / pattern already exists in this repo — `Grep`
    for functionally close names before flagging a new implementation.
  - A popular library already in the project manifest
    (`pyproject.toml` / `package.json`) covers this: requests/httpx,
    tenacity, pydantic, lodash.
  - The task dissolves with a smaller scope: "just use the existing
    flow" beats "build a parallel flow".

A flag requires a **concrete alternative** AND a **rough benefit
estimate** (at least -10 lines, or reuse of an already-imported
tool). Without a named alternative: stay silent. Without a benefit
estimate: downgrade to `[WARNING]`.

Examples:
- Valuable: 30 lines of hand-written `key=value\n` parsing → cite
  `configparser` or `csv.reader(f, delimiter='=')`.
- Valuable: a new retry loop while `tenacity` is already imported
  elsewhere in the repo.
- Useless: "this class could be simpler" without naming what to do
  instead. Do not flag.

Do NOT flag:
- Extract-method helpers that improve readability, even if called from
  one place.
- Named constants used once for semantic clarity.
- Factory / strategy / interface patterns where 2+ implementations
  exist or are explicitly required.
- Parameters that have plausible real use cases, even if only one
  value is passed today.

**2. Over-abstraction.** Abstract class / factory / strategy /
interface introduced for a single concrete implementation with no
requirement for extension.

- `AbstractEmailRenderer` with one subclass `HTMLEmailRenderer` →
  replace with a function.
- New registry pattern for one registered item.
- Generic / template / metaclass written for one concrete use case.

**3. Semantic duplication.** New function duplicates existing logic
under a different name — not textual copy (jscpd catches that), but
semantic copy with renamed variables or reworded names.

- Method: `Grep` synonym terms: `calculate`→`compute`/`evaluate`,
  `create`→`make`/`build`/`generate`, `validate`→`check`/`verify`/`ensure`,
  `process`→`handle`/`transform`/`parse`.
- Compare the logic bodies, not just the function names.

Example from real review logs: a new `_render_and_send()` method sits
alongside an existing `_render_email_content` + `_send_email_to_partner`
pipeline — neither is canonical; that is a duplication finding.

**4. Architectural fit.** Does the new code live in the correct layer
for this project? Layers and conventions are **project-specific**
(Django ≠ Odoo ≠ FastAPI ≠ Rails), so:

- Read `CLAUDE.md` at the project root first — it describes the
  architecture, layers, and conventions.
- `Glob` neighboring directories (`src/**`, `models/**`, `services/**`,
  `controllers/**`) to infer the mental model: where does business
  logic live, where does I/O live, where do views live.
- Only then judge: does the new code fit? Is business logic in the
  service layer rather than the controller / view? Is a helper used
  from 3+ places extracted to a shared module rather than copied? Does
  the new module follow its neighbors' structure and naming?

If the project offers no clear architectural signal, leave this
category empty. Do not flag on generic knowledge alone.

### Out of scope for this lens

- Bugs, security, performance, race conditions — `bugs` lens.
- Missing test coverage — `tests` lens.
- Cyclomatic complexity / nesting depth / function length — ruff
  C901, PLR0912, PLR0915 if configured in the project; we do not
  duplicate them.
- Style, naming, formatting — linters.

### Defensive checks against impossible states

As a sub-case of simplicity, flag null checks, exception catches, or
validations for conditions that cannot occur in real code:

- Framework / type system already guarantees non-null at this call
  site.
- Upstream validation already rejected the case.
- Internal invariant makes the branch unreachable.

Rule from CLAUDE.md: validate at system boundaries, trust internal
code.

### Section 2 header for this lens

Use `### Section 2 — Architecture findings`.
