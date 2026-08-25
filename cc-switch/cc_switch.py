#!/usr/bin/env python3
"""cc-switch — switch Claude Code OAuth accounts.

Swaps only the credentials (`~/.claude/.credentials.json`) and the
`oauthAccount` field inside `~/.claude.json`. Nothing else under `~/.claude/`
is touched.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import fcntl
import io
import json
import math
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple, NoReturn

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CREDS_FILE = CLAUDE_DIR / ".credentials.json"
MAIN_FILE = HOME / ".claude.json"
PROFILES_DIR = HOME / ".claude-profiles"
ACTIVE_FILE = PROFILES_DIR / ".active"
# The resolved live account, refreshed on every tick so the statusline can
# name it without parsing JSON or spawning a process.
LIVE_FILE = PROFILES_DIR / ".live"
BACKUPS_DIR = PROFILES_DIR / ".backups"
BACKUP_KEEP = 5

AUTO_FILE = PROFILES_DIR / ".auto"
GATE_FILE = PROFILES_DIR / ".gate"
EXHAUSTED_FILE = PROFILES_DIR / ".exhausted"
SETTLE_FILE = PROFILES_DIR / ".settle"
LOG_FILE = PROFILES_DIR / ".auto.log"
LOCK_FILE = PROFILES_DIR / ".lock"

EXIT_OK = 0
EXIT_USER = 1
EXIT_SYS = 2

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# The Claude Code CLI's public OAuth client id. Public clients have no client
# secret, so this identifier is not confidential — it ships inside the binary
# and travels in every token request. gitleaks flags it purely on UUID entropy.
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # gitleaks:allow
USER_AGENT = "cc-switch (local account switcher)"
HTTP_TIMEOUT = 20


# Leaving a 5h window early costs little — it refills in hours. Leaving a
# weekly window early costs a week, so 7d is squeezed much harder.
DEFAULT_EXIT_5H = 95.0
DEFAULT_EXIT_7D = 99.0
# ENTER_* sit below EXIT_* on purpose: the gap is the hysteresis that stops
# two accounts from trading places at the threshold.
DEFAULT_ENTER_5H = 90.0
DEFAULT_ENTER_7D = 94.0
DEFAULT_BALANCE_GAP_7D = 5.0
DEFAULT_SETTLE_SECONDS = 60.0
# Pause after an unreachable candidate: long enough not to hammer a throttled
# endpoint, short enough that a blip costs minutes rather than days.
DEFAULT_RETRY_SECONDS = 300.0


def die(msg: str, code: int = EXIT_SYS) -> NoReturn:
    print(f"cc-switch: {msg}", file=sys.stderr)
    sys.exit(code)


def _env_float(name: str, default: float) -> float:
    """Read a percentage override, rejecting values that break arithmetic later.

    `float()` happily accepts "nan" and "inf". A NaN threshold would sail
    through here and only blow up in `math.floor()` while writing the gate —
    after `.auto` is already on, so every statusline render would spawn a
    crashing tick.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        die(f"invalid {name}: {raw!r} (expected a number)", EXIT_USER)
    if not math.isfinite(value):
        die(f"invalid {name}: {raw!r} (must be a finite number)", EXIT_USER)
    if not 0 <= value <= 100:
        die(f"invalid {name}: {raw!r} (must be between 0 and 100)", EXIT_USER)
    return value


def _env_seconds(name: str, default: float) -> float:
    """Read a duration override. Non-finite or negative would break deadlines."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        die(f"invalid {name}: {raw!r} (expected a number)", EXIT_USER)
    if not math.isfinite(value) or value < 0:
        die(f"invalid {name}: {raw!r} (must be a non-negative finite number)", EXIT_USER)
    return value


EXIT_5H = _env_float("CC_SWITCH_EXIT_5H", DEFAULT_EXIT_5H)
EXIT_7D = _env_float("CC_SWITCH_EXIT_7D", DEFAULT_EXIT_7D)
ENTER_5H = _env_float("CC_SWITCH_ENTER_5H", DEFAULT_ENTER_5H)
ENTER_7D = _env_float("CC_SWITCH_ENTER_7D", DEFAULT_ENTER_7D)
BALANCE_GAP_7D = _env_float("CC_SWITCH_BALANCE_GAP_7D", DEFAULT_BALANCE_GAP_7D)
SETTLE_SECONDS = _env_seconds("CC_SWITCH_SETTLE_SECONDS", DEFAULT_SETTLE_SECONDS)
RETRY_SECONDS = _env_seconds("CC_SWITCH_RETRY_SECONDS", DEFAULT_RETRY_SECONDS)


def check_threshold_consistency(
    enter_5h: float, exit_5h: float, enter_7d: float, exit_7d: float, balance_gap: float
) -> None:
    """Reject threshold combinations that make switching unstable.

    An entry bar above its exit threshold destroys the hysteresis: an account
    could be left and re-entered at the same percentage. A zero balance gap is
    worse — equal weekly usage becomes a reason to move, so two healthy
    accounts trade places after every settle window, forever.
    """
    if enter_5h > exit_5h:
        die("CC_SWITCH_ENTER_5H must not exceed CC_SWITCH_EXIT_5H", EXIT_USER)
    if enter_7d > exit_7d:
        die("CC_SWITCH_ENTER_7D must not exceed CC_SWITCH_EXIT_7D", EXIT_USER)
    if balance_gap <= 0:
        die("CC_SWITCH_BALANCE_GAP_7D must be greater than 0", EXIT_USER)


check_threshold_consistency(ENTER_5H, EXIT_5H, ENTER_7D, EXIT_7D, BALANCE_GAP_7D)


def ensure_dirs() -> None:
    try:
        PROFILES_DIR.mkdir(mode=0o700, exist_ok=True)
        BACKUPS_DIR.mkdir(mode=0o700, exist_ok=True)
    except OSError as e:
        die(f"failed to create {PROFILES_DIR}: {e}")


def detect_indent(raw: str) -> int:
    for line in raw.split("\n")[1:]:
        stripped = line.lstrip(" ")
        if stripped and stripped != line:
            return len(line) - len(stripped)
    return 2


def _parse_json_object(path: Path) -> tuple[dict[str, Any], str, bool]:
    """Read and parse a JSON-object file.

    Raises FileNotFoundError, OSError, json.JSONDecodeError, or TypeError
    (on non-object JSON) — callers wrap these with command-specific messages.
    """
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("expected a JSON object")
    return data, raw, raw.endswith("\n")


def read_json_file(path: Path) -> tuple[dict[str, Any], str, bool]:
    try:
        return _parse_json_object(path)
    except FileNotFoundError:
        die(f"file not found: {path}")
    except json.JSONDecodeError as e:
        die(f"invalid JSON in {path}: {e}")
    except TypeError:
        die(f"expected a JSON object in {path}")
    except OSError as e:
        die(f"failed to read {path}: {e}")


def _open_write_0600(path: Path) -> io.TextIOWrapper:
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, "w", encoding="utf-8")


def atomic_write_json(
    path: Path,
    data: dict[str, Any],
    indent: int,
    trailing_nl: bool,
) -> None:
    tmp = path.parent / (path.name + ".tmp")
    try:
        with _open_write_0600(tmp) as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            if trailing_nl:
                f.write("\n")
        os.replace(tmp, path)
    except OSError as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        die(f"failed to write {path}: {e}")


def profile_path(name: str) -> Path:
    if not name or "/" in name or "\\" in name or name.startswith("."):
        die(f"invalid profile name: {name!r}", EXIT_USER)
    return PROFILES_DIR / f"{name}.json"


def confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def read_profile(name: str) -> dict[str, Any]:
    path = profile_path(name)
    if not path.exists():
        die(f"profile not found: {name}", EXIT_USER)
    try:
        data, _, _ = _parse_json_object(path)
    except json.JSONDecodeError as e:
        die(f"corrupted profile {name}: {e}")
    except TypeError:
        die(f"corrupted profile {name}: expected a JSON object")
    except OSError as e:
        die(f"failed to read {path}: {e}")
    return data


def save_profile(name: str, creds: dict[str, Any], oauth_account: dict[str, Any]) -> None:
    path = profile_path(name)
    now = _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload: dict[str, Any] = {"credentials": creds, "oauthAccount": oauth_account, "savedAt": now}
    atomic_write_json(path, payload, indent=2, trailing_nl=True)


def read_active() -> str | None:
    if not ACTIVE_FILE.exists():
        return None
    try:
        value = ACTIVE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def write_text_file(path: Path, text: str, fatal: bool = True) -> bool:
    tmp = path.parent / (path.name + ".tmp")
    try:
        with _open_write_0600(tmp) as f:
            f.write(text)
        os.replace(tmp, path)
    except OSError as e:
        if fatal:
            die(f"failed to write {path}: {e}")
        return False
    return True


def write_active(name: str) -> None:
    write_text_file(ACTIVE_FILE, name + "\n")


def rotate_backups() -> None:
    try:
        backups = sorted(p for p in BACKUPS_DIR.iterdir() if p.is_file() and p.name.startswith("claude.json."))
    except OSError:
        return
    while len(backups) > BACKUP_KEEP:
        victim = backups.pop(0)
        try:
            victim.unlink()
        except OSError:
            pass


def backup_main() -> None:
    if not MAIN_FILE.exists():
        return
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = BACKUPS_DIR / f"claude.json.{ts}"
    try:
        shutil.copy2(MAIN_FILE, dest)
        os.chmod(dest, 0o600)
    except OSError as e:
        die(f"failed to create backup {dest}: {e}")
    rotate_backups()


def _read_json_lenient(path: Path, strict: bool) -> dict[str, Any] | None:
    """Parse a JSON object; when not strict, unreadable means None, not exit.

    `strict=False` has to mean it for damaged files too, not just missing
    ones: a half-written credentials file is exactly the situation the
    non-strict callers exist to survive.
    """
    if strict:
        data, _, _ = read_json_file(path)
        return data
    try:
        data, _, _ = _parse_json_object(path)
    except (OSError, ValueError, TypeError):  # ValueError covers JSON and UTF-8 errors
        return None
    return data


def read_current_state(strict: bool = True) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Read the currently active credentials and oauthAccount.

    When strict=False, returns None instead of exiting when the data is
    missing, damaged, or incomplete.
    """
    if not CREDS_FILE.exists():
        if strict:
            die(f"missing {CREDS_FILE} — log in to Claude Code first", EXIT_USER)
        return None
    creds = _read_json_lenient(CREDS_FILE, strict)
    # The value must be an object, not merely present: a list or string there
    # sails past a key check and crashes the first `.get` downstream.
    if creds is None or not isinstance(creds.get("claudeAiOauth"), dict):
        if strict:
            die(f"{CREDS_FILE} has no usable 'claudeAiOauth' object")
        return None
    if not MAIN_FILE.exists():
        if strict:
            die(f"missing {MAIN_FILE} — log in to Claude Code first", EXIT_USER)
        return None
    main_data = _read_json_lenient(MAIN_FILE, strict)
    if main_data is None:
        return None
    oauth = main_data.get("oauthAccount")
    if not isinstance(oauth, dict):
        if strict:
            die(f"{MAIN_FILE} has no 'oauthAccount' field — log in to Claude Code first")
        return None
    return creds, oauth


