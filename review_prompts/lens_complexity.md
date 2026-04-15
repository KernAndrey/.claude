## Lens: Complexity

Your only job for this call is to find unreasonable complexity added
by the diff. Ignore everything else.

### What to flag

- Nesting deeper than 3 levels of control flow
- Functions or methods longer than ~30 lines of logic
- Classes doing too many unrelated things (god object)
- Unclear control flow: deeply nested conditions with multiple early
  returns mixed with post-return work

### Explicitly out of scope for this lens

- Formatting, whitespace, line length — linters handle these
- Naming — stylistic, not a complexity concern
- Complexity that was already present before the diff (only flag
  complexity ADDED by the change)
- Other review areas

### Section 2 header for this lens

Use `### Section 2 — Complexity findings`.
