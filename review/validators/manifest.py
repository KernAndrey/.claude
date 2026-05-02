"""Pure-Python validator for ``.review/manifest.yaml``.

Runs as a cheap pre-check in front of the chunked review pipeline. No LLM
calls. Catches obvious staleness, miscoverage, and oversized chunks before
spawning N reviewer subprocesses.

Returns :class:`ValidationResult` accumulating every problem found, so the
writer sees all errors in one pass instead of fixing them one at a time.

All git interaction is wrapped behind ``runner`` so unit tests pass synthetic
diff/status/numstat without invoking git.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

import config

# Reuse the same prod-code definition the rest of the hook uses so per-chunk
# size accounting matches MAX_PROD_LINES gating.
from hook import is_production_code


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationError:
    """One problem found in a manifest. ``code`` is machine-readable, ``message``
    is the actionable line we'll surface to the writer."""

    code: str
    message: str
    chunk_id: str | None = None
    file_path: str | None = None


@dataclass
class ValidationResult:
    ok: bool = True
    errors: list[ValidationError] = field(default_factory=list)

    def add(
        self,
        code: str,
        message: str,
        *,
        chunk_id: str | None = None,
        file_path: str | None = None,
    ) -> None:
        self.errors.append(ValidationError(code=code, message=message, chunk_id=chunk_id, file_path=file_path))
        self.ok = False

    def to_text(self) -> str:
        if self.ok:
            return "manifest valid"
        lines = [f"manifest validation failed ({len(self.errors)} error(s)):"]
        for err in self.errors:
            tag = err.code
            if err.chunk_id:
                tag += f" [chunk={err.chunk_id}]"
            if err.file_path:
                tag += f" [file={err.file_path}]"
            lines.append(f"  - {tag}: {err.message}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Diff parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileChange:
    """One staged path with its diff status, line count, and added line numbers
    (post-image)."""

    path: str
    status: str  # A, M, D, R, C, T
    original_path: str | None
    added: int
    removed: int
    is_binary: bool
    added_line_numbers: frozenset[int]


_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_PLUS_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")


def _classify_diff_header(raw: str) -> tuple[str, str | None]:
    """Identify the header role of a non-hunk-body diff line.

    Returns ``(kind, payload)`` where ``kind`` is one of
    ``"file_reset"``, ``"plus_header"``, ``"hunk"``, ``"none"`` and the
    payload is the parsed value (path or new-line number) when relevant.
    """
    if raw.startswith("diff --git "):
        return "file_reset", None
    if raw.startswith("--- "):
        return "none", None
    m = _PLUS_FILE_HEADER_RE.match(raw)
    if m:
        path = m.group(1).strip()
        return "plus_header", None if path == "/dev/null" else path
    m = _HUNK_HEADER_RE.match(raw)
    if m:
        return "hunk", m.group(1)
    return "none", None


def parse_added_lines(diff_text: str) -> dict[str, set[int]]:
    """Walk a unified diff. Return ``{path: {new_file_line_numbers_with_'+'}}``.

    Uses the ``+++ b/`` header to bind subsequent hunks to a path;
    ``diff --git`` resets state so binary blocks (no ``+++``) don't leak.
    """
    result: dict[str, set[int]] = {}
    current_path: str | None = None
    new_line: int = 0
    in_hunk = False

    for raw in diff_text.splitlines():
        kind, payload = _classify_diff_header(raw)
        if kind == "file_reset":
            current_path = None
            in_hunk = False
            continue
        if kind == "plus_header" and not in_hunk:
            current_path = payload
            in_hunk = False
            continue
        if kind == "hunk":
            new_line = int(payload)  # type: ignore[arg-type]
            in_hunk = True
            continue
        if not in_hunk or current_path is None:
            continue
        if raw.startswith("+"):
            result.setdefault(current_path, set()).add(new_line)
            new_line += 1
        elif raw.startswith(" ") or raw == "":
            new_line += 1
        # "-" lines and "\ No newline at end of file" do not advance new_line.
    return result


_NAME_STATUS_RENAME_RE = re.compile(r"^[RC](\d{1,3})$")

# Chunk IDs are baked into reviewer-finding IDs as
# ``<chunk_id>-<backend>-F<n>``. The ID-pattern regex in hook.py
# (`_FINDING_ID_PATTERN`) accepts lowercase alphanumerics, digits, and
# underscores. Restrict chunk_id to match — this prevents a manifest
# from emitting findings the consolidation parser silently drops.
_VALID_CHUNK_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def parse_name_status(text: str) -> list[tuple[str, str, str | None]]:
    """Parse ``git diff --cached --name-status -M`` output.

    Returns ``[(status_letter, path, original_path_or_None), ...]``.
    Rename/copy statuses ``R100``/``C95`` are normalized to ``R``/``C``.
    """
    out: list[tuple[str, str, str | None]] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status_raw = parts[0]
        m = _NAME_STATUS_RENAME_RE.match(status_raw)
        if m:
            status = status_raw[0]  # "R" or "C"
            if len(parts) < 3:
                continue  # malformed rename row — skip
            original_path = parts[1]
            new_path = parts[2]
            out.append((status, new_path, original_path))
        else:
            status = status_raw[0] if status_raw else "M"
            path = parts[1]
            out.append((status, path, None))
    return out


def parse_numstat(text: str) -> dict[str, tuple[int, int, bool]]:
    """Parse ``git diff --cached --numstat`` output.

    Returns ``{path: (added, removed, is_binary)}``. Binary files surface
    as ``-\\t-\\tpath`` in numstat; we map them to ``(0, 0, True)``.

    For renames, numstat emits ``a\\tb\\told -> new`` or, when ``-M`` was on
    the producing command, ``a\\tb\\tnew``. We accept both and key by the
    new path; a future ``a\\tb\\told -> new`` form lands in the dict under
    ``new`` after stripping the arrow.
    """
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
        added = 0 if is_binary else int(added_s)
        removed = 0 if is_binary else int(removed_s)
        out[path_field] = (added, removed, is_binary)
    return out


def _extract_new_path(path_field: str) -> str:
    """Handle git rename numstat path fields: brace form and arrow form.

    Brace form ``prefix/{old => new}suffix`` normalises to
    ``prefix/newsuffix``. Simple arrow form ``old => new`` becomes ``new``.
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


GitRunner = Callable[[list[str]], str]


def _default_git_runner(args: list[str]) -> str:
    return subprocess.check_output(["git", *args], text=True)


def collect_file_changes(diff_text: str, *, runner: GitRunner | None = None) -> list[FileChange]:
    """Combine name-status + numstat + parsed added-lines into FileChange list."""
    runner = runner or _default_git_runner
    name_status = parse_name_status(runner(["diff", "--cached", "--name-status", "-M"]))
    numstat = parse_numstat(runner(["diff", "--cached", "--numstat"]))
    added_map = parse_added_lines(diff_text)

    changes: list[FileChange] = []
    for status, path, original in name_status:
        added, removed, is_binary = numstat.get(path, (0, 0, False))
        changes.append(
            FileChange(
                path=path,
                status=status,
                original_path=original,
                added=added,
                removed=removed,
                is_binary=is_binary,
                added_line_numbers=frozenset(added_map.get(path, set())),
            )
        )
    return changes


# ---------------------------------------------------------------------------
# Top-level validator
# ---------------------------------------------------------------------------


def _parse_manifest(manifest_text: str, res: ValidationResult) -> dict[str, Any] | None:
    """Parse YAML, validate the top-level shape. Returns None if unrecoverable."""
    try:
        manifest = yaml.safe_load(manifest_text)
    except yaml.YAMLError as e:
        res.add("malformed_yaml", f"manifest.yaml is not valid YAML: {e}")
        return None
    if manifest is None:
        res.add("empty_manifest", "manifest.yaml is empty")
        return None
    if not isinstance(manifest, dict):
        res.add("malformed_yaml", "top-level of manifest.yaml must be a mapping")
        return None
    return manifest


def _check_diff_hash(manifest: dict[str, Any], diff_text: str, res: ValidationResult) -> None:
    expected = hashlib.sha256(diff_text.encode()).hexdigest()
    if manifest.get("diff_hash") != expected:
        res.add(
            "stale_hash",
            "diff_hash mismatch — manifest is stale relative to the staged diff. "
            "Regenerate with python3 ~/.claude/review/scripts/scaffold_manifest.py.",
        )


# review_lenses was removed from the per-chunk schema: it was an unused
# focus-hint that the per-chunk reviewer prompt ignores in practice
# (every chunk reviewer covers all of bugs/architecture/tests). The
# whole-diff lens layer fans out across config.ALLOWED_LENSES regardless,
# so writers shouldn't have to declare lenses per chunk.


def _index_chunk_files(
    chunk: dict[str, Any],
    cid: str,
    file_to_chunks: dict[str, list[str]],
    res: ValidationResult,
) -> bool:
    """Validate file-entry shapes and update the path → chunks index. Returns True
    if at least the basic shape is OK."""
    files = chunk.get("files")
    if not isinstance(files, list) or not files:
        res.add(
            "missing_files",
            f"chunk '{cid}': 'files' must be a non-empty list",
            chunk_id=cid,
        )
        return False
    for entry in files:
        if not isinstance(entry, dict):
            res.add(
                "malformed_file_entry",
                f"chunk '{cid}': each file entry must be a mapping",
                chunk_id=cid,
            )
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            res.add(
                "missing_file_path",
                f"chunk '{cid}': file entry missing 'path'",
                chunk_id=cid,
            )
            continue
        file_to_chunks.setdefault(path, []).append(cid)
    return True


def _validate_chunks(chunks: list[Any], res: ValidationResult) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Per-chunk schema validation. Returns (well_formed_chunks, file_to_chunks)."""
    chunk_ids: set[str] = set()
    chunk_records: list[dict[str, Any]] = []
    file_to_chunks: dict[str, list[str]] = {}

    for i, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            res.add("malformed_chunk", f"chunks[{i}] must be a mapping")
            continue
        cid = chunk.get("id")
        if not isinstance(cid, str) or not cid:
            res.add("missing_chunk_id", f"chunks[{i}] missing string 'id'")
            continue
        if not _VALID_CHUNK_ID_RE.match(cid):
            res.add(
                "invalid_chunk_id",
                f"chunk id '{cid}' must match [a-z][a-z0-9_]* "
                "(lowercase letters, digits, underscores; starts with a letter)",
                chunk_id=cid,
            )
            continue
        if cid in chunk_ids:
            res.add("duplicate_chunk_id", f"duplicate chunk id '{cid}'", chunk_id=cid)
            continue
        chunk_ids.add(cid)
        if not _index_chunk_files(chunk, cid, file_to_chunks, res):
            continue
        chunk_records.append(chunk)
    return chunk_records, file_to_chunks


def _check_coverage(
    file_to_chunks: dict[str, list[str]],
    by_path: dict[str, FileChange],
    res: ValidationResult,
) -> None:
    """Cross-check files claimed by manifest against files in the staged diff.

    File-level overlap (same file in multiple chunks) is **allowed** — that's
    how the writer splits a big file into pieces. Line-level overlap is
    flagged separately by ``_check_aggregate_line_coverage``.
    """
    changed = set(by_path.keys())
    claimed = set(file_to_chunks.keys())
    for p in sorted(claimed - changed):
        res.add("unknown_file", f"manifest lists '{p}' but it is not in the staged diff", file_path=p)
    for p in sorted(changed - claimed):
        res.add("uncovered_file", f"file '{p}' is in the staged diff but no chunk claims it", file_path=p)


def _resolve_claimed_lines(
    line_ranges: Any,  # noqa: ANN401 — raw YAML value, type-checked inside
    cid: str,
    path: str,
    fc: FileChange,
    res: ValidationResult,
) -> set[int] | None:
    """Turn a manifest ``line_ranges`` value into the set of line numbers it
    claims. Returns None on a malformed/inverted range — caller skips further
    accounting for this entry."""
    if line_ranges == "all":
        return set(fc.added_line_numbers)
    if not isinstance(line_ranges, list):
        res.add(
            "malformed_line_ranges",
            f"chunk '{cid}' file '{path}': line_ranges must be a list of [start, end] pairs or the string 'all'",
            chunk_id=cid,
            file_path=path,
        )
        return None
    claimed: set[int] = set()
    for rng in line_ranges:
        if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, int) for x in rng)):
            res.add(
                "malformed_range",
                f"chunk '{cid}' file '{path}': line_ranges entries must be [start, end] integer pairs",
                chunk_id=cid,
                file_path=path,
            )
            return None
        start, end = rng
        if start > end:
            res.add(
                "invalid_range",
                f"chunk '{cid}' file '{path}': inverted range [{start}, {end}]",
                chunk_id=cid,
                file_path=path,
            )
            return None
        claimed.update(range(start, end + 1))
    return claimed


