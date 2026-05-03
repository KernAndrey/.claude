"""Pre-flight coverage + assert gate.

Self-contained script — no imports from validators/, hook.py, or config.py.
Can be run standalone: ``python scripts/preflight_gate.py``.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import Any

try:
    import tomllib
except ImportError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Diff parsing (inlined from validators/manifest.py — keep self-contained)
# ---------------------------------------------------------------------------

_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_PLUS_FILE_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")


def _classify_diff_header(raw: str) -> tuple[str, str | None]:
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
    return result


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateConfig:
    enabled: bool = True
    coverage_check_enabled: bool = True
    assert_check_enabled: bool = True
    compare_branch: str = "origin/main"
    coverage_threshold: float = 100.0
    threshold_setup_error: str | None = None
    config_setup_error: str | None = None
    fail_open_on_error: bool = False
    output_format: str = "ansi"
    max_report_lines_in_output: int = 30
    count_pytest_raises_as_assertion: bool = True
    min_assertions_per_test: int = 1
    custom_assertion_helpers: frozenset[str] = field(default_factory=frozenset)
    test_file_patterns: tuple[str, ...] = (
        "**/tests/**/test_*.py",
        "**/tests/**/*_test.py",
        "**/test_*.py",
        "**/*_test.py",
    )
    coverage_exclude_paths: tuple[str, ...] = (
        # Tests don't exercise themselves — without this exclusion, every
        # added test line is reported as `uncovered` by diff-cover.
        # diff-cover uses fnmatch (single `*`), so `**` recursive globs do
        # NOT work. Use `*/foo/*` for "any path containing /foo/" style.
        "*/tests/*",
        "test_*.py",
        "*/test_*.py",
        "*_test.py",
        "*/*_test.py",
        "conftest.py",
        "*/conftest.py",
    )
    javascript_assert_enabled: bool = True
    javascript_test_function_names: tuple[str, ...] = (
        "it",
        "test",
        "test.skip",
        "test.only",
        "it.skip",
        "it.only",
        "test.each",
        "it.each",
    )
    javascript_custom_assertion_helpers: frozenset[str] = field(default_factory=frozenset)
    coverage_reports: tuple[str, ...] = ("coverage.xml",)
    # Tighter assertion checks — flag tests whose only "assertion" is
    # a trivial literal (assert True, assertTrue(True), etc.). Default ON;
    # opt-out via `[tool.code_review.coverage_gate.assert_counter.python]
    # trivial_assertion_check = false`.
    trivial_assertion_check: bool = True
    # Flag tests that mock the very symbol they claim to test. Heuristic:
    # any `@patch("...X")` / `with patch("...X")` / `mocker.patch("...X")`
    # whose final segment matches a public def/class added in this same
    # diff. False positives possible; opt-out via `mock_of_uut_check`.
    mock_of_uut_check: bool = True
    # Flag new public def/class in added prod files whose name appears
    # nowhere in any test file in the same diff. Catches the most common
    # "forgot to add a test" miss.
    missing_test_reference_check: bool = True
    # Strict coverage-freshness via blob hash. Default OFF — requires
    # coverage.xml metadata that not every coverage backend writes.
    coverage_freshness_strict: bool = False


def _load_config(repo_root: Path) -> GateConfig:  # noqa: C901,PLR0912,PLR0915
    pyproject = repo_root / "pyproject.toml"
    section: dict[str, Any] = {}
    if pyproject.exists():
        with pyproject.open("rb") as fh:
            try:
                data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                return GateConfig(config_setup_error=f"malformed pyproject.toml: {exc}")
        section = data.get("tool", {}).get("code_review", {}).get("coverage_gate", {})

    enabled = section.get("enabled", True)
    if enabled is False:
        return GateConfig(enabled=False)

    cov = section.get("coverage_check", {})
    ast_cfg = section.get("assert_counter", {})
    py_cfg = ast_cfg.get("python", {})
    js_cfg = ast_cfg.get("javascript", {})

    env_threshold = os.environ.get("CODE_REVIEW_COVERAGE_THRESHOLD")
    threshold_setup_error: str | None = None
    if env_threshold is not None:
        try:
            threshold = float(env_threshold)
        except ValueError:
            threshold = 100.0
            threshold_setup_error = f"CODE_REVIEW_COVERAGE_THRESHOLD must be a number, got: {env_threshold!r}"
    else:
        threshold = cov.get("threshold", 100.0)

    raw_helpers_py = py_cfg.get("custom_assertion_helpers", [])
    if isinstance(raw_helpers_py, str):
        custom_helpers_py = frozenset((raw_helpers_py,))
    else:
        custom_helpers_py = frozenset(raw_helpers_py)
    raw_helpers_js = js_cfg.get("custom_assertion_helpers", [])
    if isinstance(raw_helpers_js, str):
        custom_helpers_js = frozenset((raw_helpers_js,))
    else:
        custom_helpers_js = frozenset(raw_helpers_js)

    reports = cov.get("reports", ["coverage.xml"])
    if isinstance(reports, str):
        reports = (reports,)

    # Accept both `exclude` (concise; matches what self-host pyproject.toml
    # already used) and `exclude_paths` (verbose) for the same field.
    raw_exclude = cov.get("exclude_paths", cov.get("exclude"))
    if raw_exclude is None:
        coverage_exclude = GateConfig.coverage_exclude_paths
    elif isinstance(raw_exclude, str):
        coverage_exclude = (raw_exclude,)
    else:
        coverage_exclude = tuple(raw_exclude)

    raw_test_patterns = py_cfg.get("test_file_patterns")
    if raw_test_patterns is None:
        test_patterns = GateConfig.test_file_patterns
    elif isinstance(raw_test_patterns, str):
        test_patterns = (raw_test_patterns,)
    else:
        test_patterns = tuple(raw_test_patterns)

    raw_js_names = js_cfg.get("test_function_names")
    if raw_js_names is None:
        js_test_names = GateConfig.javascript_test_function_names
    elif isinstance(raw_js_names, str):
        js_test_names = (raw_js_names,)
    else:
        js_test_names = tuple(raw_js_names)

    fail_open = section.get("fail_open_on_error", False)
    if os.environ.get("CODE_REVIEW_GATE_FAIL_OPEN", "") == "1":
        fail_open = True

    return GateConfig(
        enabled=True,
        coverage_check_enabled=cov.get("enabled", True),
        assert_check_enabled=ast_cfg.get("enabled", True),
        compare_branch=cov.get("compare_branch", "origin/main"),
        coverage_threshold=threshold,
        threshold_setup_error=threshold_setup_error,
        fail_open_on_error=fail_open,
        output_format=section.get("output_format", "ansi"),
        max_report_lines_in_output=section.get("max_report_lines_in_output", 30),
        count_pytest_raises_as_assertion=py_cfg.get("count_pytest_raises_as_assertion", True),
        min_assertions_per_test=py_cfg.get("min_assertions_per_test", 1),
        custom_assertion_helpers=custom_helpers_py,
        trivial_assertion_check=py_cfg.get("trivial_assertion_check", True),
        mock_of_uut_check=py_cfg.get("mock_of_uut_check", True),
        missing_test_reference_check=py_cfg.get("missing_test_reference_check", True),
        coverage_freshness_strict=cov.get("freshness_strict", False),
        test_file_patterns=test_patterns,
        javascript_assert_enabled=js_cfg.get("enabled", True),
        javascript_test_function_names=js_test_names,
        javascript_custom_assertion_helpers=custom_helpers_js,
        coverage_reports=tuple(reports),
        coverage_exclude_paths=coverage_exclude,
    )


# ---------------------------------------------------------------------------
# Reporter
# ---------------------------------------------------------------------------


def _red(msg: str) -> str:
    return f"\033[31m{msg}\033[0m"


def _yellow(msg: str) -> str:
    return f"\033[33m{msg}\033[0m"


def _cyan(msg: str) -> str:
    return f"\033[36m{msg}\033[0m"


def _green(msg: str) -> str:
    return f"\033[32m{msg}\033[0m"


def _tag(color: str, label: str, msg: str) -> str:
    return f"{color}[preflight] {msg}\033[0m"


@dataclass(frozen=True)
class CoverageFinding:
    path: str
    line: int
    kind: str


@dataclass
class CoverageResult:
    findings: list[CoverageFinding] = field(default_factory=list)
    setup_error: str | None = None
    internal_error: str | None = None


@dataclass(frozen=True)
class AssertFinding:
    path: str
    function_name: str
    def_line: int
    # Discriminator for the finding's class. Existing call sites use the
    # default "no_assertion" (test function with zero real assertions).
    # New checks emit "mocks_unit_under_test", "missing_test_reference".
    kind: str = "no_assertion"
    # Optional human-readable hint shown in the report after the location.
    detail: str = ""


@dataclass
class AssertResult:
    findings: list[AssertFinding] = field(default_factory=list)
    setup_error: str | None = None
    internal_error: str | None = None


def _format_report(
    coverage_result: CoverageResult | None,
    assert_result: AssertResult | None,
    config: GateConfig,
) -> str:
    use_color = config.output_format == "ansi" and sys.stderr.isatty()
    lines: list[str] = []
    cov_ok = coverage_result is None or not coverage_result.findings
    assert_ok = assert_result is None or not assert_result.findings

    def c(col_fn: Callable[[str], str], text: str) -> str:
        return col_fn(text) if use_color else text

    # Coverage section
    if coverage_result is not None:
        if coverage_result.findings:
            lines.append(c(_red, f"[1/2] Coverage gate — {len(coverage_result.findings)} uncovered line(s)/branch(es)"))
            for f in coverage_result.findings[: config.max_report_lines_in_output]:
                lines.append(f"  {f.path}:{f.line} ({f.kind})")
            if len(coverage_result.findings) > config.max_report_lines_in_output:
                lines.append(f"  ... and {len(coverage_result.findings) - config.max_report_lines_in_output} more")
        else:
            lines.append(c(_green, "[1/2] Coverage gate — OK"))

    # Assert section
    if assert_result is not None:
        if assert_result.findings:
            kind_label = {
                "no_assertion": "missing assertion",
                "mocks_unit_under_test": "mocks unit under test",
                "missing_test_reference": "no test references this symbol",
            }
            lines.append(c(_red, f"[2/2] Assert gate — {len(assert_result.findings)} finding(s)"))
            for f in assert_result.findings[: config.max_report_lines_in_output]:
                label = kind_label.get(f.kind, f.kind)
                detail = f"  ({f.detail})" if f.detail else ""
                lines.append(f"  [{label}] {f.path}:{f.def_line} {f.function_name}{detail}")
            if len(assert_result.findings) > config.max_report_lines_in_output:
                lines.append(f"  ... and {len(assert_result.findings) - config.max_report_lines_in_output} more")
        else:
            lines.append(c(_green, "[2/2] Assert gate — OK"))

    if not cov_ok or not assert_ok:
        lines.append("")
        lines.append(
            c(_yellow, "Bypass: CODE_REVIEW_SKIP_GATE=1  or  [tool.code_review.coverage_gate] enabled = false")
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Coverage check
# ---------------------------------------------------------------------------


def _ref_exists(ref: str, repo_root: Path) -> bool:
    """True if `ref` resolves to a commit in `repo_root`. Defensive: any
    subprocess error → False (lets the ladder move on to the next candidate)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def _resolve_compare_branch(config: GateConfig, repo_root: Path) -> str | None:
    """Pick the diff-cover --compare-branch value, with fallback ladder.

    Order:
      1. Env override `CODE_REVIEW_COMPARE_BRANCH` — if set, use as-is.
      2. Config `compare_branch` if it's NOT the legacy default `"origin/main"` —
         user explicitly chose this, trust it.
      3. `git symbolic-ref refs/remotes/origin/HEAD` — what the remote calls its
         default branch (most reliable signal across `main`/`master`/`dev`).
      4. Ladder `origin/main`, `origin/master`, `main`, `master`, `HEAD~1` —
         first one that resolves wins.
      5. `HEAD` — verify it resolves first; if not (truly empty repo), return None.
    """
    env = os.environ.get("CODE_REVIEW_COMPARE_BRANCH")
    if env:
        return env

    raw = config.compare_branch
    if raw and raw != "origin/main":
        return raw

    try:
        head_probe = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        head_probe = None
    if head_probe is not None and head_probe.returncode == 0:
        ref = (getattr(head_probe, "stdout", "") or "").strip()
        if ref.startswith("refs/remotes/"):
            short = ref[len("refs/remotes/") :]
            if _ref_exists(short, repo_root):
                return short

    for cand in ("origin/main", "origin/master", "main", "master", "HEAD~1"):
        if _ref_exists(cand, repo_root):
            return cand

    if _ref_exists("HEAD", repo_root):
        return "HEAD"

    return None


