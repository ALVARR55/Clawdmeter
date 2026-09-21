#!/usr/bin/env python3
"""Unit tests for the macOS daemon's spend parsing (Enterprise "Period" box).

`spend.used` from /api/oauth/usage arrives as integer minor units + exponent.

Run: python -m pytest daemon/tests/test_macos_spend.py -x -q
"""
from daemon.claude_usage_daemon import _parse_spend_usd


def test_parse_spend_minor_units_and_exponent():
    body = {"spend": {"used": {"amount_minor": 69986, "currency": "USD", "exponent": 2}}}
    assert _parse_spend_usd(body) == 699.86


def test_parse_spend_defaults_exponent_to_cents():
    assert _parse_spend_usd({"spend": {"used": {"amount_minor": 1234}}}) == 12.34


def test_parse_spend_zero_is_a_real_value():
    assert _parse_spend_usd({"spend": {"used": {"amount_minor": 0, "exponent": 2}}}) == 0.0


def test_parse_spend_missing_or_malformed_is_none():
    assert _parse_spend_usd({}) is None
    assert _parse_spend_usd({"spend": None}) is None
    assert _parse_spend_usd({"spend": {"used": None}}) is None
    assert _parse_spend_usd({"spend": {"used": {"amount_minor": "69986"}}}) is None
    assert _parse_spend_usd({"spend": {"used": {"amount_minor": True}}}) is None