def _check_per_chunk_strays(
    cid: str,
    entry: dict[str, Any],
    fc: FileChange,
    claimed: set[int],
    res: ValidationResult,
) -> None:
    """Per-(chunk, file) sanity: flag a chunk whose entire claimed range
    sits outside any actually-changed line. Aggregate gap/overlap checks
    live in ``_check_aggregate_line_coverage`` so files split across chunks
    via disjoint ranges aren't wrongly accused of leaving lines uncovered."""
    path = entry["path"]
    if claimed - set(fc.added_line_numbers) and not (claimed & fc.added_line_numbers) and fc.added_line_numbers:
        preview = sorted(claimed - set(fc.added_line_numbers))[:5]
        res.add(
            "range_outside_diff",
            f"chunk '{cid}' file '{path}': line_ranges {preview!r} cover no actually-changed lines",
            chunk_id=cid,
            file_path=path,
        )


def _validate_file_entry(
    cid: str,
    entry: dict[str, Any],
    by_path: dict[str, FileChange],
    res: ValidationResult,
) -> int:
    """Run line-coverage checks for one (chunk, file) pair. Returns the count of
    added prod lines this entry contributes to the chunk's size budget. Returns
    0 for binary/deleted/unknown files (they don't count toward MAX_PROD_LINES)."""
    path = entry.get("path")
    if not isinstance(path, str):
        return 0
    fc = by_path.get(path)
    if fc is None or fc.is_binary or fc.status in ("D", "T"):
        return 0
    if fc.status in ("R", "C"):
        claimed_op = entry.get("original_path")
        if claimed_op != fc.original_path:
            res.add(
                "rename_path_mismatch",
                f"chunk '{cid}' file '{path}': manifest original_path={claimed_op!r}, diff says {fc.original_path!r}",
                chunk_id=cid,
                file_path=path,
            )
    claimed = _resolve_claimed_lines(entry.get("line_ranges", []), cid, path, fc, res)
    if claimed is None:
        return 0
    _check_per_chunk_strays(cid, entry, fc, claimed, res)
    if is_production_code(path):
        return len(claimed & fc.added_line_numbers)
    return 0


