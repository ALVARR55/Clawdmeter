#!/usr/bin/env python3
"""Unit tests for the macOS menu-bar pure helpers (no pystray / Pillow needed).

Run: python -m pytest daemon/tests/test_macos_menubar.py -x -q
"""
import time

import daemon.claude_usage_daemon as mod
from daemon.menubar_macos import (
    STALE_AFTER_S, TrayState, effective_state, find_logo, header_text,
)

T0 = 1_790_000_000.0


def test_initial_state_is_waiting():
    ts = TrayState()
    assert effective_state(ts, T0) == "waiting"
    assert header_text(ts, T0) == "Waiting for device…"


def test_connected_fresh_and_then_stale():
    ts = TrayState()
    ts.set_connected(T0)
    assert effective_state(ts, T0 + 30) == "connected"
    assert header_text(ts, T0 + 30).startswith("Connected · last update ")
    late = T0 + STALE_AFTER_S + 1
    assert effective_state(ts, late) == "stale"
    assert header_text(ts, late).startswith("Connected · no update since ")


def test_error_wins_and_reason_is_shown():
    ts = TrayState()
    ts.set_connected(T0)
    ts.set_error("Claude Code not logged in — run `claude auth login`")
    assert effective_state(ts, T0) == "error"
    assert header_text(ts, T0) == "Error: Claude Code not logged in — run `claude auth login`"


def test_waiting_after_connected_keeps_last_sync_but_not_state():
    ts = TrayState()
    ts.set_connected(T0)
    ts.set_waiting()
    assert effective_state(ts, T0) == "waiting"
    assert ts.last_sync == T0          # still shown once reconnected
    ts.set_connected(T0 + 60)
    assert effective_state(ts, T0 + 61) == "connected"


def test_header_uses_local_time_of_last_sync():
    ts = TrayState()
    ts.set_connected(T0)
    assert time.strftime("%H:%M", time.localtime(T0)) in header_text(ts, T0)


def test_find_logo_resolves_repo_asset():
    p = find_logo()
    assert p is not None and p.name == "logo_80.png" and p.is_file()


