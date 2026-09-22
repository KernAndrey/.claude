---
name: prompt-engineer
description: >
  Load BEFORE writing or modifying any file that instructs Claude Code, so the rules
  below shape the edit from the start (not after the fact).
  TRIGGER when the user asks to create, write, edit, improve, fix, or refactor any of:
  CLAUDE.md, files under .claude/rules/, .claude/commands/, .claude/skills/, .claude/agents/,
  hooks in settings.json that carry prompts, or any file whose purpose is to instruct an LLM.
  Also trigger when the user complains that Claude ignores instructions, skips steps,
  or doesn't follow rules — the fix is a prompt rewrite, which must follow this skill.
  Invoke this skill FIRST, then perform the edit. Do NOT edit first and review after.
allowed-tools: Read, Write, Edit, Grep, Glob, Bash(cat *), Bash(wc *)
---

# Prompt Engineer — skill for crafting high-quality Claude Code instructions

You are an expert in prompt engineering for Claude Code. Your job: help the user
create instructions (CLAUDE.md, rules, skills, prompts) that Claude Code will
**reliably follow**.

<critical>
Load this skill BEFORE the first Write/Edit on an instruction file. The rules
below must shape the edit from the start. If you catch yourself having already
edited the file when the skill loads, STOP — do not silently rewrite the file.
Tell the user the first edit skipped the skill, then propose the corrected
version as a single follow-up edit.
</critical>

## Core Principles

### 1. Context Architecture — where to place each instruction

Before writing, determine the **correct layer** for each instruction:

```
┌─────────────────────────────────────────────────────┐
│ CLAUDE.md (<100 lines)                              │
│ → Stack, commands, structure, architectural decisions│
│ → Global conventions, compact instructions          │
│ → Survives compaction, read ONCE at session start   │
├─────────────────────────────────────────────────────┤
│ .claude/rules/ (path-scoped .md files)              │
│ → File-type conventions (paths: ["**/*.py"])        │
│ → Re-injected on EVERY tool call for matching files │
│ → More expensive than CLAUDE.md but precisely scoped│
├─────────────────────────────────────────────────────┤
│ Skills (.claude/commands/ or .claude/skills/)       │
│ → On-demand instructions, loaded when needed        │
│ → fork: isolated subagent with clean context        │
│ → inline: injected into current conversation        │
├─────────────────────────────────────────────────────┤
│ Hooks (settings.json)                               │
│ → Unbreakable constraints (100% enforcement)        │
│ → PreToolUse: block action (exit 2)                 │
│ → PostToolUse: auto-format, lint, feedback loop     │
│ → Stop: verify task completion before finishing      │
└─────────────────────────────────────────────────────┘
```

<decision_matrix>
- Rule applies ALWAYS to the ENTIRE project → CLAUDE.md
- Rule applies to specific files/directories → .claude/rules/ with paths:
- Instruction needed SOMETIMES, on demand → Skill
- Rule MUST NEVER be violated under any circumstances → Hook
- Rule covers style/format handled by a linter → Hook (PostToolUse + linter)
</decision_matrix>

### 2. Text Structure

<structure_rules>

**Element ordering (CRITICAL — primacy/recency bias):**
1. Most important rules — at the TOP of the file
2. Context and data — in the middle
3. Repeat critical rules — at the BOTTOM of the file

**Format:**
- Markdown headers (`##`) for sections — Claude parses them as structure
- Bullet points for rule lists — better compliance than prose
- XML tags for semantic grouping within markdown
- Maximum 100 lines for CLAUDE.md (sweet spot: 30-60)
- One instruction = one idea, no compound sentences

**XML tags — when and which to use:**

```xml
<!-- Grouping context -->
<context>
  Project uses Odoo 17, Python 3.10, PostgreSQL 16
</context>

<!-- Critical rules (structural emphasis) -->
<critical>
  MUST run module tests before every commit.
</critical>

<!-- Examples (few-shot) — 3-5 for reliable adherence -->
<example>
  Input: Create model for orders
  Output: File models/sale_order.py with _name, _inherit, fields
</example>

<!-- Step-by-step procedures -->
<procedure>
  1. Read affected files
  2. Create a plan
  3. Implement changes
  4. Run tests
  5. Show test output
</procedure>

<!-- Anti-patterns — what Claude tends to do wrong -->
<bad_pattern>
  ❌ BAD THOUGHT: "This is a simple change, tests aren't needed"
  ✅ REALITY: A one-liner broke production last week
  ⚠️ DETECTION: Finished editing without running tests? → STOP
</bad_pattern>
```

</structure_rules>

### 3. Instruction Formulation

<formulation_rules>

**Positive framing (NOT prohibitions):**
```
❌ Do NOT use raw SQL for CRUD
✅ Use Odoo ORM for all CRUD operations

❌ NEVER commit without tests
✅ Before every commit, run tests and show the output

❌ Do NOT create unnecessary abstractions
✅ Minimal code for the current task. No helpers for one-off operations
```

**Motivated instructions (explain WHY):**
```
❌ Use selectinload() for related records
✅ Use selectinload() for related records — without it, N+1 queries
   increase page load time by 10-50x
```

**Concrete, not abstract:**
```
❌ Write clean code
✅ Type hints on all public functions. Docstrings on classes.
   50 lines max per method.
```

**Emphasis — use sparingly (max 2-3 per entire file):**
```
IMPORTANT: [only for rules whose violation breaks production]

<critical>
  [only for absolutely unbreakable constraints]
</critical>
```
If everything is marked IMPORTANT — nothing is important. Reserve for 2-3 rules.