def list_profiles() -> list[Path]:
    try:
        items = [
            p for p in PROFILES_DIR.iterdir() if p.is_file() and p.suffix == ".json" and not p.name.startswith(".")
        ]
    except OSError:
        return []
    return sorted(items)


def load_profile_data(name: str) -> dict[str, Any] | None:
    """Read a profile without exiting — callers decide what a bad one means."""
    try:
        data, _, _ = _parse_json_object(profile_path(name))
    except (OSError, ValueError, TypeError):  # ValueError covers JSON and UTF-8 errors
        return None
    return data


# ---- Auto-switch state ------------------------------------------------


def _now() -> float:
    return time.time()


def _iso_now() -> str:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(ts: object) -> float | None:
    """Parse an API timestamp to epoch seconds; None when absent or unparsable.

    `resets_at` comes back null for a window with no usage at all, so the
    missing case is ordinary rather than exceptional.
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def timestamp_arg_to_iso(raw: str | None) -> str | None:
    """Normalise a `--resets-*` argument to the ISO form the API produces.

    Two producers disagree about the shape of a reset deadline. The OAuth
    usage endpoint returns an ISO 8601 string; the Claude Code statusline
    payload documents `resets_at` as a *number* — Unix epoch seconds — and
    argparse hands every value over as text. Converting once, here at the
    edge, keeps one shape on disk, so `parse_iso` and every reader below it
    stay single-format and `usage --json` never emits two.

    ISO is tried first on purpose: `float()` reads a hand-written
    `"20260827"` as epoch 20260827 — August 1970 — which would silently
    declare a live window rolled over. `fromisoformat` reads it as the date
    it plainly is.

    Every rejection matters, and the one range check makes all of them. The
    shell forwards whatever it found rather than parsing, so a JSON `null`
    arrives as the literal string and `float()` refuses it. NaN passes
    `float()` and then loses every comparison, including this one, so the
    chain is False and it falls out; infinity and a finite but absurd value
    (1e300, which clears every other numeric check and then raises inside
    `datetime.fromtimestamp`) both fail the ceiling. Zero is how every other
    epoch field in this file spells "no deadline", so the bound is exclusive
    at the bottom. An explicit `math.isfinite` in front of this read as the
    thing keeping NaN out and was doing nothing — no mutation of it could
    change an answer.
    """
    if not isinstance(raw, str) or not raw:
        return None
    if parse_iso(raw) is not None:
        return raw
    try:
        epoch = float(raw)
    except ValueError:
        return None
    if not 0 < epoch <= MAX_EPOCH:
        return None
    return _dt.datetime.fromtimestamp(epoch, _dt.UTC).isoformat()


def read_epoch_file(path: Path) -> float:
    """Read a deadline written as plain text; 0.0 when missing or unreadable.

    Plain text rather than JSON so the statusline can read it from bash
    without a parser.
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def write_epoch_file(path: Path, epoch: float) -> None:
    write_text_file(path, f"{int(epoch)}\n")


