"""Tests for the revspeed state-file resolver in config.py and the CLI."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
from types import ModuleType

import config
import pytest


@pytest.fixture
def speed_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    p = tmp_path / "kimi-speed"
    monkeypatch.setattr(config, "KIMI_SPEED_FILE", p)
    return p


def test_missing_file_defaults_to_standard(speed_file: Path) -> None:
    assert not speed_file.exists()
    assert config.kimi_review_model() == config._KIMI_STANDARD


def test_highspeed_selects_highspeed_alias(speed_file: Path) -> None:
    speed_file.write_text("highspeed\n", encoding="utf-8")
    assert config.kimi_review_model() == config._KIMI_HIGHSPEED


def test_standard_selects_standard_alias(speed_file: Path) -> None:
    speed_file.write_text("standard\n", encoding="utf-8")
    assert config.kimi_review_model() == config._KIMI_STANDARD


def test_unrecognized_content_defaults_to_standard(speed_file: Path) -> None:
    speed_file.write_text("gibberish\n", encoding="utf-8")
    assert config.kimi_review_model() == config._KIMI_STANDARD


def test_case_and_whitespace_insensitive(speed_file: Path) -> None:
    speed_file.write_text("  HIGHSPEED  \n", encoding="utf-8")
    assert config.kimi_review_model() == config._KIMI_HIGHSPEED


# --- revspeed CLI (review/scripts/kimi-speed) ------------------------------


def _load_kimi_speed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[ModuleType, Path]:
    """Load the extensionless kimi-speed CLI as a module, with both its own and
    config's KIMI_SPEED_FILE pointed at a temp file (write vs read must agree)."""
    path = Path(__file__).resolve().parent / "scripts" / "kimi-speed"
    # kimi-speed has no .py suffix, so give importlib an explicit source loader.
    loader = importlib.machinery.SourceFileLoader("kimi_speed_cli", str(path))
    spec = importlib.util.spec_from_loader("kimi_speed_cli", loader)
    assert spec
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    p = tmp_path / "kimi-speed"
    monkeypatch.setattr(mod, "KIMI_SPEED_FILE", p)  # write_speed target
    monkeypatch.setattr(config, "KIMI_SPEED_FILE", p)  # current_speed() reads via kimi_review_model
    return mod, p


def test_cli_fast_writes_highspeed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod, p = _load_kimi_speed(monkeypatch, tmp_path)
    assert mod.main(["kimi-speed", "fast"]) == 0
    assert p.read_text().strip() == "highspeed"
    assert "highspeed" in capsys.readouterr().out


def test_cli_std_writes_standard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod, p = _load_kimi_speed(monkeypatch, tmp_path)
    assert mod.main(["kimi-speed", "std"]) == 0
    assert p.read_text().strip() == "standard"


def test_cli_toggle_flips_both_directions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mod, p = _load_kimi_speed(monkeypatch, tmp_path)
    p.write_text("standard\n", encoding="utf-8")
    assert mod.main(["kimi-speed", "toggle"]) == 0
    assert p.read_text().strip() == "highspeed"
    assert mod.main(["kimi-speed", "toggle"]) == 0
    assert p.read_text().strip() == "standard"


def test_cli_show_does_not_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod, p = _load_kimi_speed(monkeypatch, tmp_path)
    assert mod.main(["kimi-speed"]) == 0
    assert not p.exists()
    assert "standard" in capsys.readouterr().out


def test_cli_unknown_arg_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mod, _p = _load_kimi_speed(monkeypatch, tmp_path)
    assert mod.main(["kimi-speed", "bogus"]) == 2
    assert "unknown speed" in capsys.readouterr().err
