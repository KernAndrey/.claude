# Kimi CLI integration with Claude Code

Hybrid workflow: Claude Code orchestrates, Kimi CLI handles delegated coding /
read-only audit work. **Delegation goes through a skill, not a subagent** —
the parent Claude (opus/sonnet) follows a deterministic Bash recipe instead
of spawning a second LLM with agency. Kimi runs in the background so a long
audit doesn't block your turn.

## What was installed

| Path | Purpose |
|---|---|
| `~/.kimi/lib/build_context.py` | Assembles `~/.claude/CLAUDE.md` + `<project>/CLAUDE.md` + `.claude/rules/*` + `.claude/commands/*` into a Markdown blob, mtime-cached at `~/.kimi/cache/`. |
| `~/.kimi/hooks/safety_shell.py` | PreToolUse / `Shell` — blocks destructive commands (rm -rf system paths, dd to disks, force-push to main, drop database non-test, etc.). |
| `~/.kimi/hooks/protect_secrets.py` | PreToolUse / `WriteFile|StrReplaceFile` — blocks writes to `.env`, `*.pem`, `~/.ssh/`, `~/.kimi/`, `~/.claude/`, etc.; scans content for credential-shaped strings. |
| `~/.kimi/hooks/format_python.py` | PostToolUse / `WriteFile|StrReplaceFile` — runs `ruff format` then `ruff check --fix --unsafe-fixes` on `.py` edits. |
| `~/.kimi/config.toml` | Three `[[hooks]]` blocks; existing `[providers.*]`, `[models.*]`, etc. preserved byte-for-byte. |
| `~/.claude/skills/kimi/SKILL.md` | Auto-trigger on "use kimi" / "delegate to kimi" / "let kimi do it". Body is a deterministic Bash recipe — no LLM-in-the-middle. |
| `~/.claude/commands/kimi.md` | `/kimi <task>` slash command — explicit invocation; thin wrapper that points at the skill. |

## Why a skill, not a subagent

The first iteration used `~/.claude/agents/kimi-{coder,context}.md` subagents.
Live-tested: the subagent (haiku) interpreted "describe the project" as
"explore and answer", made 15 tool calls reading files itself, and **never
once invoked kimi**. Sessions dir at `~/.kimi/sessions/` confirmed: zero
new entries.

Root cause: putting an LLM as a wrapper layer creates non-deterministic
behavior — even with strict prompts, the model can decide to "help" instead
of strictly delegating. A skill is a prompt for the parent Claude
(opus/sonnet, much stronger instruction-following), and the skill body is a
literal Bash recipe with hard rules ("no Read/Grep/Glob before kimi").

## How rule injection works

kimi natively reads `.claude/skills/` (cross-CLI brand discovery via
`merge_all_available_skills = true`) and `<project>/AGENTS.md` (auto-injected
into the system prompt). It does **not** read `CLAUDE.md`, `.claude/rules/`,
or `.claude/commands/`.

The skill fills that gap. Before every kimi call, the recipe runs
`~/.kimi/lib/build_context.py <project_root>` which assembles:

1. `~/.claude/CLAUDE.md` (user-level preferences)
2. `<project>/CLAUDE.md` (project guide)
3. `<project>/.claude/rules/*.md` (sorted, project rules)
4. `<project>/.claude/commands/*.md` (sorted, command awareness)

into a single Markdown blob. The blob is **prepended** to the user's task
text and passed to kimi via `--prompt`.

`.claude/skills/` is **not** in the blob — kimi reads them itself. `.claude/agents/`
is **not** included — Claude-Code-specific.

**Caching**: by `(project_root, sorted(input_paths, mtimes))` hash at
`~/.kimi/cache/`. Cache hit ~30 ms; mtime change → automatic invalidation;
old cache files for the same project are GC'd.

**80 KB cap**: lowest-priority sections dropped first (commands → rules →
user-level), with a `> NOTE:` line documenting what was truncated.

## How to use

- **Implicit (recommended)** — say "use kimi to add a docstring to
  `<file>:<func>`" or "delegate to kimi" or "ask kimi to summarize what
  `tms_core/invoicing/` does". Claude auto-loads the skill via description match.
- **Explicit** — `/kimi <task; include explicit file paths>`.
- **Background by default** — Claude fires kimi via `run_in_background: true`,
  tells you the log path (`/tmp/kimi-<ts>-<pid>.log`), then waits for the
  `=== KIMI_DONE rc=<N>` marker before presenting the result. You can ask
  "what's kimi doing?" between fire and finish to see `tail -30 $LOG`.

## How to verify it actually ran