def _check_chunk_sizes(
    chunk_records: list[dict[str, Any]],
    by_path: dict[str, FileChange],
    res: ValidationResult,
) -> None:
    for chunk in chunk_records:
        cid = chunk["id"]
        prod_lines = 0
        for entry in chunk.get("files", []):
            if isinstance(entry, dict):
                prod_lines += _validate_file_entry(cid, entry, by_path, res)
        if prod_lines > config.MAX_PROD_LINES:
            res.add(
                "oversized_chunk",
                f"chunk '{cid}': {prod_lines} added prod lines, max is "
                f"{config.MAX_PROD_LINES}. Split this chunk further.",
                chunk_id=cid,
            )


def _gather_per_file_claims(
    chunk_records: list[dict[str, Any]],
    by_path: dict[str, FileChange],
    res: ValidationResult,
) -> dict[str, list[tuple[str, set[int]]]]:
    """Collect the (chunk_id, claimed_lines) list for every changed file
    that has line semantics (skips binary / deleted / mode-only)."""
    per_file: dict[str, list[tuple[str, set[int]]]] = {}
    for chunk in chunk_records:
        cid = chunk["id"]
        for entry in chunk.get("files", []):
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not isinstance(path, str):
                continue
            fc = by_path.get(path)
            if fc is None or fc.is_binary or fc.status in ("D", "T"):
                continue
            claimed = _resolve_claimed_lines(entry.get("line_ranges", []), cid, path, fc, res)
            if claimed is None:
                continue
            per_file.setdefault(path, []).append((cid, claimed))
    return per_file


