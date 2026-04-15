## Lens: Type annotations (Python)

Your only job for this call is to find missing or wrong type
annotations on new or changed Python functions. Ignore everything else.

### What to flag

- New or changed function / method parameter without a type annotation
- Missing return annotation on a new or changed function / method
- `*args` or `**kwargs` without annotation on a new or changed
  function / method
- A return annotation that contradicts what the function actually
  returns (e.g. `-> None` on a function that returns a value)

### Explicitly out of scope for this lens

- Non-Python files (write `No findings in this lens.` quickly)
- Annotations on code that was NOT changed in this diff — we only
  enforce the rule on new or modified signatures
- Style (annotation formatting, typing style) — linters handle that
- Other review areas

### Section 2 header for this lens

Use `### Section 2 — Type annotation findings`.
