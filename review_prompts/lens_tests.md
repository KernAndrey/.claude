## Lens: Test coverage

Your only job for this call is to find missing test coverage for new
business logic in the diff. Ignore everything else.

### What to flag

- New public (non-`_prefixed`) functions or methods with business logic
  that have no test added in the same diff
- Changed public signatures whose existing tests are now stale or
  missing new-branch coverage
- New error paths or edge cases that tests do not exercise

Use `Glob` for `tests/`, `test_*`, `*_test.py` to discover existing
test files.

### Explicitly out of scope for this lens

- Config files, migrations, `__init__.py`, type stubs, private methods
- Test files themselves — they are test code, not production
- Lint/format/style in tests
- Other review areas — covered by other lenses

### Section 2 header for this lens

Use `### Section 2 — Test coverage findings`.