def _flag_aggregate_gap_and_overlap(
    path: str,
    fc: FileChange,
    claims: list[tuple[str, set[int]]],
    res: ValidationResult,
) -> None:
    union: set[int] = set()
    for _, claimed in claims:
        union |= claimed
    uncovered = fc.added_line_numbers - union
    if uncovered:
        preview = sorted(uncovered)[:5]
        res.add(
            "line_gap",
            f"file '{path}': {len(uncovered)} added line(s) not claimed by any chunk; first: {preview}",
            file_path=path,
        )
    for i, (cid_a, set_a) in enumerate(claims):
        for cid_b, set_b in claims[i + 1 :]:
            overlap = set_a & set_b & fc.added_line_numbers
            if overlap:
                preview = sorted(overlap)[:5]
                res.add(
                    "range_overlap",
                    f"file '{path}': lines {preview} claimed by both '{cid_a}' "
                    f"and '{cid_b}' (each added line must belong to exactly one chunk)",
                    file_path=path,
                )


def _check_aggregate_line_coverage(
    chunk_records: list[dict[str, Any]],
    by_path: dict[str, FileChange],
    res: ValidationResult,
) -> None:
    """For each changed file, aggregate the claimed lines across every chunk
    that claims it. Splitting via disjoint ranges is fine — flag only:
    * **gaps** — lines no chunk owns
    * **overlap** — lines two chunks both own
    Binary, deleted, and mode-only files are skipped (no line semantics).
    """
    per_file = _gather_per_file_claims(chunk_records, by_path, res)
    for path, claims in per_file.items():
        _flag_aggregate_gap_and_overlap(path, by_path[path], claims, res)


