/**
 * Pre-flight gate JS/TS assert detector.
 *
 * Reads stdin JSON: {files: [{path, source, added_lines: [int]}], config: {...}}
 * Writes stdout JSON: {findings: [{path, function_name, def_line}]}
 *
 * Peer dependency: @typescript-eslint/parser (expected in target repo node_modules).
 */

const path = require("path");

const targetCwd = process.env.PREFLIGHT_TARGET_CWD || process.cwd();
let parser;
try {
  const parserPath = require.resolve("@typescript-eslint/parser", { paths: [targetCwd, path.join(targetCwd, "node_modules")] });
  parser = require(parserPath);
} catch (e) {
  console.error("Cannot find module '@typescript-eslint/parser' — " + e.message);
  process.exit(2);
}

function main() {
  let input = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => { input += chunk; });
  process.stdin.on("end", () => {
    let payload;
    try {
      payload = JSON.parse(input);
    } catch (e) {
      console.error("Invalid JSON on stdin:", e.message);
      process.exit(2);
    }

    const files = payload.files || [];
    const cfg = payload.config || {};
    const minAssertions = cfg.min_assertions_per_test || 1;
    const testNames = new Set(cfg.test_function_names || ["it", "test", "test.skip", "test.only", "it.skip", "it.only", "test.each", "it.each"]);
    const customHelpers = new Set(cfg.custom_assertion_helpers || []);

    const findings = [];

    for (const file of files) {
      let ast;
      const isJsx = /\.(jsx|tsx)$/i.test(file.path || "");
      try {
        ast = parser.parse(file.source, {
          ecmaVersion: "latest",
          sourceType: "module",
          range: true,
          loc: true,
          jsx: isJsx,
          filePath: file.path,
        });
      } catch (e) {
        console.error(`Parse error in ${file.path}: ${e.message}`);
        process.exit(2);
      }

      const added = new Set(file.added_lines || []);

      for (const node of walk(ast)) {
        if (node.type === "CallExpression") {
          const name = calleeName(node.callee);
          if (!name) continue;

          const isSkipped = name.endsWith(".skip") || name.startsWith("x");
          const baseName = isSkipped ? "" : name;
          if (!testNames.has(name) && !testNames.has(baseName)) continue;
          if (isSkipped) continue;

          let bodyNode = null;
          let funcName = "<anonymous>";

          const firstArg = node.arguments[0];
          if (firstArg && firstArg.type === "Literal" && typeof firstArg.value === "string") {
            funcName = firstArg.value;
          }

          const secondArg = node.arguments[1];
          if (secondArg) {
            if (secondArg.type === "ArrowFunctionExpression" || secondArg.type === "FunctionExpression") {
              bodyNode = secondArg.body;
            }
          }

          if (!bodyNode) continue;

          const startLine = bodyNode.loc ? bodyNode.loc.start.line : node.loc.start.line;
          const endLine = bodyNode.loc ? bodyNode.loc.end.line : node.loc.end.line;

          if (!intersects(startLine, endLine, added)) continue;

          const count = countAssertions(bodyNode, customHelpers);
          if (count < minAssertions) {
            findings.push({
              path: file.path,
              function_name: funcName,
              def_line: node.loc.start.line,
            });
          }
        }
      }
    }

    console.log(JSON.stringify({ findings }));
  });
}

function calleeName(callee) {
  if (!callee) return null;
  if (callee.type === "Identifier") return callee.name;
  if (callee.type === "MemberExpression") {
    const obj = calleeName(callee.object);
    const prop = callee.property ? (callee.property.name || callee.property.value) : "";
    if (obj && prop) return `${obj}.${prop}`;
    return prop || obj;
  }
  if (callee.type === "CallExpression") {
    const base = calleeName(callee.callee);
    if (!base) return null;
    return base;
  }
  return null;
}

function* walk(node) {
  if (!node || typeof node !== "object") return;
  yield node;
  for (const key of Object.keys(node)) {
    if (key === "parent") continue;
    const child = node[key];
    if (Array.isArray(child)) {
      for (const c of child) yield* walk(c);
    } else if (child && typeof child === "object" && child.type) {
      yield* walk(child);
    }
  }
}

function intersects(startLine, endLine, added) {
  for (let i = startLine; i <= endLine; i++) {
    if (added.has(i)) return true;
  }
  return false;
}

function chainContains(node, propName) {
  if (!node) return false;
  if (node.type === "MemberExpression") {
    if (node.property && (node.property.name === propName || node.property.value === propName)) {
      return true;
    }
    return chainContains(node.object, propName);
  }
  return false;
}

function* walkSkipNested(node) {
  if (!node || typeof node !== "object") return;
  yield node;
  if (
    node.type === "FunctionDeclaration" ||
    node.type === "FunctionExpression" ||
    node.type === "ArrowFunctionExpression" ||
    node.type === "MethodDefinition"
  ) {
    return;
  }
  for (const key of Object.keys(node)) {
    if (key === "parent") continue;
    const child = node[key];
    if (Array.isArray(child)) {
      for (const c of child) yield* walkSkipNested(c);
    } else if (child && typeof child === "object" && child.type) {
      yield* walkSkipNested(child);
    }
  }
}

function countAssertions(node, customHelpers) {
  let count = 0;
  for (const n of walkSkipNested(node)) {
    if (n.type === "CallExpression") {
      const name = calleeName(n.callee);
      if (!name) continue;
      if (
        name.startsWith("assert") ||
        name.startsWith("expect") ||
        name.startsWith("chai.") ||
        name.startsWith("should.") ||
        name === "fail" ||
        customHelpers.has(name)
      ) {
        count++;
        continue;
      }
      if (chainContains(n.callee, "should")) {
        count++;
        continue;
      }
    }
  }
  return count;
}

main();
