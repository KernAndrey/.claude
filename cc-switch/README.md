# cc-switch

`cc_switch.py` (symlinked as `~/bin/cc-switch`) swaps Claude Code OAuth
accounts by rewriting `~/.claude/.credentials.json` and the `oauthAccount`
field in `~/.claude.json`. Running sessions pick the change up without a
restart. Profiles live in `~/.claude-profiles/<name>.json`.

Manual: `add` / `use` / `list` / `current` / `remove`, plus `usage` to refresh
and print every account's limits and `pick` to move to the account that has
to burn its weekly quota fastest.

`current`, `list` and `usage` all report when each login expires — the date
plus how far off it is (`27 Aug 15:10 (in 6d)`). That is the refresh token's
expiry: past it no switching helps and the account needs `claude` to sign in
again, so it is worth seeing before it happens rather than after.
`usage --json` carries the raw epoch as `login_expires_at`.

The date and the verdict beside it always come from the same copy: the live
credentials for the account that is signed in, the saved profile for every
other. Mixing them printed a future date next to `! refresh token expired`
for anyone who had just signed in again — the profile still holds the token
they retired.

## Automatic switching

`cc-switch auto on` enables it; the flag is permanent until `auto off`. The
statusline is the only sensor — it already receives live 5h/7d percentages,
so there is no daemon and no polling interval.

Switching happens for one of three reasons. The first outranks the others: the
live login stopped working — its refresh token expired, or the credentials file
is missing or damaged. That case cannot wait for a threshold, because a
logged-out session reports no usage at all; the gate carries the login's expiry
so the statusline can wake a tick on time alone. That rescue considers every
saved profile, including the one `.active` names: with no live credentials
there is no email to identify the running account by, so the marker is a
guess, and the profile it points at may hold the only working login left. The other two are the ordinary
ones: the active account hit a limit (5h ≥ 95% or 7d ≥ 99%), or another
account has more urgent quota to spend. The rule is always "rank every usable
profile and take the one that has to burn fastest", never "leave the current
one", which is why it cannot loop. When nothing clears the entry bar the
active account stays put and the earliest server-provided reset is recorded
in `.exhausted`.

Urgency is the required burn rate — `headroom / hours-to-reset ** α`, where
headroom is `100 − weekly %` and α is `CC_SWITCH_URGENCY_ALPHA`, 1 by default.
At 1 that reads directly as percentage points per hour: what you would have to
spend for nothing to be destroyed when the window rolls over. Weekly quota is
use-it-or-lose-it, so the question was never "who has the most left" but
"whose is about to be thrown away". An account at 75% resetting in twelve
hours has to burn 2.1pp/h; one at 20% resetting in six days has to burn
0.6pp/h — so the first is used first and its remaining quarter is not lost.
Ranking by lowest weekly usage did the opposite, every time.

A window with no known reset counts as a fresh week, the least urgent answer
there is and the only one that cannot invent urgency out of missing data. That
is also what a window past its reset reads as, alongside the zeroed usage
`_window_value` already gives it, so the two describe one coherent account.

Two cases are worth stating because they are not generalizations, they are the
same rule. **α = 0** removes deadlines from the comparison entirely and
restores "lowest weekly usage wins" exactly, not approximately — it is the
escape hatch. And whenever every window resets at the same time, or none is
known, the deadline term cancels and the comparison reduces to that same old
thing at *any* α.

The 5pp gap still means five percentage points, measured on the active
account's own window: internally a candidate is restated as what it would be
worth here — `100 − burn_rate × hours_this_account_has_left` — which turns the
whole comparison back into `active % − candidate % ≥ 5pp`, the expression it
always was. That is why one number still reaches the statusline.

Two kinds of account get no urgency credit and are judged on their plain
weekly percentage instead: one whose window closes within the hour, because
its headroom cannot be spent before it rolls over, and one under
`CC_SWITCH_MIN_HEADROOM_7D` (10pp), because there is too little left to be
worth the refresh-token rotation a switch costs. Neither is ruled out — that
looked equivalent and was not, since `pick` has no limit branch and would have
sat on a 99% account with a 94% one available. They are also demoted in the
ranking so a forced evacuation prefers somewhere it can actually stay, while
still landing on them rather than nowhere.

