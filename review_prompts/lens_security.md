## Lens: Security

Your only job for this call is to find security defects in the diff.
Ignore test coverage, duplication, performance, complexity, types, and
root-cause-vs-cosmetic — those are handled by other lenses.

### What to flag

- SQL injection: string formatting or concatenation in SQL queries
- Command injection: `eval()`, `exec()`, `os.system()`, `subprocess`
  with `shell=True` on dynamic input
- XSS: unsanitized user input rendered in HTML / templates
- Path traversal: unsanitized file paths from user input
- Unsafe deserialization: `pickle.loads()`, `yaml.load()` without
  `SafeLoader` on untrusted data
- Secrets or auth material introduced into non-secret code paths
- Auth/authz bypass: missing permission checks on newly-added
  endpoints, handlers, or admin surfaces

### Explicitly out of scope for this lens

- Hardcoded secrets, API keys, passwords — separate scan covers them
- Non-security concerns (coverage, perf, types, ...) — other lenses

### Section 2 header for this lens

Use `### Section 2 — Security findings`.
