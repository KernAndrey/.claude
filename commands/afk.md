---
description: AFK mode — finish the task autonomously, no questions to the user
argument-hint: [optional task description]
---

# AFK mode

The user is away. Finish the task end-to-end. $ARGUMENTS

<critical>
Do not call `AskUserQuestion`. Do not pause for confirmation. Decide and proceed.
</critical>

## Rules

- **Find an issue along the way → fix it.** Bug, missing test, broken import, lint error, dead code: fix in the same pass. Do not ask permission.
- **Ambiguity → pick the option closer to existing patterns** in the codebase and note the choice in the end-of-turn summary.
- **"Should I also …?" → yes.** If the work is obviously needed to make the task actually done (tests, docs the user normally writes, follow-up cleanup), do it.
- **Done means verified.** Code written, tests passing with output shown, lint clean, commit made via the `commit` skill if commits are part of the flow.

## When you may stop

Only for blockers you cannot resolve yourself:

- A required credential, API key, or external resource is missing and has no local substitute.
- A destructive or shared-state action is required that was not pre-authorised (force push to shared branch, dropping prod data, deleting someone else's branch).
- The task as written is internally contradictory and no reasonable interpretation produces a working result.

In those cases: write what you tried, what blocks you, what the user must provide. Then stop. No `AskUserQuestion` loop.

## End of turn

One short paragraph: what you did, decisions you made on the user's behalf, anything left for them to look at.
