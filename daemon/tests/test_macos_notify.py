#!/usr/bin/env python3
"""Unit tests for the macOS daemon's one-shot "run claude auth login" notification.

The banner must fire once per no-token outage (not every 60 s poll) and
re-arm after a successful poll.

Run: python -m pytest daemon/tests/test_macos_notify.py -x -q
"""
from unittest.mock import patch

import daemon.claude_usage_daemon as mod


def _fresh():
    mod.note_token_ok()  # reset the module-level latch between tests


def test_notifies_once_per_outage():
    _fresh()
    with patch.object(mod, "_notify_macos") as notify:
        assert mod.note_no_token() is True
        assert mod.note_no_token() is False
        assert mod.note_no_token() is False
    notify.assert_called_once_with(mod.LOGIN_NOTICE_TITLE, mod.login_notice_text())
    assert mod.LOGIN_FIX in mod.login_notice_text()


def test_rearms_after_a_successful_poll():
    _fresh()
    with patch.object(mod, "_notify_macos") as notify:
        assert mod.note_no_token() is True
        mod.note_token_ok()
        assert mod.note_no_token() is True
    assert notify.call_count == 2


def test_token_ok_without_prior_outage_is_harmless():
    _fresh()
    with patch.object(mod, "_notify_macos") as notify:
        mod.note_token_ok()
        mod.note_token_ok()
    notify.assert_not_called()


def test_notify_is_a_noop_off_macos():
    with patch.object(mod.sys, "platform", "linux"), \
         patch.object(mod.subprocess, "run") as run:
        mod._notify_macos("t", "m")
    run.assert_not_called()


def test_notify_message_is_an_applescript_string_literal():
    with patch.object(mod.sys, "platform", "darwin"), \
         patch.object(mod.subprocess, "run") as run:
        mod._notify_macos("Clawdmeter", 'say "hi" \\ bye')
    argv = run.call_args.args[0]
    assert argv[:2] == ["osascript", "-e"]
    assert argv[2] == 'display notification "say \\"hi\\" \\\\ bye" with title "Clawdmeter"'