def _git_run(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, result.args, result.stdout, result.stderr)
    return result.stdout


def _glob_match_simple(path: str, pattern: str) -> bool:
    """fnmatch-style single-`*` glob match. Shared by exclude-paths and
    test-file detection in strict-freshness check."""
    i = 0
    parts: list[str] = []
    while i < len(pattern):
        if pattern[i : i + 3] == "**/":
            parts.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return bool(re.match("^" + "".join(parts) + "$", path))


def _strict_freshness_missing_files(
    config: GateConfig,
    staged_files: list[str],
    xml_texts: dict[Path, str],
) -> list[str]:
    """Return staged .py files that are NOT mentioned as `<class
    filename=...>` in any of the coverage XML reports — meaning the
    report was generated against a different state of the tree.

    Excluded paths and test files are filtered out: their absence from
    coverage.xml is expected.
    """
    py_staged = [f for f in staged_files if f.endswith(".py")]
    if not py_staged:
        return []

    covered: set[str] = set()
    for xml_text in xml_texts.values():
        for m in re.finditer(r'<class\b[^>]*\bfilename="([^"]+)"', xml_text):
            covered.add(m.group(1))

    def is_excluded(path: str) -> bool:
        for pat in config.coverage_exclude_paths:
            if _glob_match_simple(path, pat):
                return True
        return False

    def is_test(path: str) -> bool:
        for pat in config.test_file_patterns:
            if _glob_match_simple(path, pat):
                return True
        return False

    return [f for f in py_staged if not is_excluded(f) and not is_test(f) and f not in covered]