A candidate is confirmed live before any swap, so a revoked or expired login
is never switched into. Only the winner is confirmed, because refreshing
rotates the refresh token — the one operation that can lose access to an
account. The account Claude Code is signed into is never refreshed; it owns
those tokens, and it is recognised by its email rather than by any profile
name. That protection lapses the moment the login stops working: with no
session left to protect, handing out the unusable live token in place of a
profile's own only made the API reject it and marked the profile holding
the recovery as unauthorized.

A candidate that could not be reached (network error, or a throttled refresh)
is not evidence of anything: those pause for `CC_SWITCH_RETRY_SECONDS` instead
of recording a reset deadline, so a blip costs minutes rather than silencing
switching until the window rolls over.

Every command that writes credentials or profiles — `use`, `add`, `remove`,
`usage`, `pick`, and the automatic tick — takes one `flock` around the whole
read-decide-write sequence. They run the same sequence, so without it a manual
switch racing a statusline tick could undo itself, and two refreshes of the
same expired token would retire a perfectly good account.

`pick` checks every candidate live before deciding, then takes the genuinely
most urgent one — stored snapshots only order the checks. It follows the
automatic rule deliberately: a human's `pick` and a statusline tick
disagreeing about the best account is what one shared scorer exists to
prevent. It follows the same
dead-login rule as the automatic path: with no live credentials it considers
every profile, the one `.active` names included.

The reset deadlines the statusline forwards arrive as *numbers* — Unix epoch
seconds — while the same field from the OAuth usage API is an ISO string.
Both are read, string form first: the numeric extractor's `sed` is greedy and
an ISO timestamp is full of colons, so it would reduce one to `59Z`. For
months only the string form was tried, which matched nothing on every render,
so the active account — the one whose numbers actually drive a decision —
stored a null weekly deadline while every other account got a real one from
the API. `cc-switch` parses whichever shape arrived and stores one.

The two window keys are absent whenever the API stops reporting them, so a
payload without them is ordinary rather than broken; the tick is skipped
entirely, exactly as it is when no percentages arrive.

The statusline's own check is a trigger, not a parser. It reads the
credentials file as text with every space, tab and newline removed — JSON
allows any amount of either around a colon, and matching the spellings one
by one called a pretty-printed file dead on every render — and anything it
cannot vouch for wakes a tick, which parses properly: no refresh token, an
empty or non-string one, or a file that never closes its object. Erring the other way costs nothing
visible and loses everything: a logged-out session reports no usage, so no
other trigger would ever fire.

The active account is resolved from the *live* credentials everywhere, never
from the `.active` marker, which goes stale as soon as Claude Code logs into a
different saved account on its own. That marker is a hint for display; acting
on it would refresh the running session's own token, and — worse — save one
account's credentials into another account's profile, destroying it.

The statusline compares whole tenths of a percent, so every trigger written
to `.gate` is rounded *up* to one. The direction matters: a trigger below the
real threshold wakes a tick that then declines to switch and writes the same
trigger back, a process per render for as long as usage sits in the gap,
while a trigger above it simply waits one more tenth.

Thresholds are overridable via `CC_SWITCH_EXIT_5H`, `CC_SWITCH_EXIT_7D`,
`CC_SWITCH_ENTER_5H`, `CC_SWITCH_ENTER_7D`, `CC_SWITCH_BALANCE_GAP_7D`,
`CC_SWITCH_SETTLE_SECONDS`, `CC_SWITCH_RETRY_SECONDS`,
`CC_SWITCH_MIN_HEADROOM_7D`, `CC_SWITCH_URGENCY_ALPHA` and
`CC_SWITCH_CROSSOVER_POLL_SECONDS`. Percentages must be
finite and within 0..100, and each entry bar must stay at or below its exit
threshold — a NaN would otherwise pass silently and crash every later tick.
The urgency exponent is not a percentage and has its own bound of 0..10: it is
raised to the window's hours, and a large one overflows a float inside the
gate, which is a crashing tick on every render — while anything past about 5
is already earliest-deadline-first, so the bound costs nothing real. The
crossover poll is a ceiling on how late a decision can be; 0 turns it off and
any positive value is floored at 60 seconds.

