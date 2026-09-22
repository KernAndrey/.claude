"""Scaffold ``.review/manifest.yaml`` for the staged diff.

This is a **scaffolder**, not an auto-classifier. The writer decides how to
split the diff into chunks; the script just removes boilerplate:

* Computes ``diff_hash`` so the validator's freshness check passes.
* Lists every staged file (with status + line count) as a YAML comment
  so the writer sees the full coverage set at a glance.
* Lays out N empty chunk templates (default 3, override with
  ``--chunks N``) with inline comments explaining lenses, line_ranges,
  and the validator's constraints.

Auto-classification was deliberately removed — heuristic layer-routing
ships bad chunks for any project that doesn't match the heuristic, and a
mediocre split is worse than no split. The writer knows their own
architecture; the scaffolder hands them a clean canvas.

Usage::

    python3 -B ~/.claude/review/scripts/scaffold_manifest.py
    python3 -B ~/.claude/review/scripts/scaffold_manifest.py --chunks 5
    python3 -B ~/.claude/review/scripts/scaffold_manifest.py --print
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_NAME_STATUS_RENAME_RE = re.compile(r"^[RC](\d{1,3})$")


@dataclass(frozen=True)
class StagedFile:
    """Display row for the comment header — what's in the diff."""

    status: str  # A / M / D / R / C / T
    path: str
    original_path: str | None
    added: int
    removed: int
    is_binary: bool


def _git(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True)


def _parse_name_status(text: str) -> list[tuple[str, str, str | None]]:
    out: list[tuple[str, str, str | None]] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status_raw = parts[0]
        if _NAME_STATUS_RENAME_RE.match(status_raw):
            if len(parts) < 3:
                continue
            out.append((status_raw[0], parts[2], parts[1]))
        else:
            out.append((status_raw[0] if status_raw else "M", parts[1], None))
    return out


def _extract_new_path(path_field: str) -> str:
    """Normalise git rename numstat path fields to the new path.

    Brace form ``prefix/{old => new}suffix`` → ``prefix/newsuffix``;
    simple ``old => new`` (or ``-> new``) → ``new``. Mirrors the helper
    in ``validators/manifest.py``.
    """
    if "{" in path_field and "}" in path_field:
        brace_open = path_field.index("{")
        brace_close = path_field.index("}")
        prefix = path_field[:brace_open]
        suffix = path_field[brace_close + 1 :]
        inside = path_field[brace_open + 1 : brace_close]
        if " => " in inside:
            return prefix + inside.split(" => ", 1)[1] + suffix
        if " -> " in inside:
            return prefix + inside.split(" -> ", 1)[1] + suffix
    if " => " in path_field:
        return path_field.split(" => ", 1)[1]
    if " -> " in path_field:
        return path_field.split(" -> ", 1)[1]
    return path_field


def _parse_numstat(text: str) -> dict[str, tuple[int, int, bool]]:
    out: dict[str, tuple[int, int, bool]] = {}
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added_s, removed_s = parts[0], parts[1]
        path_field = _extract_new_path(parts[2])
        is_binary = added_s == "-" and removed_s == "-"
        out[path_field] = (
            0 if is_binary else int(added_s),
            0 if is_binary else int(removed_s),
            is_binary,
        )
    return out


def _collect_staged_files() -> list[StagedFile]:
    name_status = _parse_name_status(_git(["diff", "--cached", "--name-status", "-M"]))
    numstat = _parse_numstat(_git(["diff", "--cached", "--numstat"]))
    out: list[StagedFile] = []
    for status, path, original in name_status:
        added, removed, is_bin = numstat.get(path, (0, 0, False))
        out.append(
            StagedFile(
                status=status,
                path=path,
                original_path=original,
                added=added,
                removed=removed,
                is_binary=is_bin,
            )
        )
    return out


# ---------------------------------------------------------------------------
# YAML scaffolding (string-formatted to preserve comments)
# ---------------------------------------------------------------------------


_HEADER = """\
# .review/manifest.yaml — chunked-review manifest for this commit
#
# Caps (config.py):
#   - max {max_chunks} chunks total (MAX_CHUNKS)
#   - each chunk: max {max_prod} added prod-code lines (MAX_PROD_LINES)
#   - tests, docs (.md/.rst), config (.yaml/.json/.toml), lock files,
#     and non-code data files do NOT count toward the per-chunk prod-line cap
#   - shell scripts (.sh, .bash) DO count as production code
#
# Chunking guidance:
#   * Group by meaning, not file type. One chunk per architectural slice
#     is usually right (e.g. data layer, security, UI).
#   * Per-chunk reviewer sees only the files in its `files:` list (line
#     ranges optional). A separate whole-diff lens layer covers
#     cross-cutting concerns (bugs/architecture/tests on the full diff).
#     Put what the arbiter must verify across chunks into
#     `cross_chunk_invariants:` below.
#   * chunk `id` must match `[a-z][a-z0-9_]*` (lowercase, underscores).
#   * `line_ranges` accepts:
#       [[start, end], ...]  — inclusive, 1-based, post-image line numbers
#       all                  — entire file (new files, full rewrites)
#       []                   — deleted file or binary file
#   * Renames: add `original_path: <old_path>` next to `path:`.
#
# Coverage rule (validator):
#   Every changed line in every staged file must be claimed by exactly
#   one chunk. The validator will tell you precisely what's missing or
#   overlapping.
"""