def _run_coverage_check(config: GateConfig, repo_root: Path) -> CoverageResult:  # noqa: C901,PLR0911,PLR0912,PLR0915
    reports = [repo_root / r for r in config.coverage_reports]
    for report_path in reports:
        if not report_path.exists():
            return CoverageResult(setup_error=f"missing coverage report at {report_path}")

    # Staleness check
    try:
        staged_files = _git_run(["diff", "--cached", "--name-only"], cwd=repo_root).splitlines()
    except subprocess.CalledProcessError:
        return CoverageResult(internal_error="git diff --cached --name-only failed")

    newest_staged_mtime: float = 0.0
    for f in staged_files:
        # Prefer working-tree mtime: this reflects when the user edited the
        # content currently being staged. Falling back to `git log --format=%ct`
        # would only see the previous commit's timestamp for a modified
        # tracked file — coverage.xml regenerated AFTER that commit but
        # BEFORE the new edit would falsely appear "fresh enough".
        try:
            st = (repo_root / f).stat()
            if st.st_mtime > newest_staged_mtime:
                newest_staged_mtime = st.st_mtime
            continue
        except OSError:
            pass
        # File staged for deletion or otherwise absent from working tree —
        # fall back to last-commit timestamp so a stale coverage report
        # generated before the deletion still gets flagged.
        try:
            log_ts = _git_run(["log", "-1", "--format=%ct", "--", f], cwd=repo_root).strip()
            if log_ts:
                newest_staged_mtime = max(newest_staged_mtime, float(log_ts))
        except (subprocess.CalledProcessError, ValueError):
            pass

    for report_path in reports:
        report_mtime = report_path.stat().st_mtime
        if report_mtime < newest_staged_mtime:
            delta = int(newest_staged_mtime - report_mtime)
            return CoverageResult(setup_error=f"coverage report stale by {delta}s — regenerate with `coverage run`")

    # Branch-data check for XML reports — Cobertura format. We only need a
    # boolean: presence of branch-rate attribute on root, or any <line> element
    # with branch="true". Two text-level regex matches are sufficient and avoid
    # exposing an XML parser to potentially-attacker-controlled coverage files
    # (XXE / billion-laughs).
    xml_reports = [p for p in reports if p.suffix == ".xml"]
    xml_texts: dict[Path, str] = {}
    for xml_path in xml_reports:
        try:
            xml_text = xml_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return CoverageResult(setup_error=f"cannot read {xml_path}: {exc}")
        xml_texts[xml_path] = xml_text

        has_branch = bool(
            re.search(r"<coverage\b[^>]*\bbranch-rate=", xml_text)
            or re.search(r"<line\b[^>]*\bbranch=\"true\"", xml_text)
        )
        if not has_branch:
            return CoverageResult(setup_error=f"{xml_path} has no branch data — set branch=true in [tool.coverage.run]")

    # Strict freshness — opt-in. Catches "regenerated xml but forgot to
    # re-run pytest after adding a new file": every added .py file (that
    # isn't a test or excluded) must appear as a <class filename=...> in
    # at least one coverage report.
    if config.coverage_freshness_strict and xml_texts:
        missing = _strict_freshness_missing_files(config, staged_files, xml_texts)
        if missing:
            preview = ", ".join(missing[:3])
            suffix = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
            return CoverageResult(
                setup_error=(
                    f"coverage.xml missing {len(missing)} added file(s): {preview}{suffix} — "
                    "regenerate with `pytest --cov`"
                )
            )

    compare_branch = _resolve_compare_branch(config, repo_root)
    if compare_branch is None:
        return CoverageResult(
            setup_error="no resolvable compare-branch — try `git fetch` or set CODE_REVIEW_COMPARE_BRANCH"
        )

    # Build diff-cover invocation. The --exclude flag uses argparse
    # `nargs="+"`, so multiple `--exclude=X` flags overwrite each other —
    # only the last wins. Emit ONE `--exclude` followed by all patterns as
    # positional values, which is what diff-cover actually consumes.
    with tempfile.TemporaryDirectory() as td:
        json_path = Path(td) / "coverage_gate.json"
        args = [
            "diff-cover",
            *[str(r) for r in reports],
            f"--compare-branch={compare_branch}",
            f"--format=json:{json_path}",
            f"--fail-under={config.coverage_threshold}",
            "--expand-coverage-report",
            "--include-untracked",
        ]
        if config.coverage_exclude_paths:
            args.extend(["--exclude", *config.coverage_exclude_paths])

        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                cwd=repo_root,
                timeout=120,
            )
        except FileNotFoundError:
            return CoverageResult(setup_error="diff-cover not on PATH — pip install diff-cover")
        except subprocess.TimeoutExpired:
            return CoverageResult(internal_error="diff-cover timed out after 120s")

        rc = result.returncode
        if rc >= 2:
            hint = ""
            if "ambiguous" in result.stderr.lower() or "unknown revision" in result.stderr.lower():
                hint = " — try `git fetch`"
            return CoverageResult(internal_error=f"diff-cover crashed (rc={rc}){hint}")

        if not json_path.exists():
            return CoverageResult(internal_error="diff-cover did not produce JSON output")

        try:
            with json_path.open() as fh:
                data = json.load(fh)
        except json.JSONDecodeError as exc:
            return CoverageResult(internal_error=f"diff-cover JSON parse error: {exc}")

        findings: list[CoverageFinding] = []
        # diff-cover JSON schema varies by version; try common shapes
        srcs = data
        if isinstance(data, dict):
            srcs = data.get("src_stats", data.get("sources", data))

        def _emit_violation(path: str, viol: object) -> None:
            # Normalize a single violation entry into a CoverageFinding.
            # Real diff-cover output shape is `[line, kind_or_None]` (list-of-pairs);
            # other versions emit dicts ({line, kind}) or bare ints.
            if isinstance(viol, dict):
                line = int(viol.get("line", 0))
                kind = viol.get("kind") or "line"
            elif isinstance(viol, list) and viol:
                line = int(viol[0])
                kind = (viol[1] if len(viol) > 1 else None) or "line"
            else:
                line = int(viol)
                kind = "line"
            findings.append(CoverageFinding(path, line, kind))

        def _emit_for_stats(path: str, stats: object) -> None:
            # diff-cover writes BOTH `violations` (richer) and `violation_lines` (just lines)
            # — prefer `violations` when present to keep findings unique and informative.
            if isinstance(stats, dict):
                if stats.get("violations"):
                    for viol in stats["violations"]:
                        _emit_violation(path, viol)
                else:
                    for viol in stats.get("violation_lines", []):
                        _emit_violation(path, viol)
            elif isinstance(stats, list):
                for viol in stats:
                    _emit_violation(path, viol)

        if isinstance(srcs, dict):
            for path, stats in srcs.items():
                _emit_for_stats(str(path), stats)
        elif isinstance(srcs, list):
            for entry in srcs:
                if isinstance(entry, dict):
                    path = str(entry.get("filename", entry.get("path", "unknown")))
                    if entry.get("violations"):
                        for viol in entry["violations"]:
                            _emit_violation(path, viol)
                    else:
                        for viol in entry.get("violation_lines", []):
                            _emit_violation(path, viol)

        return CoverageResult(findings=findings)


