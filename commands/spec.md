Generate a specification from a draft task using an agent team.

## Instructions

1. Read `.tasks.toml`. If missing → tell user to run `/task-init` and stop.
2. Locate the draft by `$ARGUMENTS` — match by ID, slug, or full path in `tasks/draft/`.
3. Read the draft file content.
4. Read `CLAUDE.md` for project context.

## Agent Team

Create an agent team with two teammates:

### Teammate: Analyst

**Role:** Turn a raw idea into a structured, actionable specification.

**Tasks:**
1. Read the draft task carefully.
2. Explore the project codebase:
   - Module/app structure
   - Relevant models, views, controllers, API endpoints
   - Existing test patterns and frameworks
3. Write the spec using template `~/.claude/templates/sdd/spec.md` (or `.claude/templates/spec.md` if project override exists).
   Fill every section:
   - **Objective**: clear goal in 1-2 sentences
   - **Scope**: what's IN and what's explicitly OUT (critical for implementer)
   - **Technical Approach**: concrete plan referencing real project files, classes, methods
   - **Acceptance Criteria**: each must be independently testable
   - **Edge Cases & Risks**: data volume, permissions, concurrency, empty states
   - **Files to Modify**: specific file paths
4. Save draft spec to `tasks/spec/{ID}-{slug}.md`. Ensure frontmatter `status: awaiting-approval` is set (it comes from the template).
5. Message Critic that the draft spec is ready for review.

**Rules:**
- Do NOT invent requirements — if something is unclear, note it in Risks.
- Acceptance criteria must be concrete and testable, not subjective.
- Technical approach must reference actual files and classes in the project.

### Teammate: Critic

**Role:** Find weaknesses, gaps, and risks in the specification.

**Tasks:**
1. Wait for Analyst's draft spec.
2. Review against this checklist:
   - [ ] Is scope clearly defined? No gray areas?
   - [ ] Is technical approach realistic? No conflicts with existing code?
   - [ ] Are acceptance criteria testable? No subjective criteria?
   - [ ] Edge cases covered? (empty data, large volumes, permissions, concurrency)
   - [ ] Security implications? (if applicable)
   - [ ] Performance implications? (at N>1000 records)
   - [ ] Any overlap with other tasks in `tasks/`?
3. Send specific findings directly to Analyst (via team messaging).
4. Analyst incorporates fixes → Critic does a final pass.
5. If satisfactory — confirm to the lead.

**Rules:**
- Be specific: not "think about performance" but "method X will cause N+1 queries at >1000 records".
- If a critical issue is found — do not let it pass, even if Analyst disagrees.
- Maximum 2 review rounds. Then finalize, noting unresolved concerns in Edge Cases & Risks.

## Finalization (Lead)

After the team finishes:
1. Verify the spec file in `tasks/spec/` has `status: awaiting-approval`.
2. Move the draft to `tasks/archive/drafts/`.
3. Output:
   - Brief spec summary (3-5 sentences)
   - Number of acceptance criteria
   - Key risks if any
   - Instruction: "Review the spec, make edits if needed, then run `/task-approve {ID}`"
