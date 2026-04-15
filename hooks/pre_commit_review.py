#!/usr/bin/env python3
"""
Git pre-commit code review gate.

Called by ~/.claude/git-hooks/pre-commit. Modes:

- Small diff (added lines < FANOUT_THRESHOLD): single-call reviewer.
- Large diff (added lines >= FANOUT_THRESHOLD): 7 parallel lens calls
  (security / tests / duplication / performance / rootcause /
  complexity / types), aggregated, then passed through a Claude Opus
  arbiter that UPHOLDs or OVERTURNs each [CRITICAL] finding.

Exit codes:
    0 — commit allowed (review passed or skipped)
    1 — commit BLOCKED (critical issues upheld)

Skip with: git commit --no-verify
"""

from __future__ import annotations

import concurrent.futures
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# --- Configuration ---
GLOBAL_PROMPT = Path.home() / ".claude" / "review_prompt.md"
PROJECT_PROMPT = Path(".claude") / "review_prompt.md"
LENS_DIR = Path.home() / ".claude" / "review_prompts"
ARBITER_PROMPT_PATH = LENS_DIR / "arbiter.md"
LOG_DIR = Path.home() / ".claude" / "review-logs"

MAX_DIFF_LINES = 2000
MIN_LINES_TO_REVIEW = 1
FANOUT_THRESHOLD = 150  # added-line count above which we fan-out
TIMEOUT_SECONDS = 1200
ARBITER_MODEL = "opus"
ARBITER_TIMEOUT_SECONDS = 900

LENS_NAMES = (
    "security",
    "tests",
    "duplication",
    "performance",
    "rootcause",
    "complexity",
    "types",
)

# Extensions that carry executable logic. Used by the lens router to skip
# lenses that have nothing to examine on a docs/config/spec-only diff.
CODE_EXTS: frozenset[str] = frozenset({
    # Python
    ".py",
    # JS/TS family (comprehensive)
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte",
    # Other typed / compiled languages
    ".go", ".rs", ".java", ".kt", ".swift",
    ".rb", ".php", ".cs",
    # Shell
    ".sh", ".bash",
})
PY_EXTS: frozenset[str] = frozenset({".py"})


def warn(msg: str) -> None:
    print(f"\033[33m⚠️  [code-review] {msg}\033[0m", file=sys.stderr)


def error(msg: str) -> None:
    print(f"\033[31m❌ [code-review] {msg}\033[0m", file=sys.stderr)


def info(msg: str) -> None:
    print(f"\033[36mℹ️  [code-review] {msg}\033[0m", file=sys.stderr)