def _build_footer(repo_root: Path) -> str:
    if (repo_root / "CLAUDE.md").exists():
        related_files_block = """\
default_related_files:
  # Files every reviewer reads as background context. Keep short — these
  # are added to the cached prompt block, so they cost tokens once per
  # run regardless of how many reviewers spawn. Common picks:
  #   - CLAUDE.md
  #   - the project's top-level architecture doc
  - CLAUDE.md
"""
    else:
        related_files_block = "default_related_files: []"
    return (
        related_files_block
        + """

cross_chunk_invariants:
  # Free-form claims the arbiter checks against the FULL diff (it sees
  # everything; per-chunk reviewers don't). One per line. Examples:
  #   - "every new model field has an ACL row in the security chunk"
  #   - "no compute-without-store field is referenced in search_count"
  #   - "all xpath references in views resolve to fields defined here"
"""
    )


def _file_comment(f: StagedFile) -> str:
    if f.is_binary:
        sizing = "binary"
    elif f.status == "D":
        sizing = f"deleted (-{f.removed})"
    elif f.status == "A":
        sizing = f"new, +{f.added} lines"
    elif f.status in ("R", "C") and f.original_path:
        sizing = f"{'renamed' if f.status == 'R' else 'copied'} from {f.original_path} (+{f.added} -{f.removed})"
    else:
        sizing = f"+{f.added} -{f.removed}"
    return f"#   - {f.path}  ({sizing})"


def _build_chunk_template(idx: int) -> str:
    return (
        f"  - id: chunk_{idx}\n"
        f'    rationale: "TODO: one sentence — what changes this chunk groups"\n'
        f"    files:\n"
        f"      # Move file paths into here from the staged-files list above.\n"
        f"      # Split a big file by listing multiple [start, end] ranges, OR\n"
        f"      # use the same path in multiple chunks with disjoint ranges.\n"
        f"      # Examples:\n"
        f"      #   - path: src/models/user.py\n"
        f"      #     line_ranges: [[1, 120]]\n"
        f"      #   - path: src/services/auth.py\n"
        f"      #     line_ranges: all\n"
        f"      #   - path: src/old.py\n"
        f"      #     line_ranges: []\n"
        f"      []  # <- replace this empty list with file entries\n"
        f"    related_files: []  # optional: files this chunk's reviewer reads\n"
    )


def build_scaffold(
    num_chunks: int,
    max_chunks: int,
    max_prod: int,
    repo_root: Path | None = None,
) -> str:
    """Compose the YAML scaffold text. ``num_chunks`` is the count of empty
    chunk templates to emit; the writer adds/removes as needed."""
    # Deferred: review/ is only on sys.path once main() has inserted it (for
    # direct `python3 scripts/scaffold_manifest.py` runs); importing at module
    # top would break that path. Single source of truth for the hash format.
    from approvals import diff_hash

    repo_root = Path.cwd() if repo_root is None else repo_root
    diff_text = _git(["diff", "--cached"]).strip()
    if not diff_text:
        raise SystemExit("no staged changes — nothing to manifest")
    staged_hash = diff_hash(diff_text)
    files = _collect_staged_files()

    parts: list[str] = []
    parts.append(_HEADER.format(max_chunks=max_chunks, max_prod=max_prod))
    parts.append("")
    parts.append("version: 1")
    parts.append(f"diff_hash: {staged_hash}")
    parts.append("")
    parts.append("# Staged files (every file must appear in exactly one chunk's `files:`):")
    for f in files:
        parts.append(_file_comment(f))
    parts.append("")
    parts.append(_build_footer(repo_root))
    parts.append("chunks:")
    for i in range(1, num_chunks + 1):
        parts.append(_build_chunk_template(i))
    return "\n".join(parts)


def write_scaffold(text: str, repo_root: Path | None = None) -> Path:
    repo_root = Path.cwd() if repo_root is None else repo_root
    out_dir = repo_root / ".review"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "manifest.yaml"
    out_path.write_text(text, encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scaffold .review/manifest.yaml for the staged diff. "
        "The writer fills in the chunks; this script does NOT auto-classify."
    )
    parser.add_argument("--chunks", type=int, default=3, help="number of empty chunk templates to emit (default: 3)")
    parser.add_argument("--repo-root", type=Path, default=None, help="repo root (default: cwd)")
    parser.add_argument("--print", action="store_true", help="write to stdout instead of .review/manifest.yaml")
    args = parser.parse_args(argv)

    # Late imports so --print and --help work without a full review/ env loaded.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import config

    if args.chunks < 1 or args.chunks > config.MAX_CHUNKS:
        parser.error(f"--chunks must be between 1 and {config.MAX_CHUNKS} (config.MAX_CHUNKS)")

    text = build_scaffold(args.chunks, config.MAX_CHUNKS, config.MAX_PROD_LINES, args.repo_root)
    if args.print:
        sys.stdout.write(text)
        return 0

    path = write_scaffold(text, args.repo_root)
    sys.stdout.write(
        f"manifest scaffolded: {path} ({args.chunks} empty chunk template(s))\n"
        "Next: edit the file — fill in `files:` for each chunk, drop unused chunks.\n"
        "Then `git commit` — pre-commit will validate and dispatch.\n"
        "Note: do NOT git-add .review/ — it lives in your global gitignore.\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