def test_read_menubar_setting_defaults_on(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CONFIG_FILE", tmp_path / "config")   # absent
    assert mod.read_menubar_setting() == "on"
    (tmp_path / "config").write_text("clock = 12\n")
    assert mod.read_menubar_setting() == "on"


def test_read_menubar_setting_off_variants(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    monkeypatch.setattr(mod, "CONFIG_FILE", cfg)
    for v in ("off", "OFF", "0", "false", "no"):
        cfg.write_text(f"menubar = {v}  # comment\n")
        assert mod.read_menubar_setting() == "off", v
    cfg.write_text("menubar = on\n")
    assert mod.read_menubar_setting() == "on"


# --- Disconnect / Reconnect (paused) -----------------------------------------

def test_paused_wins_over_every_other_state():
    ts = TrayState()
    ts.set_connected(T0)
    ts.paused = True
    assert effective_state(ts, T0) == "paused"
    assert header_text(ts, T0).startswith("Disconnected (paused)")
    ts.set_error("Claude Code not logged in", "run `claude auth login`")
    assert effective_state(ts, T0) == "paused"          # still paused
    ts.paused = False
    assert effective_state(ts, T0) == "error"           # underlying state resurfaces


def test_error_carries_a_fix_line():
    ts = TrayState()
    ts.set_error(mod.LOGIN_MISSING, mod.LOGIN_FIX, action="login")
    assert ts.fix == mod.LOGIN_FIX
    assert ts.action == "login"
    assert header_text(ts, T0) == f"Error: {mod.LOGIN_MISSING}"
    ts.set_connected(T0)
    assert ts.fix == "" and ts.action == ""             # cleared on recovery
    ts.set_error(mod.BT_DENIED, mod.BT_DENIED_FIX, action="bluetooth")
    ts.set_waiting()
    assert ts.fix == "" and ts.action == ""


# --- proactive login detection ------------------------------------------------

def test_login_problem_when_no_dir_has_a_token(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "read_config_dirs", lambda: [tmp_path / "a", tmp_path / "b"])
    monkeypatch.setattr(mod, "read_token_for", lambda d: None)
    assert mod.login_problem() == mod.LOGIN_MISSING


def test_login_problem_none_when_any_dir_has_a_token(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "read_config_dirs", lambda: [tmp_path / "a", tmp_path / "b"])
    monkeypatch.setattr(mod, "read_token_for", lambda d: "TOK" if d.name == "b" else None)
    assert mod.login_problem() is None


# --- Bluetooth permission / radio state --------------------------------------

class _Reason:
    def __init__(self, name):
        self.name = name


class _BtErr(Exception):
    """Stand-in for bleak's BleakBluetoothNotAvailableError (msg + .reason)."""
    def __init__(self, msg, reason=None):
        super().__init__(msg)
        self.reason = reason


def test_classify_bt_error_by_bleak_reason():
    assert mod.classify_bt_error(_BtErr("x", _Reason("DENIED_BY_USER"))) == (mod.BT_DENIED, mod.BT_DENIED_FIX)
    assert mod.classify_bt_error(_BtErr("x", _Reason("DENIED_BY_UNKNOWN"))) == (mod.BT_DENIED, mod.BT_DENIED_FIX)
    assert mod.classify_bt_error(_BtErr("x", _Reason("DENIED_BY_SYSTEM"))) == (mod.BT_RESTRICTED, mod.BT_RESTRICTED_FIX)
    assert mod.classify_bt_error(_BtErr("x", _Reason("POWERED_OFF"))) == (mod.BT_OFF, mod.BT_OFF_FIX)


def test_classify_bt_error_by_message_and_transients():
    denied = "Bluetooth access is denied by the user for the current application. Check macOS privacy settings."
    assert mod.classify_bt_error(RuntimeError(denied)) == (mod.BT_DENIED, mod.BT_DENIED_FIX)
    assert mod.classify_bt_error(RuntimeError("Bluetooth device is turned off")) == (mod.BT_OFF, mod.BT_OFF_FIX)
    # Resetting / unknown states stay amber (None), not red.
    assert mod.classify_bt_error(_BtErr("Connection to the Bluetooth system service was lost", _Reason("UNKNOWN"))) is None
    assert mod.classify_bt_error(RuntimeError("boom")) is None


def test_bt_fix_names_python_and_the_settings_pane():
    # macOS lists the grant under the interpreter's name, so the fix must say "Python".
    assert "Python" in mod.BT_DENIED_FIX and "Privacy & Security > Bluetooth" in mod.BT_DENIED_FIX
    from daemon.menubar_macos import BT_SETTINGS_URL
    assert BT_SETTINGS_URL.startswith("x-apple.systempreferences:") and "Privacy_Bluetooth" in BT_SETTINGS_URL


def test_login_shell_cmd_waits_for_enter_then_logs_in():
    from daemon.menubar_macos import LOGIN_SHELL_CMD
    assert LOGIN_SHELL_CMD.endswith("claude auth login")
    assert "read -r" in LOGIN_SHELL_CMD            # pre-typed, runs on Enter
    assert '"' not in LOGIN_SHELL_CMD              # embedded verbatim in an AppleScript string


# --- network failures while connected ---------------------------------------

def test_network_problem_needs_two_consecutive_failures():
    import httpx
    mod._note_net_ok()
    mod._note_net_failure(httpx.ConnectError("[Errno 8] nodename nor servname provided"))
    assert mod.network_problem() is None                # one blip stays quiet
    mod._note_net_failure(httpx.ConnectError("[Errno 8] nodename nor servname provided"))
    assert mod.network_problem() == (mod.NET_OFFLINE, mod.NET_OFFLINE_FIX, "offline")
    mod._note_net_ok()
    assert mod.network_problem() is None                # cleared by a good poll


def test_classify_net_error_spots_tls_inspection():
    import httpx
    e = httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate")
    assert mod.classify_net_error(e) == (mod.NET_TLS, mod.NET_TLS_FIX, "tls")
    assert mod.classify_net_error(httpx.ReadTimeout("timed out"))[2] == "offline"


def test_faq_anchors_exist_in_the_page():
    page = (mod.Path(__file__).resolve().parents[2] / "docs" / "troubleshooting.html").read_text()
    for anchor in ("tls", "offline", "spend", "pairing", "nodata", "bluetooth", "login"):
        assert f'id="{anchor}"' in page, anchor


def test_spend_problem_after_five_consecutive_failures():
    mod._spend_failures = 0
    for _ in range(mod.SPEND_FAILS_BEFORE_ERROR - 1):
        mod._spend_failures += 1
        assert mod.spend_problem() is None
    mod._spend_failures += 1
    assert mod.spend_problem() == (mod.SPEND_STALE, mod.SPEND_STALE_FIX, "spend")
    mod._spend_failures = 0
    assert mod.spend_problem() is None


def test_device_name_matching_accepts_owner_suffixes_only():
    assert mod.is_our_device_name("Clawdmeter")
    assert mod.is_our_device_name("Clawdmeter-Ricardo")
    assert mod.is_our_device_name("Clawdmeter-r2_d2")
    assert not mod.is_our_device_name("Clawdmeter2")
    assert not mod.is_our_device_name("clawdmeter")
    assert not mod.is_our_device_name("")
    assert not mod.is_our_device_name(None)


# --- Claude Code events (hooks -> file -> board) -----------------------------

def test_event_inbox_ignores_history_and_reports_fresh_words(tmp_path):
    f = tmp_path / "event"
    f.write_text("done")
    inbox = mod.EventInbox(f)                   # pre-existing content is history
    assert inbox.check() is None and not inbox.pending.is_set()
    import os, time as _t
    _t.sleep(0.01); f.write_text("needs"); os.utime(f, None)
    assert inbox.check() == "needs" and inbox.pending.is_set()
    assert inbox.take() == "needs" and not inbox.pending.is_set()
    assert inbox.take() is None


def test_event_inbox_rejects_unknown_words_and_stale_files(tmp_path):
    import os
    f = tmp_path / "event"
    inbox = mod.EventInbox(f)                   # file absent -> seen = 0
    f.write_text("bogus")
    assert inbox.check() is None
    f.write_text("done")
    old = 1_000_000_000
    os.utime(f, (old, old))                     # written ages ago
    assert inbox.check(now=old + mod.EVENT_MAX_AGE_S + 1) is None


def test_install_hooks_is_idempotent_and_keeps_other_hooks(tmp_path):
    import json
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "opus", "hooks": {
        "Stop": [{"hooks": [{"type": "command", "command": "echo someone-else"}]}]}}))
    mod.install_claude_hooks(p)
    mod.install_claude_hooks(p)                 # second run: no duplicates
    d = json.loads(p.read_text())
    assert d["model"] == "opus"
    stops = d["hooks"]["Stop"]
    assert len(stops) == 2 and stops[0]["hooks"][0]["command"] == "echo someone-else"
    assert mod.HOOK_TAG in stops[1]["hooks"][0]["command"] and "printf done" in stops[1]["hooks"][0]["command"]
    assert d["hooks"]["Notification"][0]["matcher"] == "permission_prompt|idle_prompt|agent_needs_input"
    assert "printf clear" in d["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    mod.install_claude_hooks(p, remove=True)
    d = json.loads(p.read_text())
    assert d["hooks"] == {"Stop": [{"hooks": [{"type": "command", "command": "echo someone-else"}]}]}


def test_install_hooks_refuses_invalid_json(tmp_path):
    p = tmp_path / "settings.json"; p.write_text("{not json")
    assert "not touching" in mod.install_claude_hooks(p)
    assert p.read_text() == "{not json"


def test_hook_command_is_a_safe_one_liner():
    cmd = mod._hook_cmd("done")
    assert cmd.startswith("mkdir -p") and cmd.endswith('/event"') and "$HOME" in cmd
    assert mod.EVENT_FILE.name == "event"