def _check_related_paths(
    paths: Any,  # noqa: ANN401 — raw YAML value, type-checked inside
    label: str,
    repo_root: Path,
    res: ValidationResult,
    *,
    chunk_id: str | None = None,
) -> None:
    if paths is None:
        return
    if not isinstance(paths, list):
        res.add("malformed_related_files", f"{label}: must be a list of paths", chunk_id=chunk_id)
        return
    for rp in paths:
        if not isinstance(rp, str):
            res.add(
                "malformed_related_files",
                f"{label}: entry must be a string, got {type(rp).__name__}",
                chunk_id=chunk_id,
            )
            continue
        if not (repo_root / rp).is_file():
            res.add(
                "missing_related_file",
                f"{label}: references missing file '{rp}'",
                chunk_id=chunk_id,
                file_path=rp,
            )


def _check_related_files(
    manifest: dict[str, Any],
    chunk_records: list[dict[str, Any]],
    repo_root: Path,
    res: ValidationResult,
) -> None:
    for chunk in chunk_records:
        _check_related_paths(
            chunk.get("related_files"),
            f"chunk '{chunk['id']}' related_files",
            repo_root,
            res,
            chunk_id=chunk["id"],
        )
    _check_related_paths(manifest.get("default_related_files"), "default_related_files", repo_root, res)


def validate(
    manifest_text: str,
    diff_text: str,
    repo_root: Path,
    *,
    runner: GitRunner | None = None,
) -> ValidationResult:
    """Run all manifest checks. Errors accumulate so the writer sees the full
    picture in one pass."""
    res = ValidationResult()
    manifest = _parse_manifest(manifest_text, res)
    if manifest is None:
        return res

    _check_diff_hash(manifest, diff_text, res)

    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        res.add("no_chunks", "'chunks' must be a non-empty list")
        return res
    if len(chunks) > config.MAX_CHUNKS:
        res.add(
            "too_many_chunks",
            f"manifest declares {len(chunks)} chunks, max is {config.MAX_CHUNKS}. "
            "Merge related chunks or split the commit.",
        )

    chunk_records, file_to_chunks = _validate_chunks(chunks, res)

    try:
        by_path = {fc.path: fc for fc in collect_file_changes(diff_text, runner=runner)}
    except (subprocess.CalledProcessError, OSError) as exc:
        res.add("git_runner_failed", f"git runner failed: {exc}")
        return res
    _check_coverage(file_to_chunks, by_path, res)
    _check_chunk_sizes(chunk_records, by_path, res)
    _check_aggregate_line_coverage(chunk_records, by_path, res)
    _check_related_files(manifest, chunk_records, repo_root, res)
    return res
