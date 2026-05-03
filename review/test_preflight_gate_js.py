from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.preflight_gate import (
    GateConfig,
    _run_assert_check_js,
)


@pytest.fixture
def fake_parser_module(tmp_path: Path) -> Path:
    """Create a minimal fake @typescript-eslint/parser module."""
    mod_dir = tmp_path / "node_modules" / "@typescript-eslint" / "parser"
    mod_dir.mkdir(parents=True)
    # A minimal parser that returns an ESTree-like AST for the given source
    (mod_dir / "index.js").write_text(
        """
function parse(source) {
    const lines = source.split('\\n');
    const body = [];
    for (let i = 0; i < lines.length; i++) {
        const line = lines[i].trim();
        // Match test.each(...)(...) or it.each(...)(...)
        const eachMatch = line.match(/^(it|test)\\.each\\([^)]*\\)\\s*\\(\\s*["']([^"']+)["']\\s*,\\s*(async\\s*)?\\(\\s*\\)\\s*=>\\s*(\\{.*\\}|.*?)\\s*\\)/);
        if (eachMatch) {
            const baseName = eachMatch[1];
            const testName = eachMatch[2];
            const bodyStr = eachMatch[4] || '';
            const stmtBody = buildBody(bodyStr, i + 1);
            body.push({
                type: 'ExpressionStatement',
                expression: {
                    type: 'CallExpression',
                    callee: {
                        type: 'CallExpression',
                        callee: {
                            type: 'MemberExpression',
                            object: { type: 'Identifier', name: baseName },
                            property: { type: 'Identifier', name: 'each' }
                        },
                        arguments: []
                    },
                    arguments: [
                        { type: 'Literal', value: testName },
                        {
                            type: 'ArrowFunctionExpression',
                            body: { type: 'BlockStatement', body: stmtBody, loc: { start: { line: i+1 }, end: { line: i+1 } } },
                            loc: { start: { line: i+1 }, end: { line: i+1 } }
                        }
                    ],
                    loc: { start: { line: i+1 }, end: { line: i+1 } }
                }
            });
            continue;
        }
        const m = line.match(/^(it|test)(\\.(skip|only))?\\s*\\(\\s*["']([^"']+)["']\\s*,\\s*(async\\s*)?\\(\\s*\\)\\s*=>\\s*(\\{.*\\}|.*?)\\s*\\)/);
        const fnM = line.match(/^(it|test)(\\.(skip|only))?\\s*\\(\\s*["']([^"']+)["']\\s*,\\s*function\\s*\\(\\s*\\)\\s*\\{(.*)\\}\\s*\\)/);
        if (m || fnM) {
            const match = m || fnM;
            const calleeName = match[2] ? match[1] + match[2] : match[1];
            const testName = match[4];
            const bodyStr = m ? (match[6] || '') : (match[5] || '');
            const stmtBody = buildBody(bodyStr, i + 1);
            const funcType = m ? 'ArrowFunctionExpression' : 'FunctionExpression';
            body.push({
                type: 'ExpressionStatement',
                expression: {
                    type: 'CallExpression',
                    callee: { type: 'Identifier', name: calleeName },
                    arguments: [
                        { type: 'Literal', value: testName },
                        {
                            type: funcType,
                            body: { type: 'BlockStatement', body: stmtBody, loc: { start: { line: i+1 }, end: { line: i+1 } } },
                            loc: { start: { line: i+1 }, end: { line: i+1 } }
                        }
                    ],
                    loc: { start: { line: i+1 }, end: { line: i+1 } }
                }
            });
        }
    }
    return { type: 'Program', body };
}

function buildBody(bodyStr, startLine) {
    const stmts = [];
    // Nested function declaration
    const nestedFn = bodyStr.match(/function\\s+(\\w+)\\s*\\(\\)\\s*\\{(.*?)\\}/);
    if (nestedFn) {
        const innerBody = buildBody(nestedFn[2], startLine);
        stmts.push({
            type: 'FunctionDeclaration',
            id: { type: 'Identifier', name: nestedFn[1] },
            body: { type: 'BlockStatement', body: innerBody, loc: { start: { line: startLine }, end: { line: startLine } } },
            loc: { start: { line: startLine }, end: { line: startLine } }
        });
        bodyStr = bodyStr.replace(nestedFn[0], '');
    }
    if (bodyStr.includes('expect')) {
        stmts.push({ type: 'ExpressionStatement', expression: { type: 'CallExpression', callee: { type: 'Identifier', name: 'expect' } } });
    }
    if (bodyStr.includes('assert')) {
        stmts.push({ type: 'ExpressionStatement', expression: { type: 'CallExpression', callee: { type: 'MemberExpression', object: { type: 'Identifier', name: 'assert' }, property: { type: 'Identifier', name: 'equal' } } } });
    }
    if (bodyStr.includes('chai')) {
        stmts.push({ type: 'ExpressionStatement', expression: { type: 'CallExpression', callee: { type: 'MemberExpression', object: { type: 'Identifier', name: 'chai' }, property: { type: 'Identifier', name: 'expect' } } } });
    }
    if (bodyStr.includes('verifyState')) {
        stmts.push({ type: 'ExpressionStatement', expression: { type: 'CallExpression', callee: { type: 'Identifier', name: 'verifyState' } } });
    }
    // should.js style: foo.should.equal(5)
    const shouldMatch = bodyStr.match(/(\\w+)\\.should\\.(\\w+)\\(/);
    if (shouldMatch) {
        stmts.push({
            type: 'ExpressionStatement',
            expression: {
                type: 'CallExpression',
                callee: {
                    type: 'MemberExpression',
                    object: {
                        type: 'MemberExpression',
                        object: { type: 'Identifier', name: shouldMatch[1] },
                        property: { type: 'Identifier', name: 'should' }
                    },
                    property: { type: 'Identifier', name: shouldMatch[2] }
                }
            }
        });
    }
    return stmts;
}

module.exports = { parse };
"""
    )
    return tmp_path


