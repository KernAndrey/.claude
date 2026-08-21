"""Behavioural tests for statusline-command.sh — it is run, not grepped.

Asserting that the script *contains* a string proved worthless: adding a
fifth field to the gate silently broke every percentage comparison, because
`read -r a b c d` folds the remainder into `d`. The script rendered fine and
printed an error to stderr that no substring test could see.

These run the real shell against a temp profiles directory and a stub
cc-switch, and assert on what it printed and whether it spawned a tick.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

SCRIPT = _ROOT.parent / "statusline-command.sh"

PAYLOAD = {
    "model": {"display_name": "Opus 5"},
    "context_window": {"used_percentage": 8},
    "rate_limits": {
        "five_hour": {"used_percentage": 26, "resets_at": "2026-08-21T19:00:00Z"},
        "seven_day": {"used_percentage": 48, "resets_at": "2026-08-27T04:00:00Z"},
    },
}


class Harness:
    """A throwaway HOME with a stub cc-switch that records its arguments."""

    def __init__(self, home: Path) -> None:
        self.home = home
        self.profiles = home / ".claude-profiles"
        self.profiles.mkdir(parents=True)
        (home / ".claude" / "cc-switch").mkdir(parents=True)
        self.creds = home / ".claude" / ".credentials.json"
        self.calls = home / "tick-calls.log"
        self.stub = home / ".claude" / "cc-switch" / "cc_switch.py"
        self.stub.write_text(f'#!/usr/bin/env bash\necho "$@" >> {self.calls}\n')
        self.stub.chmod(0o755)

    def write_creds(self, body: str | None = None) -> None:
        self.creds.write_text(body if body is not None else json.dumps({"claudeAiOauth": {"refreshToken": "r"}}))

    def write_gate(
        self,
        not_before: int,
        recheck: int,
        t5h: float,
        t7d: float,
        deadline: int,
        settle: int = 0,
    ) -> None:
        """Triggers are given as percentages here and written in tenths.

        `not_before` gates the percentage comparisons and carries both
        deadlines; `settle` gates the dead-login check and carries only the
        window after a switch.
        """
        (self.profiles / ".gate").write_text(
            f"{not_before} {recheck} {int(t5h * 10)} {int(t7d * 10)} {deadline} {settle}\n"
        )

    def enable_auto(self) -> None:
        (self.profiles / ".auto").write_text("on\n")

    def run(self, payload: dict[str, object] | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ, HOME=str(self.home))
        # Compact, like the real harness: the script's grep-based parser
        # matches `"key":"value"` with no space after the colon.
        body = json.dumps(PAYLOAD if payload is None else payload, separators=(",", ":"))
        return subprocess.run(
            ["bash", str(SCRIPT)],
            input=body,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def ticks(self, expected: int = 1, timeout: float = 5.0) -> list[str]:
        """Recorded tick invocations, waited for rather than sampled.

        The script launches the tick with `( ... & )`, so `subprocess.run`
        returns as soon as the parent shell exits — the child may not have
        written its line yet. Reading immediately would make every spawn
        assertion flaky.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            lines = self.calls.read_text().splitlines() if self.calls.exists() else []
            if len(lines) >= expected:
                return lines
            time.sleep(0.02)
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def no_ticks(self, settle: float = 0.5) -> list[str]:
        """Assert-nothing-happened needs a pause, or it proves nothing.

        A background child that has not started yet looks identical to one
        that was never launched.
        """
        time.sleep(settle)
        return self.calls.read_text().splitlines() if self.calls.exists() else []


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    h = Harness(tmp_path)
    h.write_creds()
    return h


