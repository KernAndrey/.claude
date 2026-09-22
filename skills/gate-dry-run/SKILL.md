---
name: gate-dry-run
description: >
  Run the full pre-commit gate — repo lint hook, gitleaks, semgrep, coverage/assert
  preflight, AI review including the chunked path — against the current index
  WITHOUT creating a commit. Use when the user wants to know whether staged changes
  would pass the commit gate: "прогони гейт без коммита", "dry-run гейта",
  "пройдёт ли коммит", "run the pre-commit review but don't commit", or /gate-dry-run.
user_invocable: true
argument_hint: "[commit message]"
---

# Gate dry run

Runs `~/.claude/git-hooks/pre-commit` — the same wrapper `git commit` runs, with its index snapshot/restore — from the repo root, then turns the output into a verdict. `run_gate.sh` and `wait_gate.sh` live next to this file.

<critical>
This skill ends with HEAD unchanged. It never runs `git commit`, `--no-verify`, a `core.hooksPath` override, `git stash`, or writes to `.review/approvals/` or `~/.claude/git-hooks/` — the guard hook refuses all of them anyway.

Exit code 0 is not "passed". Fail-open (EMPTY/ERROR/TIMEOUT), CRASH and SKIP all exit 0. Take the verdict from the marker, the log name and `gate.txt` using the table below.
</critical>

## Procedure

1. **Index.** `cd "$(git rev-parse --show-toplevel)" && git diff --cached --stat`.
   If nothing is staged, ask with AskUserQuestion which files to stage, then `git add <paths>` (named paths, never `-A`/`.`). Otherwise use the index as it is.

2. **Divergence warning — show before launching.**
   ```bash
   git status --porcelain | grep -vE '^[MADRCT] '
   env | grep -E '^CODE_REVIEW_(SKIP_GATE|SKIP_COVERAGE|SKIP_ASSERT|GATE_FAIL_OPEN|COMPARE_BRANCH)='
   ```
   - Unstaged or untracked entries: a real commit stashes them first. Here the repo lint hook and the coverage-freshness check see the working tree, so their result may differ from a real commit. Warn and continue; leave stash alone.
   - A set `SKIP_*` / `GATE_FAIL_OPEN` turns part of the gate off — say so prominently. `COMPARE_BRANCH` changes what counts as new code for coverage.

3. **Memory.** Run `free -h`. If swap is nearly full, warn: the chunked path starts every reviewer at once, and the harness kills heavy runs under memory pressure.

4. **Commit message.** The reviewer compares the diff with the message, so pass one.
   - `OUT=<scratchpad>/gate-$(date +%Y%m%d-%H%M%S)`; `mkdir -p "$OUT"`. One `$OUT` per run: every rerun (steps 8 and 10) starts again from here with a new `$OUT`; `run_gate.sh` refuses a used one.
   - Message = the skill argument if given; otherwise draft one from `git diff --cached`: conventional commit, English, subject ≤72 chars, no AI attribution.
   - Write it to `$OUT/msg.txt` and show it to the user.

5. **Launch detached** from the repo root (a Bash `run_in_background` task gets reaped on runs this long):
   ```bash
   setsid nohup ~/.claude/skills/gate-dry-run/run_gate.sh "$OUT" "$OUT/msg.txt" > "$OUT/launcher.log" 2>&1 < /dev/null & disown
   ```

6. **Wait** with the Monitor tool (load via ToolSearch): command `~/.claude/skills/gate-dry-run/wait_gate.sh "$OUT"`, `timeout_ms: 3600000`, `persistent: false`.
   - It prints the marker (`GATE_RC`, `VERDICT`, `LOG`, `LOG_COUNT`) when the run ends, or `PROCESS_GONE_WITHOUT_MARKER` / `LAUNCH_FAILED` when it died.
   - If the monitor itself times out, the run is still going: check `$OUT/marker`, then start the waiter again.

7. **Verdict.** Read `$OUT/marker`, `$OUT/gate.txt` and the `LOG` file; classify with the table.
   - Take findings from the log's review section — `gate.txt` can omit WARNINGs entirely even when the review produced them:
     ```bash
     awk '/^## Full diff/{exit} {print}' "$LOG" | grep -E '\[(CRITICAL|WARNING)\]'
     ```
     Stopping at `## Full diff` keeps tags quoted inside the diff out of the list.
   - With `LOG=none`, fall back to `grep -E '\[(CRITICAL|WARNING)\]' "$OUT/gate.txt"`.