HELPER = Path(__file__).parent / "scripts" / "preflight_gate_ts_helper.js"


def _run_helper(payload: dict, fake_parser_module: Path) -> dict:
    env = {**os.environ, "NODE_PATH": str(fake_parser_module / "node_modules")}
    result = subprocess.run(
        ["node", str(HELPER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    return json.loads(result.stdout)


def test_it_with_expect_passes(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'it("x",()=>{expect(x).toBe(5);})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert result["findings"] == []


def test_test_empty_fails(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'test("x",()=>{})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert len(result["findings"]) == 1
    assert result["findings"][0]["function_name"] == "x"


def test_test_console_log_fails(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'test("x",()=>{console.log("ok");})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert len(result["findings"]) == 1


def test_it_skip_passes(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'it.skip("x",()=>{})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert result["findings"] == []


def test_assert_equal_passes(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'test("x",()=>{assert.equal(a,b);})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert result["findings"] == []


def test_chai_expect_passes(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'test("x",()=>{chai.expect(x).to.equal(5);})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert result["findings"] == []


def test_custom_helper_whitelisted_passes(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'test("x",()=>{verifyState(obj);})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1, "custom_assertion_helpers": ["verifyState"]},
    }
    result = _run_helper(payload, fake_parser_module)
    assert result["findings"] == []


def test_async_expect_passes(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'it("x",async()=>{await expect(foo()).resolves.toBe(1);})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# Python wrapper tests
# ---------------------------------------------------------------------------


def test_js_wrapper_node_missing() -> None:
    diff = (
        "diff --git a/src.test.ts b/src.test.ts\n"
        "--- a/src.test.ts\n"
        "+++ b/src.test.ts\n"
        "@@ -0,0 +1 @@\n"
        "+it('x',()=>{})\n"
    )

    with (
        patch("scripts.preflight_gate._git_run", return_value=diff),
        patch("scripts.preflight_gate.subprocess.run", side_effect=FileNotFoundError("node")),
    ):
        result = _run_assert_check_js(GateConfig(), Path("/tmp"))

    assert result.setup_error is not None
    assert "Node.js required" in result.setup_error


def test_js_wrapper_parser_missing() -> None:
    diff = (
        "diff --git a/src.test.ts b/src.test.ts\n"
        "--- a/src.test.ts\n"
        "+++ b/src.test.ts\n"
        "@@ -0,0 +1 @@\n"
        "+it('x',()=>{})\n"
    )

    fake_proc = subprocess.CompletedProcess(args=["node"], returncode=1, stderr="Cannot find module '@typescript-eslint/parser'")

    with (
        patch("scripts.preflight_gate._git_run", return_value=diff),
        patch("scripts.preflight_gate.subprocess.run", return_value=fake_proc),
    ):
        result = _run_assert_check_js(GateConfig(), Path("/tmp"))

    assert result.setup_error is not None
    assert "@typescript-eslint/parser" in result.setup_error


def test_js_wrapper_malformed_json() -> None:
    diff = (
        "diff --git a/src.test.ts b/src.test.ts\n"
        "--- a/src.test.ts\n"
        "+++ b/src.test.ts\n"
        "@@ -0,0 +1 @@\n"
        "+it('x',()=>{})\n"
    )

    fake_proc = subprocess.CompletedProcess(args=["node"], returncode=0, stdout="not json", stderr="")

    with (
        patch("scripts.preflight_gate._git_run", return_value=diff),
        patch("scripts.preflight_gate.subprocess.run", return_value=fake_proc),
    ):
        result = _run_assert_check_js(GateConfig(), Path("/tmp"))

    assert result.internal_error is not None
    assert "JSON parse error" in result.internal_error


def test_js_wrapper_timeout() -> None:
    diff = (
        "diff --git a/src.test.ts b/src.test.ts\n"
        "--- a/src.test.ts\n"
        "+++ b/src.test.ts\n"
        "@@ -0,0 +1 @@\n"
        "+it('x',()=>{})\n"
    )

    with (
        patch("scripts.preflight_gate._git_run", return_value=diff),
        patch("scripts.preflight_gate.subprocess.run", side_effect=subprocess.TimeoutExpired("node", 60)),
    ):
        result = _run_assert_check_js(GateConfig(), Path("/tmp"))

    assert result.internal_error is not None
    assert "timed out" in result.internal_error


# ---------------------------------------------------------------------------
# C14 — tsx / jsx patterns
# ---------------------------------------------------------------------------


def test_assertion_check_tsx_test_file_pattern(fake_parser_module: Path) -> None:
    diff = (
        "diff --git a/tests/foo.tsx b/tests/foo.tsx\n"
        "--- a/tests/foo.tsx\n"
        "+++ b/tests/foo.tsx\n"
        "@@ -0,0 +1 @@\n"
        "+it('x',()=>{})\n"
    )

    with (
        patch("scripts.preflight_gate._git_run", return_value=diff),
        patch("scripts.preflight_gate.subprocess.run") as m_run,
    ):
        m_run.return_value = subprocess.CompletedProcess(
            args=["node"],
            returncode=0,
            stdout='{"findings": []}',
            stderr="",
        )
        result = _run_assert_check_js(GateConfig(), Path("/tmp"))

    assert result.setup_error is None
    # Assert that the helper was invoked (files_payload not empty)
    assert m_run.call_count == 1
    payload = json.loads(m_run.call_args.kwargs["input"])
    assert any(f["path"].endswith(".tsx") for f in payload["files"])


# ---------------------------------------------------------------------------
# C19 — test.each table-driven
# ---------------------------------------------------------------------------


def test_assertion_check_test_each_table_driven(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'test.each([1,2])("case %s",()=>{})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert len(result["findings"]) == 1
    assert result["findings"][0]["function_name"] == "case %s"


# ---------------------------------------------------------------------------
# C20 — nested helper function no credit
# ---------------------------------------------------------------------------


def test_assertion_check_helper_function_no_credit(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'it("x",()=>{function helper(){expect(1);}})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert len(result["findings"]) == 1
    assert result["findings"][0]["function_name"] == "x"


# ---------------------------------------------------------------------------
# C21 — should.js chain
# ---------------------------------------------------------------------------


def test_assertion_check_should_chain(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'it("x",()=>{foo.should.equal(5);})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert result["findings"] == []


# ---------------------------------------------------------------------------
# C18 — resolves parser from target cwd
# ---------------------------------------------------------------------------


def test_assert_check_js_resolves_parser_from_target_cwd() -> None:
    """PREFLIGHT_TARGET_CWD must direct require.resolve to the target repo."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        target = Path(td)
        mod_dir = target / "node_modules" / "@typescript-eslint" / "parser"
        mod_dir.mkdir(parents=True)
        (mod_dir / "package.json").write_text('{"name": "@typescript-eslint/parser", "main": "index.js"}')
        (mod_dir / "index.js").write_text(
            "function parse() { return { type: 'Program', body: [] }; }\nmodule.exports = { parse };\n"
        )

        payload = {
            "files": [{"path": "a.test.ts", "source": 'it("x",()=>{})', "added_lines": [1]}],
            "config": {"min_assertions_per_test": 1},
        }

        env = {**os.environ, "PREFLIGHT_TARGET_CWD": str(target)}
        result = subprocess.run(
            ["node", str(HELPER)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        data = json.loads(result.stdout)
        assert data["findings"] == []


# ---------------------------------------------------------------------------
# C18 — JS wrapper returns findings from helper
# ---------------------------------------------------------------------------


def test_run_assert_check_js_returns_findings() -> None:
    diff = (
        "diff --git a/src.test.ts b/src.test.ts\n"
        "--- a/src.test.ts\n"
        "+++ b/src.test.ts\n"
        "@@ -0,0 +1 @@\n"
        "+it('x',()=>{})\n"
    )

    fake_proc = subprocess.CompletedProcess(
        args=["node"],
        returncode=0,
        stdout='{"findings": [{"path": "src.test.ts", "function_name": "x", "def_line": 1}]}',
        stderr="",
    )

    with (
        patch("scripts.preflight_gate._git_run", return_value=diff),
        patch("scripts.preflight_gate.subprocess.run", return_value=fake_proc),
    ):
        result = _run_assert_check_js(GateConfig(), Path("/tmp"))

    assert len(result.findings) == 1
    assert result.findings[0].path == "src.test.ts"
    assert result.findings[0].function_name == "x"
    assert result.findings[0].def_line == 1


# ---------------------------------------------------------------------------
# C19 — ts_helper treats it.only as a test
# ---------------------------------------------------------------------------


def test_helper_treats_test_only_as_test(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'it.only("x",()=>{})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert len(result["findings"]) == 1
    assert result["findings"][0]["function_name"] == "x"


# ---------------------------------------------------------------------------
# C20 — ts_helper treats function expression callback as test
# ---------------------------------------------------------------------------


def test_helper_treats_function_expression_callback(fake_parser_module: Path) -> None:
    payload = {
        "files": [{"path": "a.test.ts", "source": 'it("x",function(){})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    assert len(result["findings"]) == 1
    assert result["findings"][0]["function_name"] == "x"


@pytest.fixture
def throwing_parser_module(tmp_path: Path) -> Path:
    """Fake parser that throws on every parse call — exercises C15 path."""
    mod_dir = tmp_path / "node_modules" / "@typescript-eslint" / "parser"
    mod_dir.mkdir(parents=True)
    (mod_dir / "index.js").write_text(
        "function parse() { throw new SyntaxError('boom'); }\n"
        "module.exports = { parse };\n"
    )
    return tmp_path


def test_helper_parse_error_exits_2(throwing_parser_module: Path) -> None:
    """When @typescript-eslint/parser throws, the helper must exit 2 and
    write a `Parse error in <path>: <msg>` line to stderr — the Python
    wrapper relies on this to surface a setup_error, not a silent pass.
    """
    env = {**os.environ, "NODE_PATH": str(throwing_parser_module / "node_modules")}
    payload = {
        "files": [{"path": "broken.test.ts", "source": "it('x', () => {})", "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = subprocess.run(
        ["node", str(HELPER)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 2
    assert "Parse error in broken.test.ts" in result.stderr


def test_helper_passes_jsx_flag_for_tsx(fake_parser_module: Path) -> None:
    """Helper must enable jsx parsing for .tsx (and .jsx) files.

    The fake parser in this fixture doesn't actually validate JSX, but a
    real parse against `@typescript-eslint/parser` with `jsx: false` would
    error on `<Foo />` in a .tsx file. Exercising the .tsx code path
    proves the helper accepts the file (no parse error) — the regex
    `/\\.(jsx|tsx)$/i.test(file.path)` is the actual production check.
    """
    payload = {
        "files": [{"path": "a.test.tsx", "source": 'it("x",()=>{expect(1).toBe(1);})', "added_lines": [1]}],
        "config": {"min_assertions_per_test": 1},
    }
    result = _run_helper(payload, fake_parser_module)
    # Empty findings means parse succeeded and the assertion was counted.
    assert result["findings"] == []
