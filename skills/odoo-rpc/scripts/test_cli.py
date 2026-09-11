#!/usr/bin/env python3
"""Tests for argument parsing, output budgeting and the no-op override refusal."""

from __future__ import annotations

import argparse
import json

import pytest

import odoo_rpc
from config import ConfigError
from odoo_rpc import _emit, _parse_csv, _parse_ids, _parse_json, main


def opts(max_bytes: int = 40000, compact: bool = False) -> argparse.Namespace:
    return argparse.Namespace(max_bytes=max_bytes, compact=compact)


# --- argument parsing ---------------------------------------------------------

def test_ids_accept_commas_and_repetition() -> None:
    assert _parse_ids(["1,2", "3"]) == [1, 2, 3]
    assert _parse_ids(None) is None
    assert _parse_ids([]) is None


def test_ids_skip_empty_pieces() -> None:
    """A trailing or doubled comma is a typo, not a request for id 0."""
    assert _parse_ids(["1,,2", " "]) == [1, 2]


def test_ids_reject_non_integers() -> None:
    with pytest.raises(ConfigError, match="expects integers"):
        _parse_ids(["1,abc"])


def test_csv_splits_and_strips() -> None:
    assert _parse_csv(" name , email ") == ["name", "email"]
    assert _parse_csv(None) is None


def test_bad_json_names_the_flag() -> None:
    with pytest.raises(ConfigError, match="--domain is not valid JSON"):
        _parse_json("[[", "domain")


def test_domain_round_trips() -> None:
    assert _parse_json('[["is_company","=",true]]', "domain") == [
        ["is_company", "=", True]]


# --- the no-op override -------------------------------------------------------

def test_override_without_write_is_refused(monkeypatch: pytest.MonkeyPatch,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    """The override lifts the gate; it never grants the write itself."""
    monkeypatch.setattr(
        odoo_rpc.sys, "argv",
        ["odoo-rpc", "search", "res.partner", "--i-know-this-is-live"],
    )
    assert main() == 1
    assert "does nothing on its own" in capsys.readouterr().err


# --- output budget ------------------------------------------------------------

def test_small_result_is_printed_whole(capsys: pytest.CaptureFixture[str]) -> None:
    _emit([{"id": 1}], opts())
    assert json.loads(capsys.readouterr().out) == [{"id": 1}]


def test_oversized_list_drops_records_and_stays_valid_json(
        capsys: pytest.CaptureFixture[str]) -> None:
    records = [{"id": n, "name": "x" * 50} for n in range(200)]
    _emit(records, opts(max_bytes=1000))
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert 0 < len(parsed) < 200
    assert "dropped" in captured.err


def test_oversized_dict_is_printed_whole_with_a_warning(
        capsys: pytest.CaptureFixture[str]) -> None:
    """Dropping keys from a dict would silently change what it means."""
    payload = {f"field_{n}": {"string": "x" * 40} for n in range(200)}
    _emit(payload, opts(max_bytes=500))
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload
    assert "--attrs" in captured.err


def test_compact_output_has_no_indentation(capsys: pytest.CaptureFixture[str]) -> None:
    _emit([{"id": 1}], opts(compact=True))
    assert capsys.readouterr().out.strip() == '[{"id": 1}]'


def test_non_positive_timeout_is_refused(monkeypatch: pytest.MonkeyPatch,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    """A negative timeout reaches socket handling and raises there, not here."""
    monkeypatch.setattr(
        odoo_rpc.sys, "argv",
        ["odoo-rpc", "count", "res.partner", "--timeout", "-1"],
    )
    assert main() == 1
    assert "--timeout must be greater than 0" in capsys.readouterr().err


def test_zero_timeout_is_refused(monkeypatch: pytest.MonkeyPatch,
                                 capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(
        odoo_rpc.sys, "argv",
        ["odoo-rpc", "count", "res.partner", "--timeout", "0"],
    )
    assert main() == 1
    assert "--timeout" in capsys.readouterr().err


def test_args_must_be_a_json_list() -> None:
    with pytest.raises(ConfigError, match="--args must be a JSON list, got int"):
        odoo_rpc._parse_typed_json("1", "args", list, "list")


def test_kwargs_must_be_a_json_object() -> None:
    with pytest.raises(ConfigError, match="--kwargs must be a JSON object, got list"):
        odoo_rpc._parse_typed_json('["x"]', "kwargs", dict, "object")


def test_a_correctly_shaped_argument_passes_through() -> None:
    assert odoo_rpc._parse_typed_json('[1, 2]', "args", list, "list") == [1, 2]
    assert odoo_rpc._parse_typed_json(None, "args", list, "list") is None
