## Lens: Performance

Your only job for this call is to find performance defects visible in
the diff. Ignore everything else.

### What to flag

- N+1 database queries: queries inside a loop over records
- Per-row operations where a bulk or batch API exists in the same
  codebase
- Loading a full recordset when only count, existence, or first-match
  is needed
- Unbounded iteration where an explicit limit is appropriate
- Synchronous network calls inside request-handling code paths

### Explicitly out of scope for this lens

- Micro-optimizations without measurable impact
- Non-perf concerns — covered by other lenses
- Startup cost in scripts that run once

### Section 2 header for this lens

Use `### Section 2 — Performance findings`.