The previous subagent design failed silently (no kimi sessions despite +15
tool uses). To confirm a real run:

```bash
ls -lat ~/.kimi/sessions/ | head -5      # new dir with current timestamp = ran
tail -50 ~/.kimi/logs/kimi.log           # latest entries from the kimi process
ls -lat /tmp/kimi-*.log 2>/dev/null      # per-invocation logs from the skill
```

If the skill's Bash recipe ran, you'll see:
- a `=== KIMI_START …` line in the background shell's stdout
- a fresh dir in `~/.kimi/sessions/`
- a `/tmp/kimi-<ts>-<pid>.log` file with kimi's actual output

If you see Claude doing `Read`/`Grep`/`Glob`/`ls` instead — the skill isn't
being followed; the rule-injection or background dispatch was skipped.
Re-read `~/.claude/skills/kimi/SKILL.md` and check Claude is loading it.

## Standalone smoke (independent of Claude)

```bash
# 1. kimi works
kimi --version

# 2. all scripts executable
for f in ~/.kimi/hooks/safety_shell.py ~/.kimi/hooks/protect_secrets.py \
         ~/.kimi/hooks/format_python.py ~/.kimi/lib/build_context.py; do
    test -x "$f" || echo "MISSING: $f"
done

# 3. config.toml valid TOML
python3 -c "import tomllib; tomllib.loads(open('$HOME/.kimi/config.toml').read())"

# 4. build_context output
~/.kimi/lib/build_context.py "$(pwd)" | head -20

# 5. Cache hit < 50 ms
time ~/.kimi/lib/build_context.py "$(pwd)" > /dev/null

# 6. Hooks deny destructive shell
echo '{"tool_input":{"command":"rm -rf /"}}' | ~/.kimi/hooks/safety_shell.py

# 7. format_python rewrites a .py
echo "x   =1" > /tmp/_kimi_fmt.py
echo '{"cwd":"/home/kern","tool_name":"WriteFile","tool_input":{"file_path":"/tmp/_kimi_fmt.py"}}' \
    | ~/.kimi/hooks/format_python.py
cat /tmp/_kimi_fmt.py    # → "x = 1"
rm /tmp/_kimi_fmt.py
```

## End-to-end smoke (manual, costs a few cents)

In a real project (say `/home/kern/projects/hubcraft-console`), open a fresh
Claude Code session, then:

```
use kimi to print "hello world" from a new file /tmp/hi.py
```

Expected:
1. Claude tells you "Kimi запущена в фоне, лог: `/tmp/kimi-<ts>-<pid>.log`".
2. After ~30–60 s, Claude presents kimi's output verbatim (the diff).
3. `ls ~/.kimi/sessions/` shows a new directory with the current timestamp.
4. `cat /tmp/hi.py` shows kimi's output (the file ruff-formatted by the
   PostToolUse hook).
5. `cat /tmp/kimi-<ts>-<pid>.log` shows kimi's full thought + final reply.

## Open caveats

- **Hooks are user-global.** They fire for every kimi session on this host.
  Project-local hooks aren't supported yet (upstream issue MoonshotAI/kimi-cli#785).
- **`kimi --quiet` skips approvals** (it implies `--print` which implies
  `--yolo`). The PreToolUse hooks (`safety_shell`, `protect_secrets`) are
  the only safety net under YOLO.
- **Direct `kimi` from a terminal bypasses rule injection.** If you run kimi
  yourself (not via the skill), kimi only sees `.claude/skills/` and
  `<project>/AGENTS.md` (none currently exists). To replicate the skill's
  behavior manually:
  ```bash
  ~/.kimi/lib/build_context.py "$(pwd)" > /tmp/ctx.md
  echo "your task..." > /tmp/task.md
  kimi --quiet --prompt "$(cat /tmp/ctx.md /tmp/task.md)"
  ```
- **Context blob capped at 80 KB.** If your `~/.claude/CLAUDE.md` plus
  project rules exceed this, lowest-priority sections are truncated with a
  `> NOTE:` annotation.
- **Generic across projects.** Project root is resolved via
  `git rev-parse --show-toplevel` at call time — works in any repo with the
  standard `CLAUDE.md` + `.claude/` layout, no per-project config.

## Uninstall

```bash
rm -rf ~/.kimi/hooks ~/.kimi/lib ~/.kimi/cache
# Then edit ~/.kimi/config.toml and remove the three [[hooks]] blocks
# under the "Claude-Code-aligned hooks" marker comment.
rm -rf ~/.claude/skills/kimi
rm ~/.claude/commands/kimi.md ~/.claude/KIMI_SETUP.md
```
