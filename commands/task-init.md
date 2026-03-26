Initialize Spec-Driven Development for the current project.

## Instructions

1. Read the project root. Determine the project type (Odoo, Django, Node.js, etc.) and a suitable short prefix (e.g. TMS, ALT, WEB — uppercase, 2-4 chars based on project name or domain).
2. Create the task directory structure:
   ```
   tasks/
     1-draft/
     2-spec/
     3-ready/
     4-in-progress/
     5-review/
     6-done/
     7-blocked/
     archive/
       drafts/
   ```
   Add `.gitkeep` to each empty directory.
3. Create `tasks/.counter` with content `0`.
4. Create `.tasks.toml` in project root:
   ```toml
   [tasks]
   dir = "tasks"
   id_prefix = "<DETECTED_PREFIX>"
   auto_branch = true
   counter_file = "tasks/.counter"

   [spec]
   # Override templates per-project if needed:
   # analyst_prompt = ".claude/prompts/spec-analyst.md"
   # critic_prompt = ".claude/prompts/spec-critic.md"

   [implement]
   max_review_iterations = 2
   ```
5. Read existing `CLAUDE.md` (if present). Append the following section (do NOT overwrite existing content):

   ~~~markdown
   ## Spec-Driven Development Workflow

   This project uses Spec-Driven Development for large tasks — specs are written
   and reviewed BEFORE code is written.

   ### Commands

   | Command | Description | Agent Team? |
   |---------|-------------|-------------|
   | `/task <description>` | Create draft from description | No |
   | `/spec <ID>` | Generate spec from draft | Yes (Analyst + Critic) |
   | `/task-approve <ID>` | Approve spec → ready | No |
   | `/implement <ID>` | Implement approved spec | Yes (Coder + Reviewer) |
   | `/task-done <ID>` | Mark task as done | No |
   | `/task-block <ID> <reason>` | Block task | No |
   | `/decompose <idea>` | Decompose big idea into tasks | No |
   | `/task-list [filter]` | Show task board | No |

   ### Task Lifecycle

   ```
   draft → spec (awaiting-approval) → ready → in-progress → review → done
                                                       ↘ blocked ↗
   ```

   ### When to Use

   - **Use SDD**: new features, large refactors, cross-module changes
   - **Skip SDD**: bugfixes, small tweaks, styling — work directly

   ### Configuration

   - Project config: `.tasks.toml`
   - Templates: `~/.claude/templates/sdd/` (global) or `.claude/templates/` (project override)
   - Tasks: `tasks/` (directories numbered for workflow order: `1-draft`, `2-spec`, etc.)

   ### Human-in-the-Loop Checkpoints

   1. After `/spec` — read and approve the spec (`/task-approve`)
   2. After `/implement` — walk through Steps for Manual Review (`/task-done`)
   ~~~

6. Output: list of created files, detected prefix, confirmation message.