def read_file(path: Path | str) -> str:
    """Read file contents, return empty string if not found."""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError):
        return ""


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def get_staged_diff() -> tuple[str, str]:
    """Get the staged diff. Returns (diff_text, error_msg)."""
    result = subprocess.run(
        ["git", "diff", "--cached"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return "", f"git diff --cached failed (rc={result.returncode}): {result.stderr.strip()}"
    return result.stdout.strip(), ""


def get_staged_files() -> str:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def get_git_status() -> str:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "(git status failed)"


def count_changed_lines(diff: str) -> int:
    """Count lines added or removed in the diff (excluding file headers)."""
    return sum(
        1
        for line in diff.split("\n")
        if (line.startswith("+") and not line.startswith("+++"))
        or (line.startswith("-") and not line.startswith("---"))
    )


def count_added_lines(diff: str) -> int:
    """Count only added ('+') lines, excluding '+++' file headers.

    This is the size signal used to route to single-call vs fan-out
    review: only new code needs LLM attention.
    """
    return sum(
        1
        for line in diff.split("\n")
        if line.startswith("+") and not line.startswith("+++")
    )


# ---------------------------------------------------------------------------
# Diff processing
# ---------------------------------------------------------------------------

def check_diff_size(diff: str) -> str | None:
    """Return an error message if the diff exceeds MAX_DIFF_LINES, else None."""
    line_count = len(diff.split("\n"))
    if line_count <= MAX_DIFF_LINES:
        return None
    return (
        f"Diff is {line_count} lines (limit {MAX_DIFF_LINES}). "
        "Split this into smaller commits so each one can be fully reviewed."
    )


# ---------------------------------------------------------------------------
# Prompt building — single-call path
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    """Assemble system prompt from review_prompt.md files (single-call mode)."""
    parts = []
    global_prompt = read_file(GLOBAL_PROMPT)
    if not global_prompt:
        return ""
    parts.append(global_prompt)

    project_rules = read_file(PROJECT_PROMPT)
    if project_rules:
        parts.append(f"## Project-specific review rules:\n{project_rules}")

    return "\n\n---\n\n".join(parts)


def build_user_prompt(diff: str, files: str, is_merge: bool) -> str:
    """Build the user prompt with diff and file list."""
    parts = []

    if is_merge:
        parts.append(
            "**MERGE CONFLICT RESOLUTION**: This is a merge commit. "
            "The individual commits were already reviewed in the feature branch. "
            "Focus ONLY on how conflicts were resolved — look for incorrect "
            "resolution, lost changes, or logic errors introduced during merge."
        )

    parts.append(f"## Changed files:\n{files}")
    parts.append(f"## Diff to review:\n```diff\n{diff}\n```")
    parts.append(
        "## Your task:\n"
        "Produce the three-section inventory exactly as described in the "
        "system prompt (file audit, seven area sweeps / lens findings, "
        "summary line).\n\n"
        "Do NOT output `OK` or `BLOCK`. The calling hook reads `[CRITICAL]` "
        "tags from your findings and decides the verdict mechanically — "
        "your job is the complete inventory, not the decision.\n\n"
        "Use your tools: `Read` changed files for full context, `Grep` for "
        "duplicates and call sites, `Glob` for test files."
    )

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Prompt building — fan-out path
# ---------------------------------------------------------------------------

def build_lens_system_prompt(lens_name: str) -> str:
    """Assemble lens prompt = common preamble + lens-specific body."""
    common = read_file(LENS_DIR / "lens_common.md")
    specific = read_file(LENS_DIR / f"lens_{lens_name}.md")
    if not common or not specific:
        return ""
    return f"{common}\n\n---\n\n{specific}"


# ---------------------------------------------------------------------------
# Review runners
# ---------------------------------------------------------------------------

def _parse_opencode_json(raw: str) -> str:
    """Extract text content from opencode --format json output."""
    import json

    parts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "text":
            text = event.get("part", {}).get("text", "")
            if text:
                parts.append(text)
    return "\n".join(parts) if parts else ""


def run_opencode(
    system_prompt: str,
    user_prompt: str,
    timeout: int = TIMEOUT_SECONDS,
) -> tuple[str, str, int]:
    """Run OpenCode CLI (--pure, no plugins). Returns (stdout, stderr, rc)."""
    full_prompt = f"{system_prompt}\n\n{user_prompt}" if system_prompt else user_prompt

    cmd = [
        "opencode",
        "run",
        "--pure",
        "--model", "github-copilot/gpt-5.4",
        "--format", "json",
        full_prompt,
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    review = _parse_opencode_json(result.stdout) if result.stdout else ""
    return review, result.stderr.strip(), result.returncode


def run_claude(
    system_prompt: str,
    user_prompt: str,
    model: str = "sonnet",
    timeout: int = TIMEOUT_SECONDS,
) -> tuple[str, str, int]:
    """Run Claude Code CLI. Returns (stdout, stderr, rc)."""
    cmd = [
        "claude",
        "-p",
        "--model", model,
        "--no-session-persistence",
        "--tools", "Read,Grep,Glob",
        "--output-format", "text",
    ]

    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    result = subprocess.run(
        cmd,
        input=user_prompt,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    return result.stdout.strip(), result.stderr.strip(), result.returncode


# ---------------------------------------------------------------------------
# Parsing — reviewer output
# ---------------------------------------------------------------------------

_CRITICAL_LINE_RE = re.compile(
    r"^[ \t]*[-*•]?[ \t]*(?:\[F\d+\]\s*)?\[CRITICAL\]",
    re.MULTILINE | re.IGNORECASE,
)
_WARNING_TAG_RE = re.compile(r"\[WARNING\]", re.IGNORECASE)
_SUMMARY_LINE_RE = re.compile(r"^[ \t]*Summary:\s", re.IGNORECASE)
_SECTION_1_MARKER = re.compile(r"(?im)^#{1,6}\s*Section\s*1\b")
_SECTION_2_MARKER = re.compile(r"(?im)^#{1,6}\s*Section\s*2\b")


def count_criticals(review: str) -> int:
    """Count [CRITICAL] finding lines in reviewer output.

    Matches only at line start (optional bullet + optional `[Fn]` id
    tag). Mid-line mentions in prose or diff quotes do not count.
    """
    if not review:
        return 0
    return len(_CRITICAL_LINE_RE.findall(review))


def is_well_formed(review: str) -> bool:
    """True if a single-call review ran to completion in the documented format.

    The contract (review_prompt.md) requires Section 1, Section 2, and
    the ``Summary:`` terminator as the final non-empty line. This
    check is used ONLY for the single-call path — the fan-out path
    synthesises its own aggregate summary.
    """
    if not review:
        return False
    if not _SECTION_1_MARKER.search(review):
        return False
    if not _SECTION_2_MARKER.search(review):
        return False
    lines = [line for line in review.splitlines() if line.strip()]
    if not lines:
        return False
    return _SUMMARY_LINE_RE.match(lines[-1]) is not None


def parse_verdict(review: str) -> str:
    """Single-call verdict: empty/malformed -> BLOCK; any [CRITICAL] -> BLOCK."""
    if not review or not review.strip():
        return "BLOCK"
    if not is_well_formed(review):
        return "BLOCK"
    return "BLOCK" if count_criticals(review) > 0 else "OK"


# ---------------------------------------------------------------------------
# Fan-out — lens router
# ---------------------------------------------------------------------------

def _iter_files(files: str) -> list[str]:
    """Split the staged-files string into non-empty path entries."""
    return [f.strip() for f in files.split("\n") if f.strip()]


def _any_file_matches(files: str, exts: frozenset[str]) -> bool:
    """True if any changed file has an extension in ``exts``."""
    for f in _iter_files(files):
        for ext in exts:
            if f.endswith(ext):
                return True
    return False


# Map lens → predicate that answers "is there anything in this diff this
# lens could plausibly flag?". Security and Duplication always run — both
# can find defects in prose (secrets, architectural duplication in spec
# files). The remaining five require executable code.
#
# Safety-first rule: when in doubt, run the lens. Missing a real finding
# is worse than wasting one parallel LLM call.
LENS_APPLICABILITY: dict[str, callable] = {
    "security":    lambda files: True,
    "duplication": lambda files: True,
    "tests":       lambda files: _any_file_matches(files, CODE_EXTS),
    "types":       lambda files: _any_file_matches(files, PY_EXTS),
    "performance": lambda files: _any_file_matches(files, CODE_EXTS),
    "rootcause":   lambda files: _any_file_matches(files, CODE_EXTS),
    "complexity":  lambda files: _any_file_matches(files, CODE_EXTS),
}


def applicable_lenses(files: str) -> list[str]:
    """Return lenses worth running for this file set, in LENS_NAMES order."""
    return [name for name in LENS_NAMES if LENS_APPLICABILITY[name](files)]


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------

def run_single_lens(
    lens_name: str,
    diff: str,
    files: str,
    is_merge: bool,
) -> dict:
    """Run one lens. Returns dict with name/status/review/error/reviewer."""
    system_prompt = build_lens_system_prompt(lens_name)
    if not system_prompt:
        return {
            "name": lens_name,
            "status": "error",
            "review": "",
            "error": f"lens_{lens_name}.md or lens_common.md missing",
            "reviewer": None,
        }

    user_prompt = build_user_prompt(diff, files, is_merge)

    review: str = ""
    stderr: str = ""
    rc: int = -1
    reviewer: str = "opencode"

    try:
        review, stderr, rc = run_opencode(system_prompt, user_prompt)
    except subprocess.TimeoutExpired:
        return {"name": lens_name, "status": "timeout",
                "review": "", "error": "opencode timeout", "reviewer": "opencode"}
    except (FileNotFoundError, OSError) as exc:
        stderr = str(exc)
        rc = -1

    if rc != 0 or not review or not review.strip():
        # fallback to Claude Code
        try:
            review, stderr, rc = run_claude(system_prompt, user_prompt)
            reviewer = "claude"
        except subprocess.TimeoutExpired:
            return {"name": lens_name, "status": "timeout",
                    "review": "", "error": "claude fallback timeout",
                    "reviewer": "claude"}
        except (FileNotFoundError, OSError) as exc:
            return {"name": lens_name, "status": "error",
                    "review": "", "error": f"both runners unavailable: {exc}",
                    "reviewer": None}
        if rc != 0 or not review or not review.strip():
            return {"name": lens_name, "status": "error",
                    "review": review, "error": f"rc={rc} stderr={stderr}",
                    "reviewer": reviewer}

    return {"name": lens_name, "status": "ok", "review": review,
            "error": "", "reviewer": reviewer}


def _aggregate_lens_outputs(per_lens: list[dict]) -> str:
    """Concatenate lens outputs. Appends a global Summary line."""
    parts: list[str] = []
    for d in per_lens:
        header = f"## Lens: {d['name']}"
        if d["status"] == "ok":
            parts.append(f"{header}\n\n{d['review']}")
        elif d["status"] == "skipped_by_router":
            parts.append(f"{header}\n\n_Skipped by router: {d['error']}_")
        else:
            parts.append(f"{header}\n\n_Lens unavailable: {d['error']}_")
    total_c = sum(count_criticals(d.get("review", "")) for d in per_lens if d["status"] == "ok")
    total_w = sum(
        len(_WARNING_TAG_RE.findall(d.get("review", "")))
        for d in per_lens
        if d["status"] == "ok"
    )
    parts.append(f"Summary: {total_c} CRITICAL, {total_w} WARNING across {len(per_lens)} lenses.")
    return "\n\n".join(parts)


def run_fanout(diff: str, files: str, is_merge: bool) -> tuple[str, list[dict]]:
    """Run applicable lenses in parallel. Returns (aggregated_text, per_lens).

    The lens router (``LENS_APPLICABILITY``) skips lenses that have
    nothing in the diff to look at (e.g. Types for non-Python diffs).
    Skipped lenses are recorded in ``per_lens`` with status
    ``skipped_by_router`` so the log shows exactly which lenses ran and
    which were pruned.
    """
    applicable = applicable_lenses(files)
    skipped = [n for n in LENS_NAMES if n not in applicable]

    per_lens: list[dict] = [
        {
            "name": name,
            "status": "skipped_by_router",
            "review": "",
            "reviewer": None,
            "error": "no applicable files for this lens",
        }
        for name in skipped
    ]

    if skipped:
        info(
            f"Fan-out: skipping {len(skipped)} lens(es) "
            f"({', '.join(skipped)}) — no applicable files."
        )
    info(f"Fan-out: launching {len(applicable)} lens review(s) in parallel...")

    if applicable:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(applicable)) as ex:
            futures = {
                ex.submit(run_single_lens, name, diff, files, is_merge): name
                for name in applicable
            }
            for fut in concurrent.futures.as_completed(futures):
                name = futures[fut]
                try:
                    per_lens.append(fut.result())
                except Exception as exc:
                    per_lens.append({
                        "name": name,
                        "status": "error",
                        "review": "",
                        "error": f"{type(exc).__name__}: {exc}",
                        "reviewer": None,
                    })

    per_lens.sort(key=lambda d: LENS_NAMES.index(d["name"]))
    ok_count = sum(1 for d in per_lens if d["status"] == "ok")
    info(
        f"Fan-out complete: {ok_count}/{len(applicable)} lens(es) returned "
        f"findings ({len(skipped)} skipped by router)."
    )

    aggregated = _aggregate_lens_outputs(per_lens)
    return aggregated, per_lens


# ---------------------------------------------------------------------------
# Arbiter
# ---------------------------------------------------------------------------

_ARBITER_VERDICT_RE = re.compile(
    r"^\s*\[(UPHELD|OVERTURN)\]\s*(F\d+)",
    re.MULTILINE | re.IGNORECASE,
)
_ARBITER_SUMMARY_RE = re.compile(
    r"^\s*Summary:\s*\d+\s+UPHELD",
    re.MULTILINE | re.IGNORECASE,
)
_FINDING_ID_INJECT_RE = re.compile(
    r"^([ \t]*[-*•]?[ \t]*)(\[CRITICAL\])",
    re.MULTILINE | re.IGNORECASE,
)


def assign_finding_ids(review_text: str) -> tuple[str, list[dict]]:
    """Inject stable IDs (F1, F2, ...) into every [CRITICAL] line.

    Returns (tagged_text, findings). findings: list of
    ``{"id": "F1", "line": "<full finding line, stripped>"}``.
    """
    counter = [0]

    def _replace(m: re.Match) -> str:
        counter[0] += 1
        return f"{m.group(1)}[F{counter[0]}] {m.group(2)}"

    tagged = _FINDING_ID_INJECT_RE.sub(_replace, review_text)

    findings: list[dict] = []
    line_re = re.compile(
        r"^[ \t]*[-*•]?[ \t]*\[(F\d+)\]\s*\[CRITICAL\].*$",
        re.IGNORECASE,
    )
    for line in tagged.splitlines():
        m = line_re.match(line)
        if m:
            findings.append({"id": m.group(1), "line": line.strip()})
    return tagged, findings


def parse_arbiter_verdict(arbiter_raw: str, all_finding_ids: list[str]) -> set[str]:
    """Extract UPHELD finding IDs from arbiter output.

    Fail-open: if the arbiter output is malformed (no Summary line),
    every finding is treated as UPHELD. The arbiter exists to *reduce*
    the blocking set; a parser bug must never expand it by silently
    dropping valid findings.
    """
    if not arbiter_raw or not _ARBITER_SUMMARY_RE.search(arbiter_raw):
        return set(all_finding_ids)

    seen: dict[str, str] = {}
    for m in _ARBITER_VERDICT_RE.finditer(arbiter_raw):
        verdict = m.group(1).upper()
        fid = m.group(2)
        seen[fid] = verdict

    upheld: set[str] = set()
    for fid in all_finding_ids:
        # Missing verdict line → fail-open (count as UPHELD).
        if seen.get(fid, "UPHELD") == "UPHELD":
            upheld.add(fid)
    return upheld


def run_arbiter(
    diff: str,
    findings: list[dict],
) -> dict:
    """Run the Claude Opus arbiter over findings.

    Returns {status, upheld_ids, raw, error}. Fail-open on any error:
    upheld_ids will equal the full set of input finding IDs.
    """
    all_ids = [f["id"] for f in findings]
    if not findings:
        return {"status": "skipped", "upheld_ids": set(),
                "raw": "", "error": "no criticals to arbitrate"}

    system_prompt = read_file(ARBITER_PROMPT_PATH)
    if not system_prompt:
        warn(f"Arbiter prompt {ARBITER_PROMPT_PATH} missing — upholding all findings")
        return {"status": "unavailable", "upheld_ids": set(all_ids),
                "raw": "", "error": f"{ARBITER_PROMPT_PATH} not found"}

    user_prompt = (
        "## Full staged diff\n\n"
        "```diff\n" + diff + "\n```\n\n"
        "## Findings to arbitrate (in order):\n\n"
        + "\n".join(f["line"] for f in findings)
        + "\n\n"
        "Output one `[UPHELD]` or `[OVERTURN]` line per finding above, "
        "in the same order, then the `Summary:` line. No other content."
    )

    info(
        f"Arbiter: analyzing {len(findings)} finding(s) with Claude "
        f"{ARBITER_MODEL} (may take 30-120s)..."
    )
    try:
        raw, stderr, rc = run_claude(
            system_prompt, user_prompt,
            model=ARBITER_MODEL, timeout=ARBITER_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        warn(f"Arbiter timed out after {ARBITER_TIMEOUT_SECONDS}s — upholding all findings")
        return {"status": "unavailable", "upheld_ids": set(all_ids),
                "raw": "", "error": "timeout"}
    except (FileNotFoundError, OSError) as exc:
        warn(f"Arbiter unreachable ({exc}) — upholding all findings")
        return {"status": "unavailable", "upheld_ids": set(all_ids),
                "raw": "", "error": f"unreachable: {exc}"}

    if rc != 0 or not raw or not raw.strip():
        warn(f"Arbiter failed (rc={rc}) — upholding all findings")
        return {"status": "unavailable", "upheld_ids": set(all_ids),
                "raw": raw, "error": f"rc={rc} stderr={stderr}"}

    upheld = parse_arbiter_verdict(raw, all_ids)
    overturned = len(all_ids) - len(upheld)
    info(f"Arbiter: {len(upheld)} UPHELD, {overturned} OVERTURN.")
    return {"status": "ok", "upheld_ids": upheld, "raw": raw, "error": ""}


def _render_with_arbiter(
    findings: list[dict],
    upheld_ids: set[str],
    arbiter: dict,
    warning_count: int,
    denominator_label: str,
    unavailable_label: str = "",
) -> str:
    """Developer-facing summary for any review path that produced findings.

    ``denominator_label`` describes the producer side ("7 lenses",
    "1 reviewer"). ``unavailable_label`` is an optional comma-separated
    list of failed/skipped producers (used by fan-out only).
    """
    upheld = [f for f in findings if f["id"] in upheld_ids]
    overturned = [f for f in findings if f["id"] not in upheld_ids]

    sections: list[str] = ["## Review summary\n"]
    if upheld:
        sections.append("### Upheld findings (blocking)")
        sections.extend(f"- {f['line']}" for f in upheld)
    else:
        sections.append("### Upheld findings (blocking)\n_(none)_")

    if overturned:
        rationales: dict[str, str] = {}
        for m in re.finditer(
            r"^\s*\[(?:UPHELD|OVERTURN)\]\s*(F\d+)\s*[—-]?\s*(.*)$",
            arbiter.get("raw", ""),
            re.MULTILINE | re.IGNORECASE,
        ):
            rationales[m.group(1)] = m.group(2).strip()
        sections.append("\n### Overturned findings (advisory — not blocking)")
        for f in overturned:
            reason = rationales.get(f["id"], "")
            sections.append(f"- {f['line']}")
            if reason:
                sections.append(f"    arbiter: {reason}")

    if warning_count:
        sections.append(f"\n### Warnings: {warning_count} (see log for detail)")
    if unavailable_label:
        sections.append(f"\n### Producers unavailable: {unavailable_label}")

    sections.append(
        f"\nSummary: {len(upheld)} UPHELD, {len(overturned)} OVERTURN, "
        f"{warning_count} WARNING across {denominator_label}."
    )
    return "\n".join(sections)


def _render_fanout_output(
    per_lens: list[dict],
    findings: list[dict],
    upheld_ids: set[str],
    arbiter: dict,
) -> str:
    """Compact summary for fan-out path. Wraps _render_with_arbiter."""
    warning_count = sum(
        len(_WARNING_TAG_RE.findall(d.get("review", "")))
        for d in per_lens if d["status"] == "ok"
    )
    unavailable = [d["name"] for d in per_lens if d["status"] != "ok"]
    return _render_with_arbiter(
        findings, upheld_ids, arbiter,
        warning_count=warning_count,
        denominator_label=f"{len(per_lens)} lenses",
        unavailable_label=", ".join(unavailable),
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _format_per_lens(per_lens: list[dict]) -> str:
    parts: list[str] = ["## Per-lens detail\n"]
    for d in per_lens:
        reviewer_suffix = f" ({d['reviewer']})" if d.get("reviewer") else ""
        parts.append(f"### Lens: {d['name']} — {d['status']}{reviewer_suffix}\n")
        if d.get("error"):
            parts.append(f"_Error:_ {d['error']}\n")
        if d.get("review"):
            parts.append(f"```\n{d['review']}\n```\n")
    return "\n".join(parts) + "\n"


def _format_arbiter(arbiter: dict) -> str:
    parts: list[str] = [
        f"## Arbiter ({ARBITER_MODEL}) — status: {arbiter.get('status', 'n/a')}\n"
    ]
    if arbiter.get("error"):
        parts.append(f"_Error:_ {arbiter['error']}\n")
    upheld = arbiter.get("upheld_ids") or set()
    if upheld:
        parts.append(f"_Upheld IDs:_ {', '.join(sorted(upheld))}\n")
    if arbiter.get("raw"):
        parts.append(f"```\n{arbiter['raw']}\n```\n")
    return "\n".join(parts) + "\n"


def save_log(
    verdict: str,
    files: str = "",
    diff: str = "",
    review: str = "",
    error_msg: str | None = None,
    diag: str | None = None,
    reviewer: str | None = None,
    per_lens: list[dict] | None = None,
    arbiter: dict | None = None,
) -> None:
    """Save review to a log file for debugging."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        project = Path.cwd().name
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_path = LOG_DIR / f"{timestamp}_{project}_{verdict}.md"

        sections: list[str] = [
            f"# Review: {project} @ {timestamp}\n",
            f"**Verdict:** {verdict}",
        ]
        if reviewer:
            sections.append(f"**Reviewer:** {reviewer}")
        if files:
            sections.append(f"**Files:**\n{files}\n")
        if diff:
            sections.append(f"## Diff stats\n{len(diff.splitlines())} lines in diff\n")
        if error_msg:
            sections.append(f"## Error\n```\n{error_msg}\n```\n")
        if diag:
            sections.append(f"## Diagnostics\n```\n{diag}\n```\n")
        if review:
            sections.append(f"## Review output\n```\n{review}\n```\n")
        if per_lens:
            sections.append(_format_per_lens(per_lens))
        if arbiter:
            sections.append(_format_arbiter(arbiter))
        if diff:
            sections.append(f"## Full diff\n```diff\n{diff}\n```")

        log_path.write_text("\n".join(sections) + "\n", encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def collect_diff() -> tuple[str, str, bool] | None:
    """Collect staged diff and metadata. Returns (diff, files, is_merge) or None."""
    diff, git_error = get_staged_diff()

    if git_error:
        warn(f"git diff failed: {git_error}")
        save_log("SKIP", error_msg=git_error)
        return None

    if not diff:
        status = get_git_status()
        diag = f"git diff --cached returned empty.\ngit status:\n{status}"
        warn("No staged changes to review.")
        save_log("SKIP", diag=diag)
        return None

    changed = count_changed_lines(diff)
    if changed < MIN_LINES_TO_REVIEW:
        save_log("SKIP", diag=f"only {changed} changed lines (min {MIN_LINES_TO_REVIEW})")
        return None

    too_big = check_diff_size(diff)
    if too_big:
        error(too_big)
        save_log("TOO_BIG", diff=diff, error_msg=too_big)
        sys.exit(1)

    files = get_staged_files()
    is_merge = Path(".git/MERGE_HEAD").is_file()
    return diff, files, is_merge


def _call_single_reviewer(
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, str, str, int]:
    """Try OpenCode first, fall back to Claude Code. (Single-call path only.)"""
    reviewer: str | None = None
    review: str = ""
    reviewer_stderr: str = ""
    returncode: int = -1

    try:
        review, reviewer_stderr, returncode = run_opencode(system_prompt, user_prompt)
        reviewer = "opencode"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        warn(f"OpenCode unavailable ({exc}), falling back to Claude Code")

    if reviewer is None or returncode != 0 or not review:
        if reviewer == "opencode":
            warn(f"OpenCode failed (rc={returncode}), falling back to Claude Code")
        review, reviewer_stderr, returncode = run_claude(system_prompt, user_prompt)
        reviewer = "claude"

    return review, reviewer_stderr, reviewer, returncode


def _run_single_call(
    diff: str,
    files: str,
    is_merge: bool,
) -> tuple[str | None, str]:
    """Legacy single-call reviewer path for small diffs."""
    system_prompt = build_system_prompt()
    if not system_prompt:
        warn("No review_prompt.md found, skipping review")
        save_log("SKIP", files=files, diff=diff, error_msg="no review_prompt.md")
        return None, "SKIP"

    user_prompt = build_user_prompt(diff, files, is_merge)

    try:
        review, reviewer_stderr, reviewer, returncode = _call_single_reviewer(
            system_prompt, user_prompt,
        )
    except subprocess.TimeoutExpired:
        warn(f"Review timed out after {TIMEOUT_SECONDS}s — allowing commit")
        save_log("TIMEOUT", files=files, diff=diff, error_msg="timed out")
        return None, "TIMEOUT"
    except FileNotFoundError:
        warn("Both reviewers unavailable — allowing commit")
        save_log("SKIP", files=files, diff=diff, error_msg="no reviewer available")
        return None, "SKIP"

    if returncode != 0:
        detail = f"{reviewer} exited with code {returncode}\nstderr: {reviewer_stderr}\nstdout: {review}"
        warn(f"{reviewer} failed (rc={returncode}) — allowing commit")
        save_log("ERROR", files=files, diff=diff, error_msg=detail)
        return None, "ERROR"

    if not review or not review.strip():
        warn(f"{reviewer} returned empty output — allowing commit")
        save_log("EMPTY", files=files, diff=diff, reviewer=reviewer,
                 error_msg=f"empty output. stderr: {reviewer_stderr}")
        return None, "EMPTY"

    return _arbitrate_single_call_review(review, reviewer, diff, files)


def _arbitrate_single_call_review(
    review: str,
    reviewer: str,
    diff: str,
    files: str,
) -> tuple[str | None, str]:
    """Apply well-formed/critical/arbiter logic to a single-call review.

    Mirrors the fan-out arbiter step so small diffs also benefit from
    Opus calibration when the primary reviewer flags CRITICALs.
    """
    if not is_well_formed(review):
        warn("Reviewer output missing `Summary:` terminator — treating as malformed (fail-closed BLOCK)")
        info(f"Reviewer: {reviewer} — malformed output")
        save_log("BLOCK", files=files, diff=diff, review=review, reviewer=reviewer)
        return review, "BLOCK"

    critical_count = count_criticals(review)
    info(f"Reviewer: {reviewer} — {critical_count} CRITICAL finding(s)")

    if critical_count == 0:
        save_log("OK", files=files, diff=diff, review=review, reviewer=reviewer)
        return review, "OK"

    tagged_review, findings = assign_finding_ids(review)
    arbiter = run_arbiter(diff, findings)
    upheld_ids = arbiter["upheld_ids"]
    warning_count = len(_WARNING_TAG_RE.findall(review))
    display = _render_with_arbiter(
        findings, upheld_ids, arbiter,
        warning_count=warning_count,
        denominator_label=f"1 reviewer ({reviewer})",
    )
    verdict = "BLOCK" if upheld_ids else "OK"
    save_log(verdict, files=files, diff=diff, review=display,
             reviewer=f"{reviewer}+arbiter", arbiter=arbiter,
             diag=f"original review (pre-arbiter):\n{tagged_review}")
    # On BLOCK return the synthesized display so the developer sees the
    # arbiter context. On OK return the raw review so main() can surface
    # [WARNING] lines via _WARNING_TAG_RE — the synthesized display only
    # carries a warning count, not the lines themselves.
    return (display if verdict == "BLOCK" else review), verdict


def _run_fanout_with_arbiter(
    diff: str,
    files: str,
    is_merge: bool,
) -> tuple[str | None, str]:
    """Fan-out reviewer → aggregator → arbiter pipeline for large diffs."""
    aggregated, per_lens = run_fanout(diff, files, is_merge)

    ok_lenses = [d for d in per_lens if d["status"] == "ok"]
    if not ok_lenses:
        warn("All fan-out lenses failed — allowing commit")
        save_log("EMPTY", files=files, diff=diff,
                 reviewer="fan-out", per_lens=per_lens,
                 error_msg="all lenses unavailable")
        return None, "EMPTY"

    tagged_aggregated, findings = assign_finding_ids(aggregated)

    if not findings:
        display = tagged_aggregated
        save_log("OK", files=files, diff=diff, review=display,
                 reviewer="fan-out", per_lens=per_lens)
        return display, "OK"

    arbiter = run_arbiter(diff, findings)
    upheld_ids = arbiter["upheld_ids"]
    display = _render_fanout_output(per_lens, findings, upheld_ids, arbiter)
    verdict = "BLOCK" if upheld_ids else "OK"

    save_log(verdict, files=files, diff=diff, review=display,
             reviewer="fan-out+arbiter",
             per_lens=per_lens, arbiter=arbiter)
    return display, verdict


def run_review(diff: str, files: str, is_merge: bool) -> tuple[str | None, str]:
    """Execute the review. Routes between single-call and fan-out+arbiter."""
    added = count_added_lines(diff)
    use_fanout = added >= FANOUT_THRESHOLD
    info(
        f"Reviewing {len(files.splitlines())} file(s), +{added} added line(s), "
        f"mode={'fan-out+arbiter' if use_fanout else 'single-call'}"
    )

    if use_fanout:
        return _run_fanout_with_arbiter(diff, files, is_merge)
    return _run_single_call(diff, files, is_merge)


def main() -> None:
    try:
        context = collect_diff()
        if not context:
            sys.exit(0)

        diff, files, is_merge = context
        review, verdict = run_review(diff, files, is_merge)

        if review is None:
            sys.exit(0)

        if verdict == "BLOCK":
            error(f"Review BLOCKED this commit:\n\n{review}")
            info(
                "Trade-off channel: if a finding above is a deliberate "
                "trade-off, document it inline via "
                "`# review-note: <specific reason>` on the relevant line "
                "(commit messages are not visible to the reviewer in "
                "this hook stage) — then re-commit. The reviewer honors "
                "specific, named-invariant explanations.\n"
                "Use sparingly: vague notes (\"intentional\", \"by design\") "
                "or 3+ in one commit are themselves flagged as CRITICAL. "
                "This is not a hook-skip substitute."
            )
            sys.exit(1)

        if _WARNING_TAG_RE.search(review):
            warn(f"Review notes (non-blocking warnings):\n{review}")
        sys.exit(0)

    except Exception as exc:
        # Never let a bug in this script block a commit
        warn(f"Review script crashed: {exc} — allowing commit")
        save_log("CRASH", error_msg=f"{type(exc).__name__}: {exc}")
        sys.exit(0)


if __name__ == "__main__":
    main()