8. **Manifest scaffold** (`VERDICT=BLOCK`, log says "manifest auto-scaffolded", `gate.txt` has `Scaffolded`):
   - Read the limits from `~/.claude/review/config.py` (`MAX_PROD_LINES`, `MAX_CHUNKS`) — every prose copy of these numbers has drifted.
   - Propose a chunk grouping following the manifest rules in `~/.claude/skills/commit/SKILL.md` ("Splitting rules"): group files by meaning, each chunk within the prod-line limit.
   - Show the proposal and ask with AskUserQuestion before writing `chunks:`. After approval, write them, validate with the commit skill's snippet, and go back to step 4 (new `$OUT`, same message).

9. **Report**, in this order:
   - Verdict in one line (from the table).
   - Every `[CRITICAL]` and `[WARNING]` with file:line.
   - Divergences from step 2 and the contents of `$OUT/env.txt` that are not `<unset>`.
   - Paths: `$OUT/gate.txt`, `LOG`, `.review/blocking.txt` (chunked path), `$OUT/review-backup` (if created).
   - If `.review/` changed: the chunked path rewrites its state and the scaffold step creates `manifest.yaml`. Say so — `review-backup/` is a copy of what was there before and is never restored automatically.
   - `$OUT/notes.txt`, when present: repeat each line. It records something that makes this run less faithful than a real commit.
   - Reminder: a later real commit runs the full review again; nothing carries over.

10. **Next step** — ask with AskUserQuestion; offer only the options that fit the verdict:
    - fix the CRITICALs (with tests) and rerun the dry run;
    - regenerate `coverage.xml` and rerun (preflight setup error);
    - rerun as is (killed / not reviewed / CRASH);
    - stop here.

## Verdict table

| Marker / output | Meaning |
|---|---|
| `GATE_RC=0`, `VERDICT=OK` | Passed. Any `[WARNING]` in the log's review section (step 7) → passed with WARNINGs, even when `gate.txt` shows none. |
| `GATE_RC=1`, `VERDICT=BLOCK`, upheld findings | Blocked by the AI review. |
| `VERDICT=BLOCK` + "chunked-review infrastructure failure" | **Not reviewed** — every chunked reviewer failed. Not a code finding; see `.review/raw/`, rerun. |
| `VERDICT=BLOCK` + "manifest_invalid" | Manifest does not match the diff — validate it (commit skill snippet). |
| `VERDICT=BLOCK` + "refused: staged .review/" | `.review/` is staged: `git restore --staged .review/`. |
| `VERDICT=BLOCK` + "manifest auto-scaffolded" | Chunked path needs a manifest — step 8. |
| `GATE_RC=0`, `VERDICT=EMPTY` / `ERROR` / `TIMEOUT` | **Not reviewed** — reviewers failed and the small path failed open. |
| `GATE_RC=0`, `VERDICT=CRASH` | Inconclusive — `review/hook.py` crashed. |
| `GATE_RC=0`, `VERDICT=SKIP` | Nothing reviewed (docs/data only, or too few lines). |
| `VERDICT=FASTPATH` | Skill bug: `SDD_REVIEW_FASTPATH` leaked. Report it. |
| `VERDICT=none`, `[preflight]` in `gate.txt` | Coverage/assert gate. `GATE_RC=2` = setup error (stale/missing `coverage.xml`), `1` = findings, `3` = crash. |
| `VERDICT=none`, `[gitleaks] secrets detected` or `[semgrep] findings` | Security scanner block. `Terminated` just before it means the run was killed, not a finding. |
| `VERDICT=none`, `gate.txt` shows "Reviewing N file(s)" | Review ran but its log write failed; classify from `gate.txt`. |
| `VERDICT=none`, `GATE_RC≠0`, none of the above | The repo's own lint hook failed. |
| `OUT_DIR_REUSED` | `$OUT` already held a run; nothing was launched. Go back to step 4 with a new `$OUT`. |
| `GATE_RC=launch-error` | See `VERDICT` and `$OUT/launch-error.txt`. |
| `PROCESS_GONE_WITHOUT_MARKER` | Run was killed. Check `free -h`, rerun. |
| `LOG_COUNT` > 1 | Another session logged for the same project during the run — confirm the verdict from `gate.txt`. |
| `codex backend unhealthy` anywhere in `gate.txt` | Reviewers ran blind (sandbox file tools fail silently with rc 0) — an `OK` from this run counts as **not reviewed**. |

## Limitations

- Dry-run logs land in `~/.claude/review/logs/` (gitignored, local only) and in `stats.jsonl`, indistinguishable from real runs, so they skew `stats_cli.py` numbers. The report names the log so it can be told apart.
- A dry run followed by a real commit costs two full reviews (up to ~20 min each).

<critical>
Finish with HEAD unchanged: no `git commit`, no stash, no writes to `.review/approvals/`. A zero exit code proves nothing — report the verdict from the table.
</critical>
