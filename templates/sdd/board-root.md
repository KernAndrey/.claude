# Resolving the SDD board

<critical>
The SDD board lives in the **main worktree**. Resolve `{main_root}` first, then read and
write every board path under it — the counter, drafts, specs, and file moves between
board directories. This holds no matter which linked worktree you are standing in.
</critical>

Why: `tasks/.counter` is a git-tracked file, so each linked worktree carries its own
working copy. A counter incremented inside a worktree is invisible to the main checkout,
and both allocate the same number — colliding task IDs. A draft written inside a worktree
is also committed to the task branch and destroyed by `wt remove` if the branch never
merges.

**Exception — the implementation family.** `/implement`, `/implement-wf` and
`/fast-implement` do not use this file. Once a worktree exists, the task file travels with
the code inside it, because its move into `4-in-progress` / `5-review` must be captured by
the commit.

## 1. Resolve `{main_root}`

```bash
git worktree list --porcelain | awk 'NR==1{sub(/^worktree /,""); print}'
```

The first record of `git worktree list` is always the main worktree, and this form
survives paths containing spaces.

Fall back to the current directory, and say so in your output, when:
- the first record carries a `bare` line — a bare repo has no main working tree, or
- the command fails — you are outside a git repository.

## 2. Discover `.tasks.toml` under `{main_root}`

Search `{main_root}` itself plus one and two levels deep (`*/.tasks.toml`,
`*/*/.tasks.toml`), skipping `node_modules/`, `.git/`, `vendor/`, and plugin/cache
directories. A repo may carry several SDD roots, e.g. one per client.

Search under `{main_root}` directly. Discovering under the current directory and then
translating the path invites relative-path arithmetic that goes wrong inside a worktree.

None found → tell the user to run `/task-init` first and stop.

## 3. Select the root

One config → use it. Several → the invoking command's prose owns the choice (by
`id_prefix` for an existing ID; by the directory the description names for a new one;
ask the user when two roots stay plausible after reading the description).

## 4. Define the path tokens

`dir` and `counter_file` inside a config resolve relative to **that config's own
directory**. From the selected config:

| token | value | example |
|---|---|---|
| `{board}` | absolute — `{main_root}/<config dir>/<dir>` | `/home/kern/projects/repo/clients/acme/tasks` |
| `{counter_file}` | absolute — config's `counter_file`, default `{board}/.counter` | `…/clients/acme/tasks/.counter` |
| `{dir}` | repo-relative — `<config dir>/<dir>` | `clients/acme/tasks` |

Use `{board}` and `{counter_file}` for every read and write. Keep `{dir}` for git refs
such as `git cat-file -e origin/dev:{dir}/3-ready/…`, which take repo-relative paths.

Work on `{board}` by absolute path — stay in the current directory throughout, so the
worktree you are standing in keeps its own board untouched. `{project root}` and
"Working directory" in agent prompts keep their current meaning: the checkout you are in.
Agents research code where you stand; only the board file lives elsewhere.

## 5. Check the ID is free (commands that mint IDs)

After computing `{ID}` from the counter, confirm no `{board}/**/{ID}-*.md` exists. If one
does, keep incrementing until the ID is free, then write that corrected value back to
`{counter_file}`. This repairs collisions already on disk.

## 6. Commit the board change

<procedure>
Commit when BOTH hold:
  a. `{main_root}` differs from the current directory — the write went into a checkout
     the user is not standing in, where they cannot see it, and
  b. `{main_root}` is on the base branch `dev`.

  git -C "{main_root}" add -- <board paths>
  git -C "{main_root}" commit -m "chore(sdd): <what changed>" -- <the same board paths>
</procedure>

- **Both steps take the same explicit path list**, and the list holds exactly what this
  run changed: `{counter_file}` and the file you wrote, or — for a move — the old path
  and the new one, which git then records as a rename.
- **`add` first is required.** `git commit -- <path>` alone refuses a newly created file
  with `error: pathspec … did not match any file(s) known to git`, because the pathspec
  form only reaches paths git already knows.
- **`commit -- <paths>` is what keeps the commit narrow.** It records those paths only,
  and anything else already staged in the main checkout stays staged and uncommitted.
- **List only paths that are present now or that this run removed.** A path that exists
  neither on disk nor in the index — e.g. a draft an earlier run already archived — makes
  `git add` abort with `fatal: pathspec … did not match any files`.
- When the commit itself fails (a hook rejects it), the board paths stay staged in
  `{main_root}`. Say so, so the user knows what to unstage.
- **Standing in the main checkout → leave the change uncommitted.** The user sees it and
  commits it themselves, as they do today.
- **`{main_root}` on another branch → leave the change uncommitted.** Committing the board
  onto an unrelated branch strands the task. Report the branch it is actually on.
- **A merge or rebase is in progress** (`MERGE_HEAD` / `REBASE_HEAD` in the git dir), or
  `git -C {main_root} status --porcelain` reports conflicts → leave the change
  uncommitted and name the reason. A partial commit is refused mid-merge.
- After committing, tell the user the change is on local `dev` only and ask whether to run
  `git -C {main_root} push origin dev`. `/implement` cuts its worktree from `origin/dev`
  and cannot see an unpushed board.

## 7. Report

When `{main_root}` differs from the current directory, include in your output: the
absolute path of the file you wrote, `{main_root}`, its current branch, and whether the
board change was committed. A file appearing in another checkout should never be a
surprise.

---

The board is resolved under `{main_root}`, and only the implementation family works on the
board inside a worktree.
