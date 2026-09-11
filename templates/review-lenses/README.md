# Standing review lenses

A **standing lens** is a review angle that recurs across tasks, carries its own
instruction file, and fires only when a mechanical gate says it applies. The
lead checks the gates in `~/.claude/commands/implement.md` Phase 2 and spawns
each applicable lens as an `Adaptive-Reviewer` with a `LENS_INSTRUCTIONS` line
pointing at the file here.

Dispatching through `Adaptive-Reviewer` is deliberate: adaptive rows already get
the `DEPTH` check, the fix-round routing, the re-review pass and the
finalization gate. A new fixed reviewer would need all four wired by hand.

| Lens | Gate | File |
|---|---|---|
| `style-conformance` | `.claude/rules/code-comments.md` exists | `style-conformance.md` |
| `ba-spec-compliance` | spec has a non-empty `## Original BA Specification` | `ba-spec-compliance.md` |
| `odoo-rpc-permissions` | Odoo project **and** diff touches `models/`, `controllers/`, `security/*.xml`, `ir.model.access.csv`, `@http.route`, `sudo()`, `groups=` | `odoo-rpc-permissions.md` |
| `odoo-version-compat` | `.claude/refs/odoo*-breaking-changes.md` exists | `odoo-version-compat.md` |

Standing lenses do not replace the free-form adaptive lenses the lead designs
per diff. They cover the angles that recur; the free-form slot covers the angle
only this change needs.

## Adding a lens

1. Drop a file here, following the shape the four existing ones share:
   `## Gate` → `## Source of truth` → `## Procedure` → `## Citation rule` →
   `## Not a finding` → `## Severity` → `## Report` with its DEPTH fields.
2. Add a row to the table above **and** to the standing-lens table in
   `~/.claude/commands/implement.md` Phase 2. A lens listed in only one place
   never runs.
3. Give it a gate a reader can evaluate mechanically — a file that exists, a
   path pattern in the changed-files list. "When it seems relevant" is not a
   gate.

## Two rules every lens here follows

**Cite the source.** Each finding names the diff line and the line of the rule,
reference row or requirement it violates. A lens that cannot point at what it is
comparing against produces opinions, and opinions train the reader to skip lens
output.

**Skip rather than improvise.** When the source file a lens depends on is
absent, it reports `VERDICT: SKIPPED` with the reason. Inventing a convention
the project declined to write is worse than reviewing nothing.
