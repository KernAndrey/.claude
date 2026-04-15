## Lens: Root-cause vs cosmetic fixes

Your only job for this call is to judge whether bug-fix-shaped diffs
address the root cause or paper over a symptom. Ignore everything else.

This lens matters only for diffs that touch error handling,
conditions, branches, or guard logic. If none of the files in the
diff match that shape, write `No findings in this lens.` quickly.

### What to flag

- Catching an exception instead of preventing it from being raised
- Adding a null-check instead of fixing why the value is null
- Adjusting a test assertion to make it pass instead of fixing the
  code it tests
- Adding a retry instead of fixing the underlying failure
- Silencing a warning / log line without addressing its cause
- Widening a type or loosening a constraint to avoid an error instead
  of fixing the call site

A real fix changes the code path that produces the wrong result. A
cosmetic fix changes what happens after the wrong result is already
produced.

### Explicitly out of scope for this lens

- Style, naming, docs — linters or other lenses handle those
- Other review areas

### Section 2 header for this lens

Use `### Section 2 — Root-cause findings`.