def clear_state_file(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def auto_enabled() -> bool:
    try:
        return AUTO_FILE.read_text(encoding="utf-8").strip() == "on"
    except OSError:
        return False


@contextlib.contextmanager
def profile_lock() -> Iterator[bool]:
    """Hold one lock across the whole read-decide-write sequence.

    Yields False when another process holds it. Two sessions crossing the
    threshold in the same second must not both auto-save and switch, or the
    second one writes stale credentials over the profile the first just
    moved to.
    """
    try:
        fd = os.open(str(LOCK_FILE), os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError:
        yield False
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def log_decision(message: str) -> None:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        fd = os.open(str(LOG_FILE), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {message}\n")
    except OSError:
        pass


# ---- Usage snapshots --------------------------------------------------


class Candidate(NamedTuple):
    name: str
    five_hour: float
    seven_day: float


def _window(utilization: float, resets_at: str | None) -> dict[str, Any]:
    return {"utilization": utilization, "resets_at": resets_at}


def make_snapshot(five: float, seven: float, resets_5h: str | None, resets_7d: str | None) -> dict[str, Any]:
    return {
        "five_hour": _window(five, resets_5h),
        "seven_day": _window(seven, resets_7d),
        "observedAt": _iso_now(),
    }


def usage_from_api(payload: dict[str, Any]) -> dict[str, Any]:
    """Reduce an /api/oauth/usage response to the two windows we act on."""
    values: dict[str, tuple[float, str | None]] = {}
    for key in ("five_hour", "seven_day"):
        block = payload.get(key)
        util = block.get("utilization") if isinstance(block, dict) else None
        resets = block.get("resets_at") if isinstance(block, dict) else None
        values[key] = (
            float(util) if isinstance(util, (int, float)) else 0.0,
            resets if isinstance(resets, str) else None,
        )
    return make_snapshot(values["five_hour"][0], values["seven_day"][0], values["five_hour"][1], values["seven_day"][1])


def _window_value(window: object, now: float) -> float:
    if not isinstance(window, dict):
        return 0.0
    resets = parse_iso(window.get("resets_at"))
    if resets is not None and now > resets:
        return 0.0
    value = window.get("utilization")
    return float(value) if isinstance(value, (int, float)) else 0.0


def effective_usage(snapshot: object, now: float) -> tuple[float, float]:
    """Snapshot usage with each window zeroed past its own reset.

    An idle account's usage only falls, so a stored snapshot is an upper
    bound. Both windows are zeroed independently: zeroing only the 5h
    component would strand an account whose stored weekly figure is high but
    whose weekly window rolled over long ago.
    """
    if not isinstance(snapshot, dict):
        return 0.0, 0.0
    return _window_value(snapshot.get("five_hour"), now), _window_value(snapshot.get("seven_day"), now)


def record_usage_snapshot(name: str, snapshot: dict[str, Any]) -> None:
    """Merge fresh usage into a saved profile and clear any auth failure.

    Having read usage at all proves the credentials work, so a stale
    `authError` must not survive it.
    """
    data = load_profile_data(name)
    if data is None:
        return
    data["usage"] = snapshot
    data.pop("authError", None)
    atomic_write_json(profile_path(name), data, indent=2, trailing_nl=True)


def mark_auth_error(name: str, reason: str) -> None:
    data = load_profile_data(name)
    if data is None:
        return
    data["authError"] = {"reason": reason, "at": _iso_now()}
    atomic_write_json(profile_path(name), data, indent=2, trailing_nl=True)


def profile_unusable_reason(data: object, now: float) -> str | None:
    """Why this profile must not be switched to, or None when it is fine."""
    if not isinstance(data, dict):
        return "corrupted"
    creds = data.get("credentials")
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    if not has_usable_refresh_token(oauth):
        return "no credentials"
    if not isinstance(data.get("oauthAccount"), dict):
        return "no account"
    # A refresh does not push this deadline out — the server returns the same
    # absolute expiry — so past it the account is dead until someone logs in.
    # An unusable value is not evidence of death, same as everywhere else.
    expiry = refresh_expiry_epoch(oauth)
    if expiry is not None and expiry <= now:
        return "refresh token expired"
    err = data.get("authError")
    if isinstance(err, dict):
        return f"auth error ({err.get('reason', 'unknown')})"
    return None


def rank_candidates(now: float, exclude: str | None) -> list[Candidate]:
    """Usable profiles that clear the entry bar, lowest weekly usage first.

    This is the whole loop-prevention argument: one evaluation over a total
    order picks the best account, rather than reacting to the current one.
    """
    ranked: list[Candidate] = []
    for path in list_profiles():
        name = path.stem
        if name == exclude:
            continue
        data = load_profile_data(name)
        if profile_unusable_reason(data, now) is not None:
            continue
        five, seven = effective_usage((data or {}).get("usage"), now)
        if five > ENTER_5H or seven > ENTER_7D:
            continue
        ranked.append(Candidate(name, five, seven))
    ranked.sort(key=lambda c: (c.seven_day, c.five_hour, c.name))
    return ranked


#: `datetime.fromtimestamp` raises beyond the platform's time_t. Anything past
#: this is malformed rather than "far future", and must not reach formatting.
MAX_EPOCH = 32503680000.0  # 3000-01-01


def has_usable_refresh_token(oauth: object) -> bool:
    """A non-empty string, matching what the statusline's pattern accepts.

    The two must agree exactly. When Python called `refreshToken: 123`
    healthy and the shell called it dead, every render woke a tick that then
    took the ordinary path and never armed the retry pause — a background
    process per render, indefinitely.
    """
    if not isinstance(oauth, dict):
        return False
    token = oauth.get("refreshToken")
    return isinstance(token, str) and bool(token)


def refresh_expiry_epoch(oauth: object) -> float | None:
    """The refresh token's expiry in epoch seconds, or None when unusable.

    The single definition of "usable", because three copies of this check had
    already drifted apart. Every rejection here matters:

    * `bool` is an `int` in Python, so `refreshTokenExpiresAt: true` would
      otherwise read as epoch 0.001 and declare a working login expired.
    * NaN loses every comparison; infinity would keep a dead token valid.
    * A finite but absurd value (1e300) passes every numeric check and then
      raises inside `datetime.fromtimestamp`, taking `list` and `usage` down.
    """
    if not isinstance(oauth, dict):
        return None
    expires = oauth.get("refreshTokenExpiresAt")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return None
    try:
        value = float(expires) / 1000
    except OverflowError:
        return None  # an int too large for a float is malformed, not distant
    if not math.isfinite(value) or not 0 <= value <= MAX_EPOCH:
        return None
    return value


def live_credentials_status(now: float) -> tuple[str | None, float]:
    """Why the live login cannot be used, and when it expires.

    One parse and one policy, because the reason and the deadline are two
    views of the same file and had already drifted into separate copies.

    The deadline is what the statusline compares against, and it cannot parse
    JSON: damaged credentials therefore report a deadline already in the past
    rather than "unknown", or the shell would wait forever on a file that is
    never coming back. A merely absent expiry stays 0 — nothing to act on.
    """
    if not CREDS_FILE.exists():
        return "no credentials", DEADLINE_NOW
    try:
        data, _, _ = _parse_json_object(CREDS_FILE)
    except (OSError, ValueError, TypeError):  # ValueError covers JSON and UTF-8 errors
        return "unreadable credentials", DEADLINE_NOW
    oauth = data.get("claudeAiOauth")
    if not has_usable_refresh_token(oauth):
        return "no refresh token", DEADLINE_NOW
    expiry = refresh_expiry_epoch(oauth)
    if expiry is None:
        return None, 0.0  # an unusable expiry is not evidence of death
    return ("login expired" if expiry <= now else None), expiry


def live_credentials_dead(now: float) -> str | None:
    """Why the live login can no longer be used, or None while it works.

    A dead login is worse than an exhausted one: the session stops with
    "Not logged in" and no usage figures are reported at all, so nothing in
    the limit path would ever notice.
    """
    return live_credentials_status(now)[0]


def switch_reason(five: float, seven: float, candidate_7d: float, expired: bool = False) -> str | None:
    """Why to leave the active account: dead login, exhaustion, imbalance, or none.

    A dead login outranks the others: there is nothing left to spend on this
    account, and the entry-bar comparison it would otherwise lose to is
    irrelevant when the alternative is a session that cannot run at all.
    """
    if expired:
        return "expired"
    if five >= EXIT_5H or seven >= EXIT_7D:
        return "limit"
    if seven - candidate_7d >= BALANCE_GAP_7D:
        return "balance"
    return None


def earliest_future_reset(names: list[str], now: float) -> float:
    """Earliest window reset still ahead of us, 0.0 when none is known.

    Past such a moment an idle snapshot zeroes itself, which can create a
    reason to switch with no usage change at all.
    """
    upcoming: list[float] = []
    for name in names:
        data = load_profile_data(name)
        snapshot = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(snapshot, dict):
            continue
        for key in ("five_hour", "seven_day"):
            window = snapshot.get(key)
            # A hand-edited profile can hold a string or a list here. This runs
            # inside recompute_gate, so raising would take down `auto on`,
            # every switch, and even a no-switch tick.
            if not isinstance(window, dict):
                continue
            resets = parse_iso(window.get("resets_at"))
            if resets is not None and resets > now:
                upcoming.append(resets)
    return min(upcoming) if upcoming else 0.0


#: A deadline already in the past, written when the live credentials are
#: damaged. The statusline has no way to parse JSON, so this is how it learns
#: to wake a tick: 0 would mean "no deadline" and it would wait forever.
DEADLINE_NOW = 1.0


def live_login_deadline() -> float:
    """When the live login stops working; 0 when there is nothing to go on."""
    return live_credentials_status(_now())[1]


def _gate_tenths(value: float) -> int:
    """A percentage as tenths, never below the value it stands for.

    The statusline compares whole tenths, so a trigger has to be one. The
    direction is not a matter of taste: a trigger a tenth *below* the real
    threshold wakes a tick that then declines to switch and writes the same
    trigger back, a process per render for as long as usage sits in the gap.
    A tenth *above* costs one more tenth of usage before the check runs.

    Rounding to nine places first. A one-decimal literal times ten is exact
    in binary, but a *sum* of two is not: `16.1 + 0.1` is 16.200000000000003,
    and a bare ceiling turns that into 163 rather than 162 — a trigger a
    tenth past the point where a reason actually appears. `trigger_7d` is
    exactly such a sum, and its precision comes from the API rather than from
    anything this file validates.
    """
    return math.ceil(round(value * 10, 9))


def recompute_gate(active: str | None, now: float) -> None:
    """Write the cheap trigger the statusline consults on every render.

    Format `<not_before> <recheck_after> <trigger_5h> <trigger_7d>
    <login_deadline> <settle_until>`, all integers so bash compares them
    without a parser; 0 means "no trigger".
    Balancing needs checks at ordinary percentages too, and this file is what
    keeps that from costing a python process per render.
    """
    ranked = rank_candidates(now, active)
    best_7d = ranked[0].seven_day if ranked else 100.0
    trigger_7d = min(EXIT_7D, best_7d + BALANCE_GAP_7D)
    not_before = max(read_epoch_file(SETTLE_FILE), read_epoch_file(EXHAUSTED_FILE))
    others = [p.stem for p in list_profiles() if p.stem != active]
    # Triggers are written in tenths of a percent, and the statusline scales
    # its own percentages the same way. Whole numbers could not express a
    # fractional threshold at all — EXIT_5H=95.1 rounded up to 96, which
    # 95.2% usage never reaches.
    #
    # The fifth field is when the live login dies. Once the session is logged
    # out no usage is reported at all, so no percentage trigger could ever
    # fire — this is the only thing that would still wake a tick.
    #
    # The sixth is the settle window alone. The dead-login check answers to
    # that and not to `not_before`, which also carries the exhaustion
    # deadline: being at a limit is no reason to stay logged out, and that
    # deadline can be days away.
    line = (
        f"{int(not_before)} {int(earliest_future_reset(others, now))} "
        f"{_gate_tenths(EXIT_5H)} {_gate_tenths(trigger_7d)} "
        f"{int(live_login_deadline())} {int(read_epoch_file(SETTLE_FILE))}\n"
    )
    write_text_file(GATE_FILE, line, fatal=False)


# ---- Live account checks ----------------------------------------------


def _http_json(req: urllib.request.Request) -> tuple[int, Any]:
    """Send a request to one of our two endpoints and parse the JSON reply.

    The allow-list is enforced rather than assumed: urllib honours `file://`,
    so a URL that ever became attacker-influenced could read local files. Both
    URLs are module constants today, and this keeps that true.
    """
    if req.full_url not in (USAGE_URL, TOKEN_URL):
        die(f"refusing to call an unexpected URL: {req.full_url}")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310  # nosemgrep: dynamic-urllib-use-detected
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (OSError, ValueError):
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return 0, {"error": {"message": str(e)}}


def fetch_usage(access_token: str) -> tuple[int, Any]:
    req = urllib.request.Request(
        USAGE_URL,
        headers={"Authorization": f"Bearer {access_token}", "User-Agent": USER_AGENT},
    )
    return _http_json(req)


def oauth_refresh(refresh_token: str) -> tuple[int, Any]:
    """Exchange a refresh token. Never retried — the endpoint throttles failures with 429."""
    body = json.dumps(
        {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": OAUTH_CLIENT_ID}
    ).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    return _http_json(req)


def _emergency_token_dump(name: str, payload: dict[str, Any]) -> None:
    """Last resort so a rotated token is never lost with nowhere to go."""
    path = PROFILES_DIR / f".recovery-{name}.json"
    try:
        with _open_write_0600(path) as f:
            json.dump(payload, f, indent=2)
        print(f"cc-switch: rotated tokens for '{name}' saved to {path}", file=sys.stderr)
    except OSError:
        print(f"cc-switch: rotated tokens for '{name}': {json.dumps(payload)}", file=sys.stderr)


def _store_refreshed_tokens(name: str, payload: dict[str, Any]) -> str:
    """Write rotated tokens to the profile before they are used anywhere else.

    The old refresh token dies the moment the server answers, so the gap
    between that answer and this write is the only place an account can be
    lost. Nothing else happens in between.
    """
    try:
        return _store_refreshed_tokens_unguarded(name, payload)
    except SystemExit:
        _emergency_token_dump(name, payload)
        raise
    except Exception:
        # Anything at all — a malformed `expires_in`, an unreadable profile,
        # a surprise from the JSON layer — still leaves the server-side old
        # token dead. The rotated pair must reach disk somewhere before this
        # propagates, or the account needs a manual login to recover.
        _emergency_token_dump(name, payload)
        raise


def _expiry_ms(value: object, now_ms: int) -> int | None:
    """Absolute expiry from a lifetime in seconds; None when unusable."""
    try:
        seconds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return now_ms + seconds * 1000 if seconds > 0 else None


def _store_refreshed_tokens_unguarded(name: str, payload: dict[str, Any]) -> str:
    data = load_profile_data(name)
    if data is None:
        die(f"profile {name} became unreadable while storing refreshed tokens")
    oauth = data["credentials"]["claudeAiOauth"]
    now_ms = int(time.time() * 1000)
    oauth["accessToken"] = payload["access_token"]
    if payload.get("refresh_token"):
        oauth["refreshToken"] = payload["refresh_token"]
    # A missing or malformed lifetime leaves the stored expiry alone: a stale
    # timestamp only costs one extra refresh, whereas guessing could park a
    # dead token far in the future and strand the account.
    access_expiry = _expiry_ms(payload.get("expires_in"), now_ms)
    if access_expiry is not None:
        oauth["expiresAt"] = access_expiry
    refresh_expiry = _expiry_ms(payload.get("refresh_token_expires_in"), now_ms)
    if refresh_expiry is not None:
        oauth["refreshTokenExpiresAt"] = refresh_expiry
    data.pop("authError", None)
    atomic_write_json(profile_path(name), data, indent=2, trailing_nl=True)
    return str(payload["access_token"])


def _refresh_error(name: str, status: int, payload: object) -> tuple[str, bool]:
    """Classify a failed refresh: retire the profile only on a real rejection.

    Returns (message, transient). A throttled or unreachable server says
    nothing about the account, so it must not retire the profile and must not
    count as evidence of exhaustion.
    """
    detail = json.dumps(payload) if payload is not None else ""
    if status in (401, 403) or "invalid_grant" in detail:
        mark_auth_error(name, f"refresh rejected ({status})")
        return f"unauthorized ({status})", False
    return f"refresh failed ({status})", True


def _is_live_account(name: str) -> bool:
    """Does this profile hold the account Claude Code is currently signed into?

    Matched by email, not by profile name: the same account can be saved
    twice under different names, and refreshing the copy would rotate the
    refresh token the running session shares with it.

    A dead login is nobody's. There is no session left to protect, and
    answering yes hands out the unusable live token in place of the
    profile's own — which comes back unauthorized and stamps `authError` on
    the very profile the rescue was reaching for.
    """
    if live_credentials_dead(_now()) is not None:
        return False
    state = read_current_state(strict=False)
    live_email = state[1].get("emailAddress") if state else None
    return bool(live_email) and _profile_email(profile_path(name)) == live_email


def _live_access_token() -> tuple[str | None, str | None, bool]:
    """The token of the account Claude Code is signed into, read as-is."""
    state = read_current_state(strict=False)
    if state is None:
        return None, "no live credentials", False
    return state[0].get("claudeAiOauth", {}).get("accessToken"), None, False


def _stored_access_token(oauth: dict[str, Any]) -> tuple[str | None, str | None, bool] | None:
    """A usable saved token, or None when one has to be fetched.

    A hand-edited profile can hold anything in `expiresAt`; a bad value skips
    this account rather than aborting the command for every other one.
    """
    try:
        stored_expiry = float(oauth.get("expiresAt") or 0)
    except (TypeError, ValueError, OverflowError):
        return None, "corrupted expiry", False
    # "NaN" and "Infinity" are valid JSON floats to Python, and a merely
    # implausible finite value is just as bad: infinity or 1e300 would treat
    # a long-dead token as valid, send it to the API, and let the resulting
    # 401 retire a profile that only needed refreshing. Same bound as the
    # refresh expiry, for the same reason.
    if not math.isfinite(stored_expiry) or not 0 <= stored_expiry / 1000 <= MAX_EPOCH:
        return None, "corrupted expiry", False
    if stored_expiry / 1000 > time.time() + 60:
        return oauth.get("accessToken"), None, False
    return None


def access_token_for(name: str, active: str | None) -> tuple[str | None, str | None, bool]:
    """Return (access token, error, transient). The live account is never refreshed.

    Claude Code owns the live credentials and rotates them itself; refreshing
    underneath it would invalidate the refresh token the running process
    holds.
    """
    if name == active or _is_live_account(name):
        return _live_access_token()
    data = load_profile_data(name)
    if data is None:
        return None, "corrupted", False
    oauth = data.get("credentials", {}).get("claudeAiOauth", {})
    usable = _stored_access_token(oauth)
    if usable is not None:
        return usable
    status, payload = oauth_refresh(str(oauth.get("refreshToken") or ""))
    if status != 200 or not isinstance(payload, dict) or "access_token" not in payload:
        message, transient = _refresh_error(name, status, payload)
        return None, message, transient
    return _store_refreshed_tokens(name, payload), None, False


class Confirmation(NamedTuple):
    """Outcome of a live candidate check.

    `transient` separates "this account is out of room / not authorized" from
    "we could not reach the server". Only the former is evidence about the
    account; treating the latter as exhaustion would silence auto-switching
    for as long as the reset deadline says.
    """

    usage: dict[str, Any] | None
    error: str | None
    transient: bool = False


def confirm_candidate(name: str, active: str | None) -> Confirmation:
    """Prove the candidate is authorized and learn its real usage.

    Offline checks cannot see a revoked session, so the winner — and only the
    winner — is confirmed live before anything is swapped.
    """
    token, error, transient = access_token_for(name, active)
    if not token:
        return Confirmation(None, error or "no access token", transient)
    status, payload = fetch_usage(token)
    if status == 200 and isinstance(payload, dict):
        snapshot = usage_from_api(payload)
        record_usage_snapshot(name, snapshot)
        return Confirmation(snapshot, None)
    if status in (401, 403):
        mark_auth_error(name, f"usage rejected ({status})")
        return Confirmation(None, f"unauthorized ({status})")
    return Confirmation(None, f"usage request failed ({status})", transient=True)


# ---- Commands ---------------------------------------------------------


def cmd_add(args: argparse.Namespace) -> int:
    ensure_dirs()
    name: str = args.name
    path = profile_path(name)
    if path.exists():
        if not confirm(f"Profile '{name}' already exists. Overwrite?"):
            print("Cancelled.")
            return EXIT_OK
    # The lock is taken after the prompt so a waiting confirmation never
    # blocks a tick, but before any write — an auto switch saving the account
    # we are about to overwrite would otherwise interleave with this.
    with profile_lock() as acquired:
        if not acquired:
            die("another cc-switch operation is in progress — try again in a moment", EXIT_USER)
        state = read_current_state(strict=True)
        assert state is not None  # strict=True would have exited on failure
        creds, oauth = state
        save_profile(name, creds, oauth)
    email = oauth.get("emailAddress", "?")
    sub = creds.get("claudeAiOauth", {}).get("subscriptionType", "?")
    print(f"Saved profile '{name}' ({email}, {sub}) -> {path}")
    return EXIT_OK


def _auto_backup_active(name: str, quiet: bool = False) -> None:
    """Save the live credentials into the profile we are leaving.

    The destination is resolved from those same live credentials, never from
    the `.active` marker. They disagree whenever Claude Code logged into a
    different saved account on its own, and writing one account's credentials
    into another account's profile destroys the second one irrecoverably.

    The usage snapshot recorded during this session is preserved, so the
    account we walk away from keeps honest numbers for the next ranking.
    """
    active = resolve_active()
    if not active or active == name or not profile_path(active).exists():
        return
    state = read_current_state(strict=False)
    if state is None:
        return
    previous = load_profile_data(active) or {}
    cur_creds, cur_oauth = state
    save_profile(active, cur_creds, cur_oauth)
    snapshot = previous.get("usage")
    if isinstance(snapshot, dict):
        record_usage_snapshot(active, snapshot)
    if not quiet:
        print(f"Auto-saved active profile '{active}'")


def _load_profile_for_use(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = read_profile(name)
    new_creds = profile.get("credentials")
    new_oauth = profile.get("oauthAccount")
    if not isinstance(new_creds, dict) or not isinstance(new_oauth, dict):
        die(f"corrupted profile: {name} (missing credentials/oauthAccount)")
    return new_creds, new_oauth


def _preflight_main() -> tuple[dict[str, Any], int, bool]:
    if not MAIN_FILE.exists():
        die(f"missing {MAIN_FILE} — log in to Claude Code first", EXIT_USER)
    main_data, raw_main, tnl_main = read_json_file(MAIN_FILE)
    return main_data, detect_indent(raw_main), tnl_main


def _preflight_creds() -> tuple[dict[str, Any] | None, int, bool]:
    """Read the outgoing credentials for formatting and rollback.

    Damaged content is not fatal here: a corrupt credentials file is one of
    the reasons to switch away in the first place, so refusing to move would
    strand the session on the very account that cannot be used. There is
    simply nothing to roll back to in that case.
    """
    if CREDS_FILE.exists():
        try:
            old_creds, raw_creds, creds_tnl = _parse_json_object(CREDS_FILE)
        except (OSError, ValueError, TypeError):  # ValueError covers JSON and UTF-8 errors
            return None, 2, False
        return old_creds, detect_indent(raw_creds), creds_tnl
    if not CLAUDE_DIR.exists():
        die(f"missing {CLAUDE_DIR} — Claude Code has never been run", EXIT_USER)
    return None, 2, False


def _apply_switch_atomic(
    new_creds: dict[str, Any],
    new_oauth: dict[str, Any],
    main_data: dict[str, Any],
    indent_main: int,
    tnl_main: bool,
    old_creds: dict[str, Any] | None,
    creds_indent: int,
    creds_tnl: bool,
) -> None:
    """Apply creds then oauth, leaving a coherent state either way.

    The two files must describe the same account. If the second write fails
    the first is undone — and when the outgoing credentials were too damaged
    to keep a copy of, the credentials file is removed instead. "No
    credentials" is a state the tool already recognises as a dead login and
    evacuates from; a credentials file paired with someone else's
    `oauthAccount` is not.
    """
    atomic_write_json(CREDS_FILE, new_creds, creds_indent, creds_tnl)
    main_data["oauthAccount"] = new_oauth
    try:
        atomic_write_json(MAIN_FILE, main_data, indent_main, tnl_main)
    except BaseException:
        _undo_credentials(old_creds, creds_indent, creds_tnl)
        raise


def _undo_credentials(old_creds: dict[str, Any] | None, indent: int, trailing_newline: bool) -> None:
    """Put the previous credentials back, or leave none at all.

    Restoring can fail in its own right — a full or read-only filesystem is
    exactly the kind of thing that made the first write fail. Removing them
    is the fallback: "no credentials" is a state the tool recognises as a
    dead login and escapes from, while credentials paired with someone
    else's `oauthAccount` are a state nothing interprets. If even the
    removal fails the error is raised rather than suppressed, because
    leaving that pair behind silently is the one outcome to avoid.
    """
    if old_creds is not None:
        try:
            atomic_write_json(CREDS_FILE, old_creds, indent, trailing_newline)
        except (SystemExit, OSError):
            CREDS_FILE.unlink(missing_ok=True)
        return
    CREDS_FILE.unlink(missing_ok=True)


def _switch_to(name: str, quiet: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
    """Swap credentials to a saved profile. Shared by manual and auto paths.

    Every switch arms the settle window, manual ones included: the statusline
    keeps reporting the old account's percentages for a moment afterwards, and
    a tick reading those would immediately undo a deliberate `cc-switch use`.

    The window is armed BEFORE the swap, and fatally: if it cannot be written
    there is no safe way to continue, because the very next render could hand
    a tick the outgoing account's numbers and switch straight back. Failing
    here leaves everything untouched; failing after the swap would not.
    """
    # Everything that can refuse the switch runs first, while nothing has been
    # written: a malformed target profile or an unreadable ~/.claude.json must
    # leave no trace. Only then is the barrier armed — and it is armed before
    # the swap, because a switch we cannot protect must not happen at all.
    new_creds, new_oauth = _load_profile_for_use(name)
    main_data, indent_main, tnl_main = _preflight_main()
    old_creds, creds_indent, creds_tnl = _preflight_creds()

    write_epoch_file(SETTLE_FILE, _now() + SETTLE_SECONDS)
    try:
        # Anything failing before the swap completes leaves a barrier guarding
        # a switch that never happened, which would silence automatic
        # switching for the whole window for nothing. The atomic write is
        # inside this block too: it rolls itself back on failure, so the
        # barrier must go with it.
        _auto_backup_active(name, quiet=quiet)
        backup_main()
        _apply_switch_atomic(
            new_creds,
            new_oauth,
            main_data,
            indent_main,
            tnl_main,
            old_creds,
            creds_indent,
            creds_tnl,
        )
    except BaseException:
        clear_state_file(SETTLE_FILE)
        raise
    write_active(name)
    # The switch itself is what invalidates the cache: `_auto_backup_active`
    # ran before the swap and left it naming the account we just left, so the
    # statusline would show the old one until some later command happened to
    # resolve again. Re-armed here because the window opened before the swap.
    _cache_live_name(name)
    write_epoch_file(SETTLE_FILE, _now() + SETTLE_SECONDS)
    return new_creds, new_oauth


def cmd_use(args: argparse.Namespace) -> int:
    """Switch to a named profile.

    Takes the same lock as the automatic path: a manual switch runs the
    identical read-decide-write sequence, so racing a statusline tick would
    let either side overwrite the other's `.active`, credentials, and saved
    profile — and silently undo the choice the user just made.
    """
    ensure_dirs()
    name: str = args.name
    if not profile_path(name).exists():
        die(f"profile not found: {name}", EXIT_USER)

    with profile_lock() as acquired:
        if not acquired:
            die("another cc-switch operation is in progress — try again in a moment", EXIT_USER)
        new_creds, new_oauth = _switch_to(name)
        # The gate describes one specific active account; a manual move
        # invalidates it just as much as an automatic one.
        recompute_gate(name, _now())

    email = new_oauth.get("emailAddress", "?")
    sub = new_creds.get("claudeAiOauth", {}).get("subscriptionType", "?")
    print(f"Switched to '{name}' ({email}, {sub})")
    return EXIT_OK


def _profile_summary(p: Path) -> tuple[str, str]:
    """Return (email, subscriptionType) for a saved profile file.

    Returns sentinel values on any read/parse error so list/current never
    crash on a single malformed profile.
    """
    try:
        data, _, _ = _parse_json_object(p)
    except (OSError, ValueError, TypeError):  # ValueError covers JSON and UTF-8 errors
        return "<corrupted>", "?"
    oauth = data.get("oauthAccount")
    creds = data.get("credentials")
    email = oauth.get("emailAddress", "?") if isinstance(oauth, dict) else "?"
    if isinstance(creds, dict):
        claude_oauth = creds.get("claudeAiOauth")
        sub = claude_oauth.get("subscriptionType", "?") if isinstance(claude_oauth, dict) else "?"
    else:
        sub = "?"
    return email, sub


def cmd_list(_args: argparse.Namespace) -> int:
    ensure_dirs()
    # The marker can name an account Claude Code has already logged out of;
    # showing the wrong one as active is how a user ends up acting on it.
    active = resolve_active()
    profiles = list_profiles()
    if not profiles:
        print("No profiles yet. Save the current one: cc-switch add <name>")
        return EXIT_OK

    now = _now()
    rows: list[tuple[bool, str, str, str, str, str]] = []
    for p in profiles:
        name = p.stem
        email, sub = _profile_summary(p)
        reason, expiry = login_status_for(name, active, now)
        login = _fmt_login_expiry(expiry, now)
        rows.append((name == active, name, email, sub, login, _login_note(reason, expiry, now)))

    name_w = max(len(r[1]) for r in rows)
    email_w = max(len(r[2]) for r in rows)
    sub_w = max(len(r[3]) for r in rows) + 2
    for is_active, name, email, sub, login, note in rows:
        marker = "*" if is_active else " "
        print(f"{marker} {name:<{name_w}}  {email:<{email_w}}  {f'({sub})':<{sub_w}}  login until {login}{note}")
    return EXIT_OK


def profile_login_expiry(data: object) -> float | None:
    """Epoch when this profile's login stops working, or None when unknown.

    The refresh token is what a login is: once it expires, no amount of
    switching helps and the account needs `claude` to sign in again.
    """
    if not isinstance(data, dict):
        return None
    creds = data.get("credentials")
    # Both levels are checked: a hand-edited profile can hold a string or a
    # list at either one, and `list` must still render every other account.
    oauth = creds.get("claudeAiOauth") if isinstance(creds, dict) else None
    return refresh_expiry_epoch(oauth)


def login_status_for(name: str, active: str | None, now: float) -> tuple[str | None, float | None]:
    """Why this account cannot be used, and when its login expires.

    Both answers come from one copy: the live credentials for the account
    that is signed in, the saved profile for every other. Claude Code renews
    the live credentials in place while the saved profile keeps whatever it
    held when it was last written, so reading the date from one and the
    verdict from the other prints a future "login until" beside
    `! refresh token expired` for anyone who has just signed in again.
    """
    if name == active:
        reason, expiry = live_credentials_status(now)
        # Missing or damaged answers with the statusline's sentinel, not a
        # date; formatting it would invent "01 Jan 00:00 (EXPIRED)".
        return reason, (None if reason is not None else expiry or None)
    data = load_profile_data(name)
    return profile_unusable_reason(data, now), profile_login_expiry(data)


def _fmt_login_expiry(expiry: float | None, now: float) -> str:
    """A date plus how far off it is — the two questions asked together."""
    if expiry is None:
        return "unknown"
    stamp = _dt.datetime.fromtimestamp(expiry).strftime("%d %b %H:%M")
    remaining = expiry - now
    if remaining <= 0:
        return f"{stamp} (EXPIRED)"
    hours = remaining / 3600
    if hours < 48:
        return f"{stamp} (in {hours:.0f}h)"
    return f"{stamp} (in {hours / 24:.0f}d)"


def _login_note(reason: str | None, expiry: float | None, now: float) -> str:
    """Flag an account that cannot be switched to, or is about to expire.

    Takes the verdict already computed for the displayed date rather than
    looking it up again, so the two can never come from different copies.
    """
    if reason is not None:
        return f"  ! {reason}"
    if expiry is not None:
        hours_left = (expiry - now) / 3600
        if hours_left < 48:
            return f"  ! login expires in {hours_left:.0f}h"
    return ""


def _profile_email(p: Path) -> str | None:
    try:
        data, _, _ = _parse_json_object(p)
    except (OSError, ValueError, TypeError):  # ValueError covers JSON and UTF-8 errors
        return None
    oauth = data.get("oauthAccount")
    if not isinstance(oauth, dict):
        return None
    email = oauth.get("emailAddress")
    return email if isinstance(email, str) else None


def _cache_live_name(name: str | None, live_email: str | None = None) -> None:
    """Record the resolved account for the statusline to display.

    Written from `resolve_active` itself rather than from one command, so any
    cc-switch invocation refreshes it — including `current` and `list`, which
    run when auto-switching is off and no tick would ever fire.

    Those readers hold no lock, so a switch can land between resolving and
    writing here; the stale name would then sit in the cache until something
    else resolved again. Re-reading the live email immediately before the
    write closes that window: if it no longer matches what was resolved, the
    switch already wrote the correct name and this one is obsolete.
    """
    if live_email is not None:
        state = read_current_state(strict=False)
        current_email = state[1].get("emailAddress") if state else None
        if current_email != live_email:
            return  # a switch landed while we were resolving; its write wins
    desired = f"{name}\n" if name else "\n"
    # ValueError as well as OSError: invalid UTF-8 in the cache raises
    # UnicodeDecodeError, and crashing here would make a completed switch
    # report failure after the credentials had already changed.
    with contextlib.suppress(OSError, ValueError):
        if LIVE_FILE.read_text(encoding="utf-8") == desired:
            return  # unchanged; skip the write
    if write_text_file(LIVE_FILE, desired, fatal=False):
        if live_email is not None and _live_email() != live_email:
            # A switch landed while this write was in flight, so what we just
            # stored is already wrong. The switch writes the cache under the
            # lock; re-running the resolve puts its answer back.
            _cache_live_name(_resolve_active_uncached())
        return
    # The cache outranks `.active` in the statusline, so leaving a name we
    # could not update would display the wrong account indefinitely. Dropping
    # it falls back to the marker until the next resolve succeeds.
    try:
        LIVE_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        log_decision(f"stale live-account cache could not be cleared: {e}")


def _resolve_active_uncached() -> str | None:
    state = read_current_state(strict=False)
    live_email = state[1].get("emailAddress") if state else None
    if live_email:
        marker = read_active()
        if marker and _profile_email(profile_path(marker)) == live_email:
            return marker
        for p in list_profiles():
            if _profile_email(p) == live_email:
                return p.stem
        return None
    # No live email to match against (never logged in, or a stripped account
    # object). The marker is all we have; it is still better than nothing.
    marker = read_active()
    return marker if marker and profile_path(marker).exists() else None


def _live_email() -> str | None:
    state = read_current_state(strict=False)
    return state[1].get("emailAddress") if state else None


def resolve_active() -> str | None:
    """The profile whose credentials are actually live.

    The live credentials win over the `.active` marker, which is a hint that
    goes stale the moment someone logs into a different account through Claude
    Code itself. This is a safety decision, not cosmetics: everything
    downstream refuses to refresh *the active profile*, so naming the wrong
    one would rotate the token out from under the running session — exactly
    what that rule exists to prevent.

    Every answer refreshes the `.live` cache the statusline displays, so any
    cc-switch invocation keeps it honest — not only the ones that recompute
    the gate.
    """
    resolved_for = _live_email()
    name = _resolve_active_uncached()
    _cache_live_name(name, live_email=resolved_for)
    return name


def cmd_current(_args: argparse.Namespace) -> int:
    ensure_dirs()
    resolved = resolve_active()
    if resolved is not None:
        now = _now()
        reason, expiry = login_status_for(resolved, resolved, now)
        note = _login_note(reason, expiry, now)
        print(f"{resolved}  (login until {_fmt_login_expiry(expiry, now)}){note}")
        return EXIT_OK

    state = read_current_state(strict=False)
    if state is None:
        print("No active profile (no current credentials)")
        return EXIT_OK
    print(f"No active profile (current email: {state[1].get('emailAddress') or '?'})")
    return EXIT_OK


def cmd_remove(args: argparse.Namespace) -> int:
    ensure_dirs()
    name: str = args.name
    path = profile_path(name)
    if not path.exists():
        die(f"profile not found: {name}", EXIT_USER)
    if not confirm(f"Remove profile '{name}'?"):
        print("Cancelled.")
        return EXIT_OK
    with profile_lock() as acquired:
        if not acquired:
            die("another cc-switch operation is in progress — try again in a moment", EXIT_USER)
        try:
            path.unlink()
        except OSError as e:
            die(f"failed to remove {path}: {e}")
        if read_active() == name:
            # Only the marker is cleared here — matching it by name is right,
            # since we are removing that exact saved profile.
            try:
                ACTIVE_FILE.unlink()
            except OSError:
                pass
        # The gate ranks candidates; removing one makes it stale immediately.
        recompute_gate(resolve_active(), _now())
    print(f"Removed profile '{name}'")
    return EXIT_OK


# ---- Auto-switch commands ---------------------------------------------


def _fmt_epoch(epoch: float) -> str:
    if epoch <= 0:
        return "unknown"
    return _dt.datetime.fromtimestamp(epoch).strftime("%d %b %H:%M")


def cmd_auto(args: argparse.Namespace) -> int:
    """Turn auto-switching on or off. The flag is permanent until changed.

    On and off take the lock so the reported state is the real one: a tick
    holding the lock re-reads the flag before deciding, so once `auto off`
    returns, no already-running tick can still switch accounts.
    """
    ensure_dirs()
    action: str = args.action
    if action in ("on", "off"):
        with profile_lock() as acquired:
            if not acquired:
                die("another cc-switch operation is in progress — try again in a moment", EXIT_USER)
            if action == "on":
                write_text_file(AUTO_FILE, "on\n")
                clear_state_file(EXHAUSTED_FILE)
                recompute_gate(resolve_active(), _now())
                print("Auto-switching enabled")
            else:
                clear_state_file(AUTO_FILE)
                clear_state_file(GATE_FILE)
                print("Auto-switching disabled")
        return EXIT_OK
    print(f"Auto-switching {'enabled' if auto_enabled() else 'disabled'}")
    exhausted = read_epoch_file(EXHAUSTED_FILE)
    if exhausted > _now():
        print(f"All accounts exhausted until {_fmt_epoch(exhausted)}")
    print(
        f"Thresholds: leave at 5h>={EXIT_5H:.0f}% or 7d>={EXIT_7D:.0f}%, "
        f"enter below 5h {ENTER_5H:.0f}% / 7d {ENTER_7D:.0f}%, balance gap {BALANCE_GAP_7D:.0f}pp"
    )
    return EXIT_OK


def _perform_auto_switch(active: str | None, name: str, reason: str, now: float) -> None:
    _switch_to(name, quiet=True)  # arms the settle window
    clear_state_file(EXHAUSTED_FILE)
    recompute_gate(name, now)
    log_decision(f"switch {active or '?'} -> {name} ({reason})")


def _no_candidate_left(active: str | None, reason: str, now: float, transient: bool = False) -> int:
    """Stay put. Only proven exhaustion earns a deadline.

    Two cases must not write one. A balance evaluation that found nothing
    would silence the limit path until the weekly window rolls over. And a
    candidate that merely could not be reached is no evidence at all — a
    network blip would suppress switching for hours or days while the account
    actually had room. Both fall back to a short retry pause instead.
    """
    if reason == "expired":
        # The login deadline is already behind us, so the statusline would
        # wake a tick on every single render. Back off like any other failed
        # attempt: nothing changes until an account frees up or a human logs
        # in, and both are minutes-scale events at best.
        write_epoch_file(SETTLE_FILE, now + RETRY_SECONDS)
        log_decision(f"no working account to escape to; retrying after {int(RETRY_SECONDS)}s")
        recompute_gate(active, now)
        return EXIT_OK
    if reason != "limit":
        recompute_gate(active, now)
        return EXIT_OK
    if transient:
        write_epoch_file(SETTLE_FILE, now + RETRY_SECONDS)
        log_decision(f"no candidate reachable; retrying after {int(RETRY_SECONDS)}s")
        recompute_gate(active, now)
        return EXIT_OK
    deadline = earliest_future_reset([p.stem for p in list_profiles()], now)
    if deadline > 0:
        write_epoch_file(EXHAUSTED_FILE, deadline)
    log_decision(f"all accounts exhausted; earliest reset {_fmt_epoch(deadline)}")
    recompute_gate(active, now)
    return EXIT_OK


def _try_candidates(
    active: str | None,
    ranked: list[Candidate],
    reason: str,
    active_usage: tuple[float, float],
    now: float,
) -> int:
    """Confirm candidates in order; the first authorized one with headroom wins.

    Each candidate is confirmed at most once, so this terminates even when
    every stored snapshot turns out to have been optimistic.
    """
    five, seven = active_usage
    unreachable = False
    for candidate in ranked:
        result = confirm_candidate(candidate.name, active)
        if result.usage is None:
            unreachable = unreachable or result.transient
            log_decision(f"skip {candidate.name}: {result.error}")
            continue
        live_5h, live_7d = effective_usage(result.usage, now)
        if live_5h > ENTER_5H or live_7d > ENTER_7D:
            log_decision(f"skip {candidate.name}: live 5h={live_5h:.0f}% 7d={live_7d:.0f}%")
            continue
        if switch_reason(five, seven, live_7d, expired=reason == "expired") is None:
            log_decision(f"skip {candidate.name}: live 7d={live_7d:.0f}% leaves no reason to move")
            continue
        _perform_auto_switch(active, candidate.name, reason, now)
        return EXIT_OK
    return _no_candidate_left(active, reason, now, transient=unreachable)


def _tick_locked(args: argparse.Namespace, now: float) -> int:
    active = resolve_active()
    dead = live_credentials_dead(now)
    if dead is not None:
        # No usage figures arrive once the login is dead, so this cannot wait
        # for a threshold: evacuate on the credentials alone.
        log_decision(f"live login unusable ({dead}); looking for a working account")
        # A dead login has no active account, so none is named here. Whatever
        # `resolve_active` returned can only be the `.active` marker — a
        # guess, since a damaged credentials file leaves no email to match a
        # profile against. Excluding that guess from the ranking hid the one
        # profile whose saved copy may still hold a working login, and naming
        # it as live made `confirm_candidate` reach for a token in the very
        # file that is the problem. The running session stays protected
        # either way: the rule that never refreshes it matches by email
        # against the live credentials, so it holds whenever there is a
        # session left to protect.
        return _try_candidates(None, rank_candidates(now, None), "expired", (0.0, 0.0), now)
    if active is None:
        return EXIT_OK
    five, seven = float(args.five_hour), float(args.seven_day)
    # A garbled statusline payload must not be persisted: a NaN makes every
    # threshold comparison false, so an exhausted account would never be
    # evacuated, and the snapshot would poison later rankings too.
    if not (math.isfinite(five) and math.isfinite(seven)):
        log_decision(f"ignoring non-finite usage from the statusline: 5h={five} 7d={seven}")
        return EXIT_OK
    five = min(max(five, 0.0), 100.0)
    seven = min(max(seven, 0.0), 100.0)
    record_usage_snapshot(
        active,
        make_snapshot(five, seven, timestamp_arg_to_iso(args.resets_5h), timestamp_arg_to_iso(args.resets_7d)),
    )
    ranked = rank_candidates(now, active)
    reason = switch_reason(five, seven, ranked[0].seven_day if ranked else 100.0)
    if reason is None:
        recompute_gate(active, now)
        return EXIT_OK
    return _try_candidates(active, ranked, reason, (five, seven), now)


def _tick_is_still_wanted(now: float) -> bool:
    """Re-read the flag and the deadlines. Called again under the lock.

    Everything checked before acquiring the lock can change while we wait for
    it: the user can run `auto off`, or a manual `use` can switch accounts and
    arm `.settle`. Acting on the pre-lock answer would switch credentials
    after auto-switching was turned off, or overwrite a manual choice using
    statusline percentages that now describe the wrong account.
    """
    if not auto_enabled():
        return False
    if read_epoch_file(SETTLE_FILE) > now:
        return False
    if read_epoch_file(EXHAUSTED_FILE) <= now:
        return True
    # An exhaustion deadline says every account was at its limit, and it can
    # be days out. A dead login is a different and worse problem — the
    # session has stopped working entirely — and it outranks limits
    # everywhere else, so it must not wait behind one. The settle window
    # above still applies: that one exists to stop a switch from immediately
    # undoing itself.
    return live_credentials_dead(now) is not None


def cmd_tick(args: argparse.Namespace) -> int:
    """One evaluation, driven by the statusline. Never loops, never blocks."""
    ensure_dirs()
    now = _now()
    # Cheap pre-check: skip the lock entirely in the common no-op case.
    if not _tick_is_still_wanted(now):
        return EXIT_OK
    with profile_lock() as acquired:
        if not acquired:
            return EXIT_OK
        if not _tick_is_still_wanted(now):
            return EXIT_OK
        return _tick_locked(args, now)


def cmd_pick(_args: argparse.Namespace) -> int:
    """Move to the account with the lowest weekly usage."""
    ensure_dirs()
    now = _now()
    with profile_lock() as acquired:
        if not acquired:
            die("another cc-switch decision is already running", EXIT_USER)
        # Resolved inside the lock: a switch completing between an unlocked
        # read and here would leave us calling the newly live account a
        # candidate and refreshing the running session's token.
        #
        # A dead login has no active account, exactly as in the automatic
        # rescue: `resolve_active` can then only be echoing the `.active`
        # marker, and excluding that guess would hide the profile holding
        # the one working login left.
        active = None if live_credentials_dead(now) else resolve_active()
        return _pick_locked(active, now)


def _pick_locked(active: str | None, now: float) -> int:
    """Check every candidate live, then take the genuinely lowest one.

    Stopping at the first candidate that is not better than the active
    account would miss a lower one behind it — stored snapshots order the
    checks, but only the live numbers decide.
    """
    active_7d = 100.0
    if active and profile_path(active).exists():
        current = confirm_candidate(active, active)
        if current.usage is not None:
            _, active_7d = effective_usage(current.usage, now)

    best: tuple[str, float] | None = None
    for candidate in rank_candidates(now, active):
        result = confirm_candidate(candidate.name, active)
        if result.usage is None:
            print(f"Skipping '{candidate.name}': {result.error}")
            continue
        live_5h, live_7d = effective_usage(result.usage, now)
        if live_5h > ENTER_5H or live_7d > ENTER_7D:
            print(f"Skipping '{candidate.name}': 5h {live_5h:.0f}%, 7d {live_7d:.0f}%")
            continue
        if best is None or live_7d < best[1]:
            best = (candidate.name, live_7d)

    if best is None:
        print("No usable account with headroom")
        return EXIT_OK
    if best[1] >= active_7d:
        print(f"Staying on '{active}' (7d {active_7d:.0f}%) — nothing lower available")
        return EXIT_OK
    _perform_auto_switch(active, best[0], "pick", now)
    print(f"Switched to '{best[0]}' (7d {best[1]:.0f}%, was {active_7d:.0f}%)")
    return EXIT_OK


def _usage_row(name: str, active: str | None, now: float) -> dict[str, Any]:
    """Live usage for one profile, falling back to its stored snapshot."""
    data = load_profile_data(name)
    email, _ = _profile_summary(profile_path(name))
    is_active = name == active
    # The saved copy decides nothing about the account signed in right now:
    # a stale "refresh token expired" there would report the live account as
    # dead and show stored limits instead of fetching real ones. A dead live
    # login is reported as itself rather than as the HTTP failure that
    # fetching with it would produce.
    reason, expiry = login_status_for(name, active, now)
    row: dict[str, Any] = {
        "profile": name,
        "email": email,
        "active": is_active,
        "login_expires_at": expiry,
    }
    if reason is not None:
        stored = (data or {}).get("usage")
        row.update(_usage_fields(stored), status=reason, stale=True)
        return row
    result = confirm_candidate(name, active)
    if result.usage is None:
        row.update(_usage_fields((data or {}).get("usage")), status=result.error or "unavailable", stale=True)
        return row
    row.update(_usage_fields(result.usage), status="active" if name == active else "ok", stale=False)
    return row


def _usage_fields(snapshot: object) -> dict[str, Any]:
    """Flatten a stored snapshot for display, tolerating any shape.

    One hand-edited profile must not stop `usage` from reporting the others,
    so a window that is not an object simply reads as unknown.
    """
    windows = snapshot if isinstance(snapshot, dict) else {}
    five = windows.get("five_hour")
    seven = windows.get("seven_day")
    five = five if isinstance(five, dict) else {}
    seven = seven if isinstance(seven, dict) else {}
    return {
        "five_hour": five.get("utilization"),
        "seven_day": seven.get("utilization"),
        "resets_5h": five.get("resets_at"),
        "resets_7d": seven.get("resets_at"),
    }


def _pct(value: object) -> str:
    return f"{float(value):.0f}%" if isinstance(value, (int, float)) else "?"


def _reset_cell(row: dict[str, Any]) -> str:
    five = parse_iso(row.get("resets_5h"))
    seven = parse_iso(row.get("resets_7d"))
    return f"{_fmt_epoch(five or 0)} / {_fmt_epoch(seven or 0)}"


def cmd_usage(args: argparse.Namespace) -> int:
    """Refresh every profile's limits from the API and print them.

    Refreshing rotates tokens, so this holds the lock too. Two processes
    refreshing the same expired token would each invalidate the other's: the
    loser gets `invalid_grant` and retires a profile that is perfectly fine.
    """
    ensure_dirs()
    profiles = list_profiles()
    if not profiles:
        print("No profiles yet. Save the current one: cc-switch add <name>")
        return EXIT_OK
    now = _now()
    with profile_lock() as acquired:
        if not acquired:
            die("another cc-switch operation is in progress — try again in a moment", EXIT_USER)
        active = resolve_active()
        rows = [_usage_row(p.stem, active, now) for p in profiles]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return EXIT_OK
    name_w = max(*(len(str(r["profile"])) for r in rows), len("PROFILE"))
    email_w = max(*(len(str(r["email"])) for r in rows), len("EMAIL"))
    logins = [_fmt_login_expiry(r["login_expires_at"], now) for r in rows]
    login_w = max(*(len(t) for t in logins), len("LOGIN UNTIL"))
    reset_w = max(*(len(_reset_cell(r)) for r in rows), len("RESETS 5h / 7d"))
    header = (
        f"  {'PROFILE':<{name_w}}  {'EMAIL':<{email_w}}  {'5h':>5}  {'7d':>5}  "
        f"{'RESETS 5h / 7d':<{reset_w}}  {'LOGIN UNTIL':<{login_w}}  STATUS"
    )
    print(header)
    for row, login in zip(rows, logins, strict=True):
        marker = "*" if row["active"] else " "
        stale = " (stored)" if row["stale"] else ""
        print(
            f"{marker} {row['profile']:<{name_w}}  {row['email']:<{email_w}}  "
            f"{_pct(row['five_hour']):>5}  {_pct(row['seven_day']):>5}  "
            f"{_reset_cell(row):<{reset_w}}  {login:<{login_w}}  {row['status']}{stale}"
        )
    return EXIT_OK


# ---- Entry point ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cc-switch",
        description="Switch Claude Code OAuth accounts (swaps only credentials; leaves hooks/skills/projects alone).",
    )
    sub = p.add_subparsers(dest="cmd", metavar="command")

    a = sub.add_parser("add", help="save the current active account as a profile")
    a.add_argument("name", help="profile name")
    a.set_defaults(func=cmd_add)

    u = sub.add_parser("use", help="switch to a saved profile")
    u.add_argument("name", help="profile name")
    u.set_defaults(func=cmd_use)

    lst = sub.add_parser("list", help="list all profiles")
    lst.set_defaults(func=cmd_list)

    c = sub.add_parser("current", help="print the active profile name")
    c.set_defaults(func=cmd_current)

    r = sub.add_parser("remove", help="delete a saved profile")
    r.add_argument("name", help="profile name")
    r.set_defaults(func=cmd_remove)

    a2 = sub.add_parser("auto", help="turn automatic switching on or off")
    a2.add_argument("action", choices=("on", "off", "status"), nargs="?", default="status")
    a2.set_defaults(func=cmd_auto)

    t = sub.add_parser("tick", help="internal: one auto-switch evaluation (called by the statusline)")
    t.add_argument("--5h", dest="five_hour", type=float, required=True, help="active account 5h utilization")
    t.add_argument("--7d", dest="seven_day", type=float, required=True, help="active account 7d utilization")
    t.add_argument(
        "--resets-5h", dest="resets_5h", default=None,
        help="epoch seconds (statusline) or ISO timestamp (API), may be empty",
    )
    t.add_argument(
        "--resets-7d", dest="resets_7d", default=None,
        help="epoch seconds (statusline) or ISO timestamp (API), may be empty",
    )
    t.set_defaults(func=cmd_tick)

    pk = sub.add_parser("pick", help="switch to the account with the lowest weekly usage")
    pk.set_defaults(func=cmd_pick)

    u2 = sub.add_parser("usage", help="refresh and show limits for every profile")
    u2.add_argument("--json", action="store_true", help="machine-readable output")
    u2.set_defaults(func=cmd_usage)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        cmd_list(args)
        print()
        parser.print_help()
        return EXIT_OK
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(EXIT_USER)
