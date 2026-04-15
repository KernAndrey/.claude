## Lens: Semantic duplication

Your only job for this call is to find code duplicated semantically
(not just textually) with existing code in the project. Ignore
everything else.

### What to flag

- Copy-paste blocks with renamed variables
- Similar logic that should be extracted to a helper or shared module
- New utility functions that duplicate an existing one

Use `Grep` with synonym strategy:
- calculate → compute / get / eval
- create → make / build / generate
- process → handle / transform / parse
- validate → check / verify / ensure

Search by key logic terms, not just function names. Check neighboring
files in the same module before reaching further.

### Explicitly out of scope for this lens

- Boilerplate (imports, decorators, test setup/teardown)
- Intentional polymorphism (e.g. subclasses implementing the same
  abstract method with different bodies)
- Other review areas

### Section 2 header for this lens

Use `### Section 2 — Duplication findings`.