`recheck_after` now covers the active account's own resets too, which it
deliberately did not before. Under "lowest weekly usage wins" its own reset
only lowered its own figure, and low usage was never a reason to leave; under
a burn rate it drops the account from "25pp in twelve hours" to "100pp in a
week", which can hand the lead to a candidate — and no percentage trigger can
catch that, because usage *falls* at a reset.

The same field carries a bounded poll, and it is the one place this stops
being purely event-driven. Pressure grows as one over the hours remaining, so
an account whose window closes sooner gains it faster and overtakes with
nothing else moving — no reset boundary crossed and no usage change to trigger
on. Differentiating the trigger written above gives its drift the sign of
(candidate hours − active hours), so it goes stale in the missed-switch
direction over exactly that condition and no other, which is also exactly the
condition for such a crossover. The poll is therefore armed only while some
candidate resets before the active account, and never at α = 0, where pressure
does not depend on time at all. A poll rather than a closed form because the
closed form exists only at α = 1 and only for an idle account, while the real
motion is usage and time together.

A candidate that could not be reached recorded nothing, so the identical
trigger is written straight back and the next render wakes another tick that
fails the same way. The retry pause is the only thing that bounds it, and it
used to be armed for limit evacuations alone — survivable while the balance
trigger sat at least a gap above zero, and not once the trigger can be
inverted down to it. It now applies whatever the reason was.

An exhaustion deadline never holds a dead login back. `.exhausted` records
that every account was at its limit and can be days out, while being logged
out stops the session entirely — so the dead-login check answers to the
settle window alone, in the tick and in the statusline gate alike. The settle
window still applies: that barrier exists to stop a switch undoing itself,
and without it a dead login with nowhere to go would wake a tick on every
render.

State in `~/.claude-profiles/`: `.auto` (flag), `.gate` (the cheap trigger the
statusline reads on every render: `not_before recheck_after trigger_5h
trigger_7d login_deadline settle_until`), `.live` (the account cc-switch last resolved
from the real credentials — what the statusline displays, refreshed by every
invocation that resolves the active account, so it stays honest even with
auto-switching off), `.exhausted` / `.settle` (deadlines), `.auto.log`
(decisions), `.lock` (flock around read-decide-write). `.active` remains as a
hint and a fallback, but nothing decides on it.

A malformed profile — a hand-edited `expiresAt`, a garbled stored snapshot — is
skipped with a reason rather than taking the whole command down, and a garbled
statusline payload (a non-finite percentage) is ignored instead of being
persisted.

The settle window is armed before anything is swapped, and a switch that cannot
arm it is refused outright rather than performed half-protected: the next render
still carries the outgoing account's percentages, and without the barrier a tick
reading those would switch straight back.

## Working on it

```bash
cd ~/.claude
python3 -m pytest cc-switch/tests/ -q
ruff check --fix cc-switch/cc_switch.py cc-switch/tests/*.py
ruff check --select ANN cc-switch/cc_switch.py cc-switch/tests/*.py
```

For a commit that has to clear the coverage gate, regenerate `coverage.xml`
with the **path** form of `--cov`. `--cov=cc_switch` resolves by import at
pytest startup, before the tests put this directory on `sys.path`, and would
silently measure nothing:

```bash
python3 -m pytest review/ cc-switch/tests/ \
    --cov=review --cov=cc-switch --cov-branch --cov-report=xml
```

It lands in the report as `filename="cc_switch.py"` under the
`~/.claude/cc-switch` source root, which is how diff-cover resolves it back
to `cc-switch/cc_switch.py`. Grep for the bare name, not the path.

`tests/conftest.py` refuses every write aimed at `~/.claude-profiles`,
`~/.claude/.credentials.json` or `~/.claude.json`, judged on the resolved
path. Never disable it to test something — a run that did once replaced a
real profile with fixture data and cost a live login. Mutate the predicate
instead, and point every probe at a name that does not exist.