class TestItRunsCleanly:
    def test_no_error_output_with_a_full_gate(self, harness: Harness) -> None:
        """The regression: five gate fields read into four variables."""
        harness.enable_auto()
        harness.write_gate(0, 0, 95, 47, int(time.time()) + 86400)
        result = harness.run()
        assert result.stderr == "", result.stderr
        assert result.returncode == 0

    def test_the_line_is_still_rendered(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_gate(0, 0, 95, 47, int(time.time()) + 86400)
        assert "Opus 5" in harness.run().stdout

    def test_no_error_without_a_gate(self, harness: Harness) -> None:
        harness.enable_auto()
        assert harness.run().stderr == ""

    def test_no_error_with_auto_off(self, harness: Harness) -> None:
        assert harness.run().stderr == ""


class TestThePercentageGate:
    """The comparisons that the field-count bug silently disabled."""

    def test_below_both_triggers_nothing_spawns(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert harness.no_ticks() == []

    def test_the_weekly_trigger_fires(self, harness: Harness) -> None:
        """48% against a trigger of 47 — the balancing wake-up."""
        harness.enable_auto()
        harness.write_gate(0, 0, 95, 47, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1
        assert "--7d 48" in harness.ticks()[0]

    def test_the_five_hour_trigger_fires(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_gate(0, 0, 20, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1
        assert "--5h 26" in harness.ticks()[0]

    def test_a_fractional_threshold_is_reachable(self, harness: Harness) -> None:
        """95.2% must clear a 95.1% threshold — the ceiling made it never fire."""
        payload = json.loads(json.dumps(PAYLOAD))
        payload["rate_limits"]["five_hour"]["used_percentage"] = 95.2  # type: ignore[index]
        harness.enable_auto()
        harness.write_gate(0, 0, 95.1, 99, int(time.time()) + 86400)
        harness.run(payload)
        assert len(harness.ticks()) == 1

    def test_just_below_a_fractional_threshold_does_not_fire(self, harness: Harness) -> None:
        payload = json.loads(json.dumps(PAYLOAD))
        payload["rate_limits"]["five_hour"]["used_percentage"] = 95.0  # type: ignore[index]
        harness.enable_auto()
        harness.write_gate(0, 0, 95.1, 99, int(time.time()) + 86400)
        harness.run(payload)
        assert harness.no_ticks() == []

    def test_a_fractional_weekly_threshold_is_reachable(self, harness: Harness) -> None:
        payload = json.loads(json.dumps(PAYLOAD))
        payload["rate_limits"]["seven_day"]["used_percentage"] = 47.3  # type: ignore[index]
        harness.enable_auto()
        harness.write_gate(0, 0, 95, 47.2, int(time.time()) + 86400)
        harness.run(payload)
        assert len(harness.ticks()) == 1

    def test_an_integer_percentage_still_compares(self, harness: Harness) -> None:
        payload = json.loads(json.dumps(PAYLOAD))
        payload["rate_limits"]["five_hour"]["used_percentage"] = 96  # type: ignore[index]
        harness.enable_auto()
        harness.write_gate(0, 0, 95.1, 99, int(time.time()) + 86400)
        harness.run(payload)
        assert len(harness.ticks()) == 1

    def test_exact_percentages_are_passed_not_floored(self, harness: Harness) -> None:
        payload = json.loads(json.dumps(PAYLOAD))
        payload["rate_limits"]["five_hour"]["used_percentage"] = 95.5  # type: ignore[index]
        harness.enable_auto()
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run(payload)
        assert "--5h 95.5" in harness.ticks()[0]

    def test_not_before_suppresses_everything(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_gate(int(time.time()) + 3600, 0, 20, 20, int(time.time()) + 86400)
        harness.run()
        assert harness.no_ticks() == []

    def test_a_missing_gate_bootstraps_one_tick(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.run()
        assert len(harness.ticks()) == 1

    def test_auto_off_never_spawns(self, harness: Harness) -> None:
        harness.write_gate(0, 0, 20, 20, int(time.time()) + 86400)
        harness.run()
        assert harness.no_ticks() == []


class TestTheDeadLoginTrigger:
    """The only path out of a session that reports no usage at all."""

    def test_a_past_deadline_spawns(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_gate(0, 0, 95, 99, int(time.time()) - 1)
        harness.run()
        assert len(harness.ticks()) == 1
        assert "--5h 0" in harness.ticks()[0]

    def test_missing_credentials_spawn(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.creds.unlink()
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_damaged_credentials_spawn(self, harness: Harness) -> None:
        """Corrupted after the deadline was written: neither other test fires."""
        harness.enable_auto()
        harness.write_creds("{not json")
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_credentials_without_a_refresh_token_spawn(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_creds(json.dumps({"claudeAiOauth": {"accessToken": "a"}}))
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_only_the_expiry_field_counts_as_dead(self, harness: Harness) -> None:
        """`refreshTokenExpiresAt` contains `refreshToken` as a prefix.

        A logged-out file holding only the expiry therefore looked alive to
        the looser pattern, and with a future deadline nothing ever fired.
        """
        harness.enable_auto()
        harness.write_creds(json.dumps({"claudeAiOauth": {"refreshTokenExpiresAt": 123}}))
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_a_real_refresh_token_is_alive(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_creds(json.dumps({"claudeAiOauth": {"refreshToken": "r", "refreshTokenExpiresAt": 123}}))
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert harness.no_ticks() == []

    @pytest.mark.parametrize(
        "gap",
        [" ", "  ", "\t", "\n", "\n    ", " \t\n "],
        ids=["one-space", "two-spaces", "tab", "newline", "newline-indent", "mixed"],
    )
    def test_any_whitespace_after_the_colon_is_still_alive(self, harness: Harness, gap: str) -> None:
        """JSON allows any amount of it, and a pretty-printer will use it.

        Enumerating the spellings called a healthy file dead on every render,
        and each render then spawned a tick that found nothing wrong — so
        nothing ever armed a back-off either.
        """
        harness.enable_auto()
        harness.write_creds('{"claudeAiOauth":{"refreshToken":' + gap + '"r"}}')
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert harness.no_ticks() == []

    @pytest.mark.parametrize(
        "gap",
        [" ", "\t", "\n    "],
        ids=["space", "tab", "newline-indent"],
    )
    def test_an_empty_token_is_dead_however_it_is_spaced(self, harness: Harness, gap: str) -> None:
        harness.enable_auto()
        harness.write_creds('{"claudeAiOauth":{"refreshToken":' + gap + '""}}')
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_a_pretty_printed_file_is_alive(self, harness: Harness) -> None:
        """The shape `json.dump(indent=2)` produces, end to end."""
        harness.enable_auto()
        harness.write_creds(json.dumps({"claudeAiOauth": {"refreshToken": "r"}}, indent=2) + "\n")
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert harness.no_ticks() == []

    def test_a_truncated_file_is_dead(self, harness: Harness) -> None:
        """A plausible token inside a file that never closes proves nothing.

        Caught mid-write, the substring test found `"refreshToken":"r"` and
        called the login healthy — and since a logged-out session reports no
        usage, no other trigger would ever have fired.
        """
        harness.enable_auto()
        harness.write_creds('{"claudeAiOauth":{"refreshToken":"r"')
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_an_empty_file_is_dead(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_creds("")
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_a_trailing_newline_is_still_alive(self, harness: Harness) -> None:
        """The closing brace check must not trip over how a file ends."""
        harness.enable_auto()
        harness.write_creds('{"claudeAiOauth":{"refreshToken":"r"}}\n')
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert harness.no_ticks() == []

    def test_trailing_whitespace_is_still_alive(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_creds('{"claudeAiOauth":{"refreshToken":"r"}}  \n\n')
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert harness.no_ticks() == []

    def test_a_null_refresh_token_is_dead(self, harness: Harness) -> None:
        """Presence is not usability — these are all logged-out shapes."""
        harness.enable_auto()
        harness.write_creds(json.dumps({"claudeAiOauth": {"refreshToken": None}}))
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_an_empty_refresh_token_is_dead(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_creds(json.dumps({"claudeAiOauth": {"refreshToken": ""}}))
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_an_empty_refresh_token_with_a_space_is_dead(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_creds('{"claudeAiOauth": {"refreshToken": ""}}')
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_a_numeric_refresh_token_is_dead(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_creds(json.dumps({"claudeAiOauth": {"refreshToken": 123}}))
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_healthy_credentials_do_not_spawn(self, harness: Harness) -> None:
        harness.enable_auto()
        harness.write_gate(0, 0, 95, 99, int(time.time()) + 86400)
        harness.run()
        assert harness.no_ticks() == []

    def test_the_settle_window_suppresses_the_dead_login_spawn(self, harness: Harness) -> None:
        """Otherwise a dead login with nowhere to go spawns on every render."""
        harness.enable_auto()
        harness.creds.unlink()
        harness.write_gate(0, 0, 95, 99, int(time.time()) - 1, settle=int(time.time()) + 3600)
        harness.run()
        assert harness.no_ticks() == []

    def test_an_exhaustion_deadline_does_not_suppress_it(self, harness: Harness) -> None:
        """Being at a limit is no reason to stay logged out.

        `not_before` carries the exhaustion deadline, which can be days out.
        Gating the dead-login check on it left a logged-out session with no
        way to escape until a usage window rolled over.
        """
        harness.enable_auto()
        harness.creds.unlink()
        harness.write_gate(int(time.time()) + 86400, 0, 95, 99, int(time.time()) - 1, settle=0)
        harness.run()
        assert len(harness.ticks()) == 1

    def test_a_gate_without_the_sixth_field_still_checks(self, harness: Harness) -> None:
        """An older gate file must not silence the check it predates."""
        harness.enable_auto()
        harness.creds.unlink()
        (harness.profiles / ".gate").write_text(f"0 0 950 990 {int(time.time()) - 1}\n")
        harness.run()
        assert len(harness.ticks()) == 1

    def test_only_one_tick_even_when_both_gates_match(self, harness: Harness) -> None:
        """A dead login with a stale high payload once matched both."""
        harness.enable_auto()
        harness.creds.unlink()
        harness.write_gate(0, 0, 20, 20, int(time.time()) - 1)
        harness.run()
        assert len(harness.ticks()) == 1


class TestTheDisplayedAccount:
    def test_the_live_file_is_preferred(self, harness: Harness) -> None:
        (harness.profiles / ".live").write_text("vlad\n")
        (harness.profiles / ".active").write_text("me\n")
        assert "vlad" in harness.run().stdout

    def test_the_marker_is_the_fallback(self, harness: Harness) -> None:
        (harness.profiles / ".active").write_text("me\n")
        assert "me" in harness.run().stdout

    def test_an_empty_live_file_falls_back(self, harness: Harness) -> None:
        (harness.profiles / ".live").write_text("\n")
        (harness.profiles / ".active").write_text("me\n")
        assert "me" in harness.run().stdout

    def test_the_exhausted_marker_is_shown(self, harness: Harness) -> None:
        (harness.profiles / ".exhausted").write_text(f"{int(time.time()) + 3600}\n")
        assert "all spent until" in harness.run().stdout

    def test_a_past_exhaustion_is_not_shown(self, harness: Harness) -> None:
        (harness.profiles / ".exhausted").write_text(f"{int(time.time()) - 3600}\n")
        assert "all spent" not in harness.run().stdout