# ---------------------------------------------------------------------------
# Assert check — Python
# ---------------------------------------------------------------------------


def _iter_test_funcs(tree: ast.AST) -> Iterator[ast.FunctionDef | ast.AsyncFunctionDef]:
    if not isinstance(tree, ast.Module):
        return
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            yield node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test_"):
                    yield sub


def _has_skip_decorator(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in func.decorator_list:
        name = ""
        if isinstance(dec, ast.Call):
            name = _attr_name(dec.func) or ""
        elif isinstance(dec, ast.Name):
            name = dec.id
        elif isinstance(dec, ast.Attribute):
            name = _attr_name(dec) or ""
        if name in (
            "pytest.mark.skip",
            "pytest.mark.skipif",
            "unittest.skip",
            "unittest.skipIf",
            "unittest.skipUnless",
            "skip",
            "skipif",
            "skipIf",
            "skipUnless",
        ):
            return True
    return False


def _attr_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    n: ast.AST = node
    while isinstance(n, ast.Attribute):
        parts.append(n.attr)
        n = n.value
    if isinstance(n, ast.Name):
        parts.append(n.id)
        return ".".join(reversed(parts))
    return None


def _count_assertions(func: ast.FunctionDef | ast.AsyncFunctionDef, config: GateConfig) -> int:
    count = 0
    for stmt in func.body:
        count += _count_assertions_in_node(stmt, config)
    return count


def _is_trivial_assertion(node: ast.AST) -> bool:  # noqa: PLR0911
    """Return True for assertion expressions whose semantic value is zero.

    Caught patterns:
      - `assert <truthy literal>` / `assert <falsy literal>`           (Constant)
      - `assert []` / `assert {}` / `assert ()` (any literal collection)
      - `assertTrue(True)` / `assertFalse(False)` / `assertIs(x, x)`   (1-arg)
      - `assertEqual(x, x)` / `assertEqual(literal, same_literal)`     (2-arg)

    Anything else (including `assert x`, `assert foo() == bar`, or any
    Call with a non-literal argument) is treated as a real assertion.
    """
    # Bare `assert` statements with literal/empty test.
    if isinstance(node, ast.Assert):
        t = node.test
        if isinstance(t, ast.Constant):
            return True
        if isinstance(t, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
            return True
        return False
    # assertX(...) Call form.
    if isinstance(node, ast.Call):
        attr = getattr(node.func, "attr", "") or (node.func.id if isinstance(node.func, ast.Name) else "")
        if not isinstance(attr, str) or not attr.startswith("assert"):
            return False
        if attr in ("assertTrue", "assertFalse", "assertIsNone", "assertIsNotNone") and len(node.args) == 1:
            arg = node.args[0]
            return isinstance(arg, ast.Constant)
        if attr in ("assertEqual", "assertEquals", "assertIs", "assertNotEqual", "assertIsNot") and len(node.args) >= 2:
            a, b = node.args[0], node.args[1]
            if isinstance(a, ast.Constant) and isinstance(b, ast.Constant) and a.value == b.value:
                return True
            if isinstance(a, ast.Name) and isinstance(b, ast.Name) and a.id == b.id:
                return True
            return False
    return False


def _count_assertions_in_node(node: ast.AST, config: GateConfig) -> int:  # noqa: C901,PLR0911,PLR0912
    if isinstance(node, ast.Assert):
        if config.trivial_assertion_check and _is_trivial_assertion(node):
            return 0
        return 1

    if isinstance(node, (ast.With, ast.AsyncWith)):
        total = 0
        for item in node.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Call):
                name = _attr_name(ctx.func)
                if name in (
                    "pytest.raises",
                    "pytest.warns",
                    "pytest.deprecated_call",
                    "raises",
                    "warns",
                    "deprecated_call",
                ):
                    if config.count_pytest_raises_as_assertion:
                        total += 1
                elif name and (
                    name.split(".")[-1].startswith("assertRaises") or name.split(".")[-1].startswith("assertWarns")
                ):
                    total += 1
        total += sum(_count_assertions_in_node(n, config) for n in node.body)
        return total

    if isinstance(node, ast.Call):
        attr_part = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if attr_part and attr_part.startswith("assert"):
            if config.trivial_assertion_check and _is_trivial_assertion(node):
                return 0
            return 1
        name = _attr_name(node.func)
        attr = getattr(node.func, "attr", "")
        if name and (
            name.startswith("assert")
            or (isinstance(attr, str) and attr.startswith("assert"))
            or name in ("pytest.fail", "self.fail")
            or name in config.custom_assertion_helpers
        ):
            return 1
        # Recurse into args for nested expressions
        total = 0
        for arg in node.args:
            total += _count_assertions_in_node(arg, config)
        for kw in node.keywords:
            total += _count_assertions_in_node(kw.value, config)
        return total

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return 0  # Do NOT recurse into nested defs

    total = 0
    for child in ast.iter_child_nodes(node):
        total += _count_assertions_in_node(child, config)
    return total


def _func_in_diff(func: ast.FunctionDef | ast.AsyncFunctionDef, added_lines: set[int]) -> bool:
    end = getattr(func, "end_lineno", None) or func.lineno
    return bool(set(range(func.lineno, end + 1)) & added_lines)


def _run_assert_check(config: GateConfig, repo_root: Path) -> AssertResult:  # noqa: C901,PLR0912,PLR0915
    try:
        diff_text = _git_run(["diff", "--cached", "--unified=0"], cwd=repo_root)
    except subprocess.CalledProcessError:
        return AssertResult(internal_error="git diff --cached failed")

    added_by_file = parse_added_lines(diff_text)
    if not added_by_file:
        return AssertResult(findings=[])

    def _glob_match(path: str, pattern: str) -> bool:
        i = 0
        parts: list[str] = []
        while i < len(pattern):
            if pattern[i : i + 3] == "**/":
                parts.append("(?:.*/)?")
                i += 3
            elif pattern[i : i + 2] == "**":
                parts.append(".*")
                i += 2
            elif pattern[i] == "*":
                parts.append("[^/]*")
                i += 1
            elif pattern[i] == "?":
                parts.append("[^/]")
                i += 1
            else:
                parts.append(re.escape(pattern[i]))
                i += 1
        return bool(re.match("^" + "".join(parts) + "$", path))

    def is_test_file(path: str) -> bool:
        for pat in config.test_file_patterns:
            if _glob_match(path, pat):
                return True
        return False

    test_files = [p for p in added_by_file if is_test_file(p)]
    prod_files = [p for p in added_by_file if p.endswith(".py") and not is_test_file(p)]

    # Collect added public prod symbols once — both mock-of-UUT and
    # missing-test-reference checks consume this set.
    added_prod_symbols: dict[str, tuple[str, int]] = {}
    if (config.mock_of_uut_check or config.missing_test_reference_check) and prod_files:
        added_prod_symbols = _collect_added_prod_symbols(prod_files, added_by_file, repo_root)

    findings: list[AssertFinding] = []
    test_sources: dict[str, str] = {}

    for path in test_files:
        try:
            staged_src = _git_run(["show", f":{path}"], cwd=repo_root)
        except subprocess.CalledProcessError:
            continue
        try:
            tree = ast.parse(staged_src, filename=path)
        except SyntaxError as exc:
            return AssertResult(setup_error=f"{path}:{exc.lineno}: SyntaxError: {exc.msg}")

        test_sources[path] = staged_src
        added_lines = added_by_file.get(path, set())
        for func in _iter_test_funcs(tree):
            if not _func_in_diff(func, added_lines):
                continue
            if _has_skip_decorator(func):
                continue
            if _count_assertions(func, config) < config.min_assertions_per_test:
                findings.append(AssertFinding(path, func.name, func.lineno, kind="no_assertion"))
            if config.mock_of_uut_check and added_prod_symbols:
                mocked = _detect_mocked_uut_in_func(func, set(added_prod_symbols))
                if mocked is not None:
                    findings.append(
                        AssertFinding(
                            path,
                            func.name,
                            func.lineno,
                            kind="mocks_unit_under_test",
                            detail=f"patches {mocked}",
                        )
                    )

    if config.missing_test_reference_check and added_prod_symbols:
        findings.extend(_detect_missing_test_reference(added_prod_symbols, test_sources))

    return AssertResult(findings=findings)


def _collect_added_prod_symbols(
    prod_files: list[str],
    added_by_file: dict[str, set[int]],
    repo_root: Path,
) -> dict[str, tuple[str, int]]:
    """Return {symbol_name: (prod_file_path, def_line)} for public def/class
    declarations whose def-line lies in this commit's added lines.

    Methods of `class TestX` are NOT collected (those are tests, not units
    under test). Methods of any other class ARE collected — they're units.
    Underscore-prefixed names are excluded (private helpers).
    """
    symbols: dict[str, tuple[str, int]] = {}
    for path in prod_files:
        try:
            src = _git_run(["show", f":{path}"], cwd=repo_root)
            tree = ast.parse(src, filename=path)
        except (subprocess.CalledProcessError, SyntaxError):
            continue
        added = added_by_file.get(path, set())
        for top in tree.body:
            if isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not top.name.startswith("_") and top.lineno in added:
                    symbols.setdefault(top.name, (path, top.lineno))
            elif isinstance(top, ast.ClassDef):
                if not top.name.startswith("_") and top.lineno in added:
                    symbols.setdefault(top.name, (path, top.lineno))
                if top.name.startswith("Test"):
                    continue  # methods of TestFoo are tests, not units
                for sub in top.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not sub.name.startswith("_") and sub.lineno in added:
                            symbols.setdefault(sub.name, (path, sub.lineno))
    return symbols


def _extract_patch_target(node: ast.AST) -> str | None:
    """Extract the literal-string first arg from a `patch(...)` /
    `patch.object(M, "...")` / `mocker.patch(...)` / `mocker.patch.object`
    call. Returns None for non-Call nodes, non-patch callees, or
    non-string-literal arguments (we can't statically resolve those).
    """
    if not isinstance(node, ast.Call):
        return None
    name = _attr_name(node.func)
    if name is None:
        return None
    last = name.rsplit(".", 1)[-1]
    second_last = name.rsplit(".", 2)[-2] if "." in name else ""
    # patch("X.Y") / mocker.patch("X.Y") / mock.patch("X.Y")
    if last == "patch" and node.args:
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    # patch.object(M, "X") / mocker.patch.object(M, "X")
    if last == "object" and second_last == "patch" and len(node.args) >= 2:
        arg = node.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
    return None


def _has_mock_behavioral_assertion(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True if the function body contains a `<x>.assert_called*` /
    `<x>.assert_not_called*` / `<x>.called` / `<x>.call_count` /
    `<x>.call_args*` reference — i.e. the test asserts ON the mock
    object itself. Such tests use the mock AS the assertion target,
    not as silent bypass of the UUT.
    """
    for node in ast.walk(func):
        if isinstance(node, ast.Attribute):
            attr = node.attr
            if (
                attr.startswith("assert_called")
                or attr.startswith("assert_not_called")
                or attr in ("called", "call_count", "call_args", "call_args_list")
            ):
                return True
    return False


def _detect_mocked_uut_in_func(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    added_prod_symbol_names: set[str],
) -> str | None:
    """Walk decorators + function body for patch calls whose final segment
    matches an added prod symbol THAT ALSO appears as a substring in the
    test function's own name. Returns the patched target string on the
    first match (one finding per test func max), else None.

    The `<symbol> in func.name` filter cuts the false-positive rate: a
    test like `test_main_preflight_gate_zero_proceeds` patching
    `run_gate` is testing the *caller* (main()), not run_gate itself —
    that's a legitimate dependency mock, not "mocks UUT".

    Tests that assert on the mock object (`m.assert_called`, `m.called`,
    `m.call_count`, …) are also exempt — the mock is the assertion
    target, not a silent UUT bypass.
    """
    relevant = {s for s in added_prod_symbol_names if s and s in func.name}
    if not relevant:
        return None
    if _has_mock_behavioral_assertion(func):
        return None
    for dec in func.decorator_list:
        target = _extract_patch_target(dec)
        if target is not None and target.rsplit(".", 1)[-1] in relevant:
            return target
    for sub in ast.walk(func):
        if isinstance(sub, (ast.With, ast.AsyncWith)):
            for item in sub.items:
                target = _extract_patch_target(item.context_expr)
                if target is not None and target.rsplit(".", 1)[-1] in relevant:
                    return target
        elif isinstance(sub, ast.Call):
            target = _extract_patch_target(sub)
            if target is not None and target.rsplit(".", 1)[-1] in relevant:
                return target
    return None


def _detect_missing_test_reference(
    added_prod_symbols: dict[str, tuple[str, int]],
    test_sources: dict[str, str],
) -> list[AssertFinding]:
    """Emit a finding for each new public prod symbol whose name appears
    in NO test source in the diff. Word-boundary regex match avoids
    substring false positives (e.g. `process` matching `processed`).
    """
    findings: list[AssertFinding] = []
    joined = "\n".join(test_sources.values())
    for name, (path, lineno) in sorted(added_prod_symbols.items()):
        if not re.search(rf"\b{re.escape(name)}\b", joined):
            findings.append(
                AssertFinding(
                    path=path,
                    function_name=name,
                    def_line=lineno,
                    kind="missing_test_reference",
                    detail="no test in this diff references this symbol by name",
                )
            )
    return findings


# ---------------------------------------------------------------------------
# Assert check — JS/TS
# ---------------------------------------------------------------------------


def _run_assert_check_js(config: GateConfig, repo_root: Path) -> AssertResult:  # noqa: C901,PLR0911,PLR0912,PLR0915
    if not config.javascript_assert_enabled:
        return AssertResult()

    try:
        diff_text = _git_run(["diff", "--cached", "--unified=0"], cwd=repo_root)
    except subprocess.CalledProcessError:
        return AssertResult(internal_error="git diff --cached failed")

    added_by_file = parse_added_lines(diff_text)
    if not added_by_file:
        return AssertResult(findings=[])

    js_patterns = (
        "**/*.test.js",
        "**/*.test.ts",
        "**/*.test.jsx",
        "**/*.test.tsx",
        "**/*.spec.js",
        "**/*.spec.ts",
        "**/*.spec.jsx",
        "**/*.spec.tsx",
        "**/tests/**/*.js",
        "**/tests/**/*.ts",
        "**/tests/**/*.tsx",
        "**/tests/**/*.jsx",
        "**/__tests__/**/*.js",
        "**/__tests__/**/*.ts",
        "**/__tests__/**/*.tsx",
        "**/__tests__/**/*.jsx",
    )

    def _glob_match_js(path: str, pattern: str) -> bool:
        # `js_patterns` is hardcoded above — only `**/`, `*`, and literals are
        # used. Bare `**` and `?` would need to be re-added if patterns become
        # config-driven.
        i = 0
        parts: list[str] = []
        while i < len(pattern):
            if pattern[i : i + 3] == "**/":
                parts.append("(?:.*/)?")
                i += 3
            elif pattern[i] == "*":
                parts.append("[^/]*")
                i += 1
            else:
                parts.append(re.escape(pattern[i]))
                i += 1
        return bool(re.match("^" + "".join(parts) + "$", path))

    def is_js_test_file(path: str) -> bool:
        for pat in js_patterns:
            if _glob_match_js(path, pat):
                return True
        return False

    test_files = [p for p in added_by_file if is_js_test_file(p)]
    if not test_files:
        return AssertResult(findings=[])

    files_payload: list[dict[str, Any]] = []
    for path in test_files:
        try:
            staged_src = _git_run(["show", f":{path}"], cwd=repo_root)
        except subprocess.CalledProcessError:
            continue
        files_payload.append(
            {
                "path": path,
                "source": staged_src,
                "added_lines": sorted(added_by_file.get(path, [])),
            }
        )

    if not files_payload:
        return AssertResult(findings=[])

    helper = Path(__file__).parent / "preflight_gate_ts_helper.js"
    payload = {
        "files": files_payload,
        "config": {
            "min_assertions_per_test": config.min_assertions_per_test,
            "test_function_names": list(config.javascript_test_function_names),
            "custom_assertion_helpers": list(config.javascript_custom_assertion_helpers),
        },
    }

    # NOTE: order matters — PREFLIGHT_TARGET_CWD must come AFTER **os.environ
    # so the gate's own value wins over an inherited shell export. The Node
    # helper relies on this var to resolve @typescript-eslint/parser from the
    # target repo's node_modules.
    env = {**os.environ, "PREFLIGHT_TARGET_CWD": str(repo_root)}
    try:
        result = subprocess.run(
            ["node", str(helper)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=60,
            env=env,
        )
    except FileNotFoundError:
        return AssertResult(
            setup_error=(
                "Node.js required for JS/TS assert check — install Node ≥18 "
                "or set [tool.code_review.coverage_gate.assert_counter.javascript].enabled = false"
            )
        )
    except subprocess.TimeoutExpired:
        return AssertResult(internal_error="JS assert helper timed out after 60s")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Cannot find module" in stderr and "typescript-eslint" in stderr:
            return AssertResult(
                setup_error="@typescript-eslint/parser not found — npm install --save-dev @typescript-eslint/parser"
            )
        return AssertResult(internal_error=f"JS assert helper failed: {stderr}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return AssertResult(internal_error=f"JS helper JSON parse error: {exc}")

    findings = [AssertFinding(f["path"], f["function_name"], f["def_line"]) for f in data.get("findings", [])]
    return AssertResult(findings=findings)


# ---------------------------------------------------------------------------
# Top-level gate
# ---------------------------------------------------------------------------


def _diff_has_production_python(diff_text: str, config: GateConfig) -> bool:
    added_by_file = parse_added_lines(diff_text)
    for path, _lines in added_by_file.items():
        if not path.endswith(".py"):
            continue
        # Skip test files
        is_test = False
        for pat in config.test_file_patterns:
            i = 0
            parts: list[str] = []
            p = pat
            while i < len(p):
                if p[i : i + 3] == "**/":
                    parts.append("(?:.*/)?")
                    i += 3
                elif p[i : i + 2] == "**":
                    parts.append(".*")
                    i += 2
                elif p[i] == "*":
                    parts.append("[^/]*")
                    i += 1
                elif p[i] == "?":
                    parts.append("[^/]")
                    i += 1
                else:
                    parts.append(re.escape(p[i]))
                    i += 1
            if re.match("^" + "".join(parts) + "$", path):
                is_test = True
                break
        if not is_test:
            return True
    return False


def run_gate(repo_root: Path | None = None) -> int:  # noqa: C901,PLR0911,PLR0912,PLR0915
    """Run the pre-flight gate.

    Returns:
        0 — ok
        1 — findings (coverage or assert failures)
        2 — setup error (missing report, stale report, etc.)
        3 — internal error (crash, diff-cover crash, etc.)
    """
    root = repo_root or Path.cwd()
    config = _load_config(root)

    if not config.enabled:
        return 0

    if os.environ.get("CODE_REVIEW_SKIP_GATE", "") == "1":
        if sys.stderr.isatty():
            print(_yellow("[preflight] CODE_REVIEW_SKIP_GATE=1 — gate bypassed"), file=sys.stderr)
        else:
            print("[preflight] CODE_REVIEW_SKIP_GATE=1 — gate bypassed", file=sys.stderr)
        return 0

    if config.config_setup_error or config.threshold_setup_error:
        msg = config.config_setup_error or config.threshold_setup_error
        if sys.stderr.isatty():
            print(_red(f"[preflight] Setup error — {msg}"), file=sys.stderr)
        else:
            print(f"[preflight] Setup error — {msg}", file=sys.stderr)
        return 2

    skip_coverage = os.environ.get("CODE_REVIEW_SKIP_COVERAGE", "") == "1"
    skip_assert = os.environ.get("CODE_REVIEW_SKIP_ASSERT", "") == "1"

    if not config.coverage_check_enabled and not config.assert_check_enabled:
        return 0

    if os.environ.get("CODE_REVIEW_GATE_VERBOSE", "") == "1":
        print(
            f"[preflight] verbose: enabled={config.enabled} threshold={config.coverage_threshold} "
            f"coverage_check={config.coverage_check_enabled} assert_check={config.assert_check_enabled} "
            f"compare_branch={config.compare_branch}",
            file=sys.stderr,
        )

    coverage_result: CoverageResult | None = None
    assert_result: AssertResult | None = None

    if config.coverage_check_enabled and not skip_coverage:
        try:
            diff_text = _git_run(["diff", "--cached", "--unified=0"], cwd=root)
        except subprocess.CalledProcessError:
            return 3
        if _diff_has_production_python(diff_text, config):
            coverage_result = _run_coverage_check(config, root)
        else:
            coverage_result = None

    if config.assert_check_enabled and not skip_assert:
        py_result = _run_assert_check(config, root)
        js_result = _run_assert_check_js(config, root) if config.javascript_assert_enabled else AssertResult()

        # Merge results
        setup_errors = [r.setup_error for r in (py_result, js_result) if r.setup_error]
        internal_errors = [r.internal_error for r in (py_result, js_result) if r.internal_error]
        all_findings = list(py_result.findings) + list(js_result.findings)

        if setup_errors:
            assert_result = AssertResult(setup_error="; ".join(setup_errors))
        elif internal_errors:
            assert_result = AssertResult(internal_error="; ".join(internal_errors))
        else:
            assert_result = AssertResult(findings=all_findings)

    # Aggregation
    any_setup_error = (coverage_result is not None and coverage_result.setup_error is not None) or (
        assert_result is not None and assert_result.setup_error is not None
    )
    any_internal_error = (coverage_result is not None and coverage_result.internal_error is not None) or (
        assert_result is not None and assert_result.internal_error is not None
    )

    if any_setup_error:
        msg_parts: list[str] = []
        if coverage_result and coverage_result.setup_error:
            msg_parts.append(f"Coverage: {coverage_result.setup_error}")
        if assert_result and assert_result.setup_error:
            msg_parts.append(f"Assert: {assert_result.setup_error}")
        full_msg = "; ".join(msg_parts)
        if sys.stderr.isatty():
            print(_red(f"[preflight] Setup error — {full_msg}"), file=sys.stderr)
        else:
            print(f"[preflight] Setup error — {full_msg}", file=sys.stderr)
        return 2

    if any_internal_error:
        msg_parts = []
        if coverage_result and coverage_result.internal_error:
            msg_parts.append(f"Coverage: {coverage_result.internal_error}")
        if assert_result and assert_result.internal_error:
            msg_parts.append(f"Assert: {assert_result.internal_error}")
        full_msg = "; ".join(msg_parts)
        if config.fail_open_on_error:
            if sys.stderr.isatty():
                print(_yellow(f"[preflight] Internal error (fail-open) — {full_msg}"), file=sys.stderr)
            else:
                print(f"[preflight] Internal error (fail-open) — {full_msg}", file=sys.stderr)
            return 0
        if sys.stderr.isatty():
            print(_red(f"[preflight] Internal error — {full_msg}"), file=sys.stderr)
        else:
            print(f"[preflight] Internal error — {full_msg}", file=sys.stderr)
        return 3

    findings_present = (coverage_result is not None and coverage_result.findings) or (
        assert_result is not None and assert_result.findings
    )

    if findings_present:
        report = _format_report(coverage_result, assert_result, config)
        print(report, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(run_gate())