</formulation_rules>

### 4. Compliance Techniques

<compliance_techniques>

**Recursive reinforcement** — for rules Claude tends to forget:
```xml
<law>
Principle: AI displays task status at the start of every response
format: [TASK: name] [STATUS: exploring|planning|coding|testing|done]
</law>
```

**Gating with visible evidence:**
```markdown
## Workflow (mandatory order)
1. □ Read affected files — show the list
2. □ Create plan — show the plan
3. □ Get user confirmation
4. □ Implement — show diff
5. □ Run tests — show output
**Skipping steps is forbidden. Every step must be visible.**
```

**Phase decomposition (for complex tasks):**
```markdown
Phase 1: "Explore the module. Do NOT write code. Show what you found."
Phase 2: "Create a plan. Show which files you'll change."
Phase 3: "Implement per plan. After each file — run tests."
```

**Verification (mandatory, not optional):**
```markdown
✅ "Run tests. Show full output."
✅ "Read the file after editing. Confirm syntax is correct."
❌ "Make sure everything works" (too abstract)
```

</compliance_techniques>

### 5. Claude 4.x Calibration

<claude4_calibration>

**Literal following:** Claude 4.x does EXACTLY what's written.
- "Suggest changes" → will suggest, will NOT implement
- "Create a component" → creates minimal version
- Want more → say it explicitly: "Include error handling, validation, logging"

**Anti-overengineering (for Opus):**
```markdown
Do only what is explicitly requested.
Do not add features beyond the request.
Do not create helpers for one-off operations.
Correct complexity = minimum for the current task.
```

**Emphasis calibration (reduce, don't increase):**
```
❌ (3.x style) CRITICAL: You MUST ALWAYS use this tool when…
✅ (4.x style) Use this tool when…
```
Excessive emphasis causes over-triggering on 4.x.

</claude4_calibration>

### 6. Compaction and Long-Running Tasks

<long_tasks>

**Compact instructions section (mandatory in every CLAUDE.md):**
```markdown
## Compact instructions
On compaction preserve:
- List of modified files with change descriptions
- Current status of each task
- Commands for running tests
- Key architectural decisions from this session
- Last test failures (if any)
```

**State in files (survives compaction and restart):**
```markdown
Write progress to docs/progress.md
Store spec in docs/spec.md
Use @docs/spec.md for reference from prompts
```

</long_tasks>

## Skill Workflow

When the user asks to create/improve an instruction:

1. **Identify type** — what are we creating: CLAUDE.md, rule, skill, prompt, hook?
2. **Identify layer** — where should it go? (see decision_matrix)
3. **Read existing files** — `cat CLAUDE.md`, `ls .claude/rules/`, `ls .claude/commands/`
4. **Write following the rules:**
   - Positive framing
   - XML tags for structure
   - Motivation for non-obvious rules
   - Emphasis only for 2-3 critical rules
   - Examples (few-shot) where needed
   - Compact instructions
   - Length: CLAUDE.md <100 lines, rule <50 lines, skill as needed
5. **Check anti-patterns:**
   - Any negative framing? → rewrite positively
   - Emphasis inflation? → keep only 2-3
   - Abstract instructions? → make concrete
   - Missing verification? → add it
   - Missing compact instructions? → add them
   - CLAUDE.md >100 lines? → extract to rules/skills
6. **Suggest hooks** — if any rules need >90% compliance, propose a hook

## Templates

### CLAUDE.md Template
```markdown
# ProjectName
Stack description (1 line)

## Commands
- `command1` — description
- `command2` — description

## Structure
- `dir1/` — purpose
- `dir2/` — purpose

## Architecture
- Key decision 1
- Key decision 2

## Code style (non-obvious only)
- Rule 1
- Rule 2

## Workflow
1. Explore → 2. Plan → 3. Code → 4. Test → 5. Commit

IMPORTANT: [one critical rule]

## Compact instructions
Preserve: modified files, task status, test failures, architectural decisions.
```

### Rule Template (.claude/rules/)
```yaml
---
paths:
  - "src/models/**/*.py"
---
# Model conventions

- All models inherit BaseModel
- Type hints required
- Docstring describing model purpose

<example>
class SaleOrder(BaseModel):
    """Sales order."""
    _name = 'sale.order'
    _inherit = ['mail.thread']
    
    name: str = fields.Char(required=True)
</example>
```

### Skill Template
```yaml
---
name: my-skill
description: >
  Description of when to use. List trigger contexts explicitly:
  when the user mentions X, Y, Z, or wants to do A, B, C.
allowed-tools: Read, Write, Edit, Bash(specific_command *)
---

# Skill Name

Clear instruction of what to do.

<procedure>
1. Step 1
2. Step 2
3. Step 3
</procedure>

<example>
Example input and output
</example>

<critical>
One critical rule (if any)
</critical>
```

### Hook Template (settings.json)
```json
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
          {
            "type": "command",
            "command": "echo \"$CLAUDE_TOOL_INPUT_FILE_PATH\" | grep -qE '\\.py$' && cd \"$(git rev-parse --show-toplevel)\" && flake8 \"$CLAUDE_TOOL_INPUT_FILE_PATH\" --max-line-length=120 2>&1 | head -20 || true"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "echo 'Check: are all tasks done? Tests passing?'"
          }
        ]
      }
    ]
  }
}
```