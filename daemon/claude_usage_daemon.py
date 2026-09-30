#!/usr/bin/env python3
"""Claude Usage Tracker Daemon (BLE) — macOS port of claude-usage-daemon.sh.

Polls Claude API rate-limit headers and writes a JSON payload to the
ESP32 "Clawdmeter" peripheral over a custom GATT service. Uses
bleak (CoreBluetooth backend on macOS).
"""

import asyncio
import calendar
import datetime
import getpass
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
from bleak import BleakClient
from bleak.exc import BleakError

# Corporate networks often TLS-inspect traffic with a company root CA that is
# trusted by the OS (macOS Keychain) but absent from Python's bundled certifi
# list, so every request dies with CERTIFICATE_VERIFY_FAILED. truststore makes
# Python's ssl module verify against the OS trust store instead. Optional: if
# it isn't installed, behavior is unchanged (public CAs still verify).
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

DEVICE_NAME = "Clawdmeter"


def is_our_device_name(name) -> bool:
    """True for the stock name and for owner-named boards ("Clawdmeter-<suffix>",
    set with `clawdmeter-flash --name`). Plain prefix matching would also accept
    unrelated "Clawdmeter2"-style names, so the dash is required."""
    return bool(name) and (name == DEVICE_NAME or name.startswith(DEVICE_NAME + "-"))
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

POLL_INTERVAL = 60
POLL_RETRY_AFTER_FAIL = 30   # a failed poll retries after this, not every TICK
TICK = 5
CONNECT_TIMEOUT = 20.0

# macOS: token lives in Keychain (service "Claude Code-credentials").
# Linux: token lives in ~/.claude/.credentials.json.
KEYCHAIN_SERVICE = "Claude Code-credentials"
DEFAULT_CONFIG_DIR = Path.home() / ".claude"
SAVED_ADDR_FILE = Path.home() / ".config" / "claude-usage-monitor" / "ble-address"
CONFIG_FILE = Path.home() / ".config" / "claude-usage-monitor" / "config"

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}

# --- Real $ spend (Enterprise "Period" box) ---------------------------------
#
# Claude Code's own `/usage` command reads spend from this OAuth endpoint, using
# the same access token the daemon already has. `spend.used` is the figure the
# claude.ai / Claude Desktop "Usage" tab shows for the current billing cycle, so
# we relay it instead of estimating from local transcripts x published prices —
# that estimate could only see Claude Code on this machine and ran ~40% low
# against real usage (phone/desktop chat never touches local transcripts).
OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

_last_spend_usd: float | None = None   # last good value, replayed over a transient fetch failure


def _parse_spend_usd(body: dict) -> float | None:
    """`spend.used` -> dollars, or None when the response has no usable spend.
    Amounts arrive as integer minor units plus an exponent ({"amount_minor":
    69986, "exponent": 2} == $699.86), so nothing is lost to float on the wire."""
    used = (body.get("spend") or {}).get("used") or {}
    amount = used.get("amount_minor")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    exponent = used.get("exponent", 2)
    if isinstance(exponent, bool) or not isinstance(exponent, int):
        exponent = 2
    return amount / (10 ** exponent)


async def fetch_spend_usd(token: str) -> float | None:
    """Current-cycle spend in USD from OAUTH_USAGE_URL. Never raises: on any
    failure it returns the last good value (None if there has never been one)
    so the device doesn't flash "---" over a network blip. Auth failures are
    left to poll_api's /v1/messages probe, which already raises TokenExpired."""
    global _last_spend_usd
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": API_HEADERS_TEMPLATE["anthropic-beta"],
        "User-Agent": API_HEADERS_TEMPLATE["User-Agent"],
        "Accept": "application/json",
    }
    global _spend_failures
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.get(OAUTH_USAGE_URL, headers=headers)
        resp.raise_for_status()
        spend = _parse_spend_usd(resp.json())
    except (httpx.HTTPError, ValueError) as e:
        _spend_failures += 1
        log(f"Spend fetch failed: {e}; reusing last value")
        return _last_spend_usd
    if spend is None:
        _spend_failures += 1
        log("Spend fetch: no spend.used in response; reusing last value")
        return _last_spend_usd
    _spend_failures = 0
    _last_spend_usd = spend
    return spend


# The spend lookup is soft: one failure replays the last $ figure and the
# board keeps updating, so a blip stays invisible. After this many failures
# in a row the "$" on the board is stale enough to say so on the icon.
SPEND_FAILS_BEFORE_ERROR = 5
SPEND_STALE = "Spend figure not updating"
SPEND_STALE_FIX = "the $ shown is the last known value; check your network (click for the FAQ)"
SPEND_STALE_FAQ = "spend"
_spend_failures = 0


def spend_problem() -> tuple[str, str, str] | None:
    """(reason, fix, faq_anchor) once SPEND_FAILS_BEFORE_ERROR consecutive spend
    lookups have failed while usage polls kept working; None otherwise."""
    if _spend_failures >= SPEND_FAILS_BEFORE_ERROR:
        return SPEND_STALE, SPEND_STALE_FIX, SPEND_STALE_FAQ
    return None


class TokenExpired(Exception):
    """Raised by poll_api on a 401/403 — the access token is dead. The daemon never
    refreshes (pure free-ride: Claude Code owns refreshing), so the caller just
    signals "No data" to the device until the CLI re-seeds the token."""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- "run claude auth login" desktop notification --------------------------------
#
# The daemon never refreshes the OAuth token (Claude Code owns that), so when
# it expires the device just idles on "No data" — which reads like a BLE
# problem unless something tells the user the fix is `claude auth login`. Mirrors
# the Windows tray daemon: one native notification on the transition into the
# no-token state, re-armed once a poll succeeds, so a long outage doesn't nag
# every 60 s.
_login_notice_shown = False

# Why the last poll cycle had no usable login — drives the tray's error text.
LOGIN_MISSING = "Claude Code not logged in"
LOGIN_EXPIRED = "Claude Code login expired"
LOGIN_FIX = "run `claude auth login` in Terminal, then wait a minute"
_last_dead_reason = LOGIN_MISSING

# Why CoreBluetooth refused to come up on the last device lookup (macOS). The
# Bluetooth permission prompt is shown once per app; a Deny is remembered and
# the daemon would otherwise sit amber forever with the reason only in the log.
# The grant is keyed to Homebrew's Python (bundle org.python.python), which is
# why the Privacy & Security list shows "Python", not "Clawdmeter".
BT_DENIED = "Bluetooth access denied for Python"
BT_RESTRICTED = "Bluetooth access blocked by a system policy"
BT_OFF = "Bluetooth is turned off"
BT_DENIED_FIX = "allow Python under Privacy & Security > Bluetooth (retries automatically)"
BT_RESTRICTED_FIX = "ask your Mac's admin to lift the Bluetooth restriction for Python"
BT_OFF_FIX = "turn Bluetooth on in Control Center (retries automatically)"
_bt_problem: tuple[str, str] | None = None   # (reason, fix) or None when CoreBluetooth is fine


def classify_bt_error(e: BaseException) -> tuple[str, str] | None:
    """Map a CoreBluetooth start-up failure to a (reason, fix) pair for the menu
    bar, or None for transient states (radio resetting, unknown) that should
    stay amber. Uses bleak's BleakBluetoothNotAvailableReason when present and
    falls back to the message text."""
    name = getattr(getattr(e, "reason", None), "name", "") or ""
    msg = str(e).lower()
    if name == "DENIED_BY_USER" or "denied by the user" in msg:
        return BT_DENIED, BT_DENIED_FIX
    if name == "DENIED_BY_SYSTEM" or "restricted" in msg:
        return BT_RESTRICTED, BT_RESTRICTED_FIX
    if name == "DENIED_BY_UNKNOWN" or "not authorized" in msg:
        return BT_DENIED, BT_DENIED_FIX
    if name == "POWERED_OFF" or "turned off" in msg:
        return BT_OFF, BT_OFF_FIX
    return None


def bluetooth_problem() -> tuple[str, str] | None:
    """(reason, fix) from the last CoreBluetooth failure, or None."""
    return _bt_problem


# Network failures reaching Anthropic while a board is connected. The device
# just keeps its last numbers (90 s freshness) and the icon would sit green
# then amber "stale" with the cause only in the log. Two consecutive failed
# polls flip the icon red with a fix line that opens the matching FAQ entry.
NET_TLS = "HTTPS certificate not trusted (corporate proxy?)"
NET_TLS_FIX = "run `brew upgrade clawdmeter`; still failing? click for the FAQ"
NET_TLS_FAQ = "tls"
NET_OFFLINE = "Can't reach Anthropic (offline / VPN?)"
NET_OFFLINE_FIX = "check your network or VPN; retrying every 30 s (click for the FAQ)"
NET_OFFLINE_FAQ = "offline"
NET_FAILS_BEFORE_ERROR = 2
_net_failures = 0
_net_problem: tuple[str, str, str] | None = None   # (reason, fix, faq anchor)


def classify_net_error(e: BaseException) -> tuple[str, str, str]:
    """(reason, fix, faq_anchor) for a failed /v1/messages probe."""
    msg = str(e)
    if "CERTIFICATE_VERIFY_FAILED" in msg or "certificate verify failed" in msg.lower():
        return NET_TLS, NET_TLS_FIX, NET_TLS_FAQ
    return NET_OFFLINE, NET_OFFLINE_FIX, NET_OFFLINE_FAQ


def _note_net_failure(e: BaseException) -> None:
    global _net_failures, _net_problem
    _net_failures += 1
    _net_problem = classify_net_error(e)


def _note_net_ok() -> None:
    global _net_failures, _net_problem
    _net_failures = 0
    _net_problem = None


def network_problem() -> tuple[str, str, str] | None:
    """(reason, fix, faq_anchor) once NET_FAILS_BEFORE_ERROR consecutive polls
    have failed to reach Anthropic; None while healthy or after a single blip."""
    return _net_problem if _net_failures >= NET_FAILS_BEFORE_ERROR else None


def login_problem() -> str | None:
    """Cheap, network-free check used while no board is connected: LOGIN_MISSING
    if no configured Claude dir holds an access token, else None. (Expiry can
    only be detected by an API call, which happens once a board is connected.)"""
    for d in read_config_dirs():
        if read_token_for(d):
            return None
    return LOGIN_MISSING


LOGIN_NOTICE_TITLE = "Clawdmeter"
LOGIN_NOTICE_TEXT = ("Claude Code login expired — run `claude auth login` in a "
                     "terminal to resume usage updates.")


def login_notice_text() -> str:
    """Banner wording, same specific reason as the menu bar shows."""
    return f"{_last_dead_reason} — {LOGIN_FIX}."


def _notify_macos(title: str, message: str) -> None:
    """Best-effort Notification Center banner via osascript; never raises.
    json.dumps gives AppleScript-compatible double-quoted string literals."""
    if sys.platform != "darwin":
        return
    script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
    try:
        subprocess.run(["osascript", "-e", script], check=False, timeout=10,
                       capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass


def note_no_token() -> bool:
    """Called each poll that finds no usable token. Notifies once per outage;
    returns True only on the poll that actually fired the notification."""
    global _login_notice_shown
    if _login_notice_shown:
        return False
    _login_notice_shown = True
    _notify_macos(LOGIN_NOTICE_TITLE, login_notice_text())
    return True


def note_token_ok() -> None:
    """Called on a successful poll: re-arm the notification for the next outage."""
    global _login_notice_shown
    _login_notice_shown = False


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        tok = data.get("accessToken")
        if isinstance(tok, str) and tok.strip():
            return tok
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict):
                tok = v.get("accessToken")
                if isinstance(tok, str) and tok.strip():
                    return tok
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _decode_keychain_blob(raw: str) -> str:
    """Transparently decode a hex-dumped Keychain secret back to text.

    ``security … -w`` prints the password as a continuous hex string whenever
    the stored bytes aren't cleanly printable (e.g. an embedded newline). A
    normal credentials blob is JSON, which is never valid hex (it contains
    '{', '"', …), so all-hex detection is unambiguous and safe.
    """
    s = raw.strip()
    if s and len(s) % 2 == 0 and re.fullmatch(r"[0-9a-fA-F]+", s):
        try:
            return bytes.fromhex(s).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return raw
    return raw


def _read_token_keychain() -> str | None:
    """Read the OAuth access token from the macOS Keychain, or None.

    ``security … -w`` may hex-dump the stored secret (see _decode_keychain_blob),
    so decode before extracting the access token.
    """
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        log(f"Keychain read failed (rc={e.returncode}): {e.stderr.strip()}")
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"Keychain access error: {e}")
        return None
    return _extract_access_token(_decode_keychain_blob(out.stdout))


def read_config_dirs() -> list[Path]:
    """Claude config dirs to poll, from the `config_dirs` option (comma list).

    Defaults to [~/.claude] so existing single-plan setups are unchanged. ~ is
    expanded. Mirrors the Linux bash daemon's read_config_dirs.
    """
    raw = ""
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "config_dirs":
                    raw = val.strip()
    except OSError:
        pass
    if not raw:
        return [DEFAULT_CONFIG_DIR]
    dirs = [Path(p.strip()).expanduser() for p in raw.split(",") if p.strip()]
    return dirs or [DEFAULT_CONFIG_DIR]


def read_token_for(config_dir: Path) -> str | None:
    """Read the OAuth token for one config dir.

    Linux: each dir keeps its own ``<dir>/.credentials.json``. macOS: the default
    install stores the token in Keychain with no file, so for the default dir we
    fall back to Keychain when no file is present — preserving existing
    single-plan macOS behavior. Additional macOS dirs are read from their files;
    a work plan whose token lives only in the single Keychain entry can't be told
    apart there (documented follow-up).
    """
    cred = config_dir / ".credentials.json"
    try:
        if cred.exists():
            return _extract_access_token(cred.read_text())
    except OSError as e:
        log(f"Error reading credentials in {config_dir}: {e}")
    if sys.platform == "darwin" and config_dir == DEFAULT_CONFIG_DIR:
        return _read_token_keychain()
    return None


def load_cached_address() -> str | None:
    if not SAVED_ADDR_FILE.exists():
        return None
    addr = SAVED_ADDR_FILE.read_text().strip()
    # Accept both Linux MAC (AA:BB:CC:DD:EE:FF) and macOS CoreBluetooth UUID
    # (E621E1F8-C36C-495A-93FC-0C247A3E6E5F).
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", addr) or re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", addr
    ):
        return addr
    log("Cached address malformed, discarding")
    SAVED_ADDR_FILE.unlink(missing_ok=True)
    return None


# --- macOS: recover a device the OS already holds as an HID keyboard --------
#
# The firmware advertises as a BLE HID keyboard so its buttons type into the
# Mac. macOS auto-connects to that HID, and CoreBluetooth then EXCLUDES the
# peripheral from BleakScanner.discover() results (already-connected devices
# never appear in scans). bleak's connect-by-address path also scans
# internally, so a cached address can't help either. The documented escape
# hatch is retrieveConnectedPeripheralsWithServices_, which returns
# peripherals the system is already connected to. We wrap the result in a
# BLEDevice carrying the live (peripheral, manager) details so BleakClient
# connects to it directly without scanning. CoreBluetooth shares the single
# physical link, so this rides the existing HID connection — the keyboard
# keeps working.
_cb_manager = None  # reused CentralManagerDelegate (CoreBluetooth)


async def _get_cb_manager():
    """Lazily create and ready a shared CoreBluetooth central manager."""
    global _cb_manager
    if _cb_manager is None:
        from bleak.backends.corebluetooth.CentralManagerDelegate import (
            CentralManagerDelegate,
        )

        mgr = CentralManagerDelegate()
        await mgr.wait_until_ready()  # raises if Bluetooth is unauthorized/off
        _cb_manager = mgr
    return _cb_manager


async def retrieve_connected_macos(skip_addr: str | None = None):
    """Return a BLEDevice for a system-connected 'Clawdmeter', or None.

    Two-step lookup, strongest signal first:

    1. Peripherals connected under our CUSTOM service UUID. Membership in
       that service is unambiguous (no other device exposes it), so we accept
       by service alone — the peripheral's name can be None on macOS.
    2. Fall back to the generic HID service 0x1812, but ONLY trust a
       peripheral whose name matches DEVICE_NAME. 0x1812 also matches
       unrelated keyboards/mice, so picking blindly here could grab the
       wrong device.

    ``skip_addr`` skips a peripheral whose UUID just failed to connect, so a
    stale CoreBluetooth handle can't trap us into never trying a fresh scan.
    """
    from CoreBluetooth import CBUUID
    from bleak.backends.device import BLEDevice

    global _bt_problem
    try:
        manager = await _get_cb_manager()
    except Exception as e:  # BleakBluetoothNotAvailableError etc.
        log(f"CoreBluetooth unavailable: {e}")
        _bt_problem = classify_bt_error(e)   # surfaces on the menu-bar icon
        return None
    _bt_problem = None

    cm = manager.central_manager

    def _wrap(p):
        addr = p.identifier().UUIDString()
        log(f"Found system-connected peripheral: {p.name()!r} [{addr}]")
        return BLEDevice(addr, p.name(), (p, manager))

    def _ok(p) -> bool:
        return not (skip_addr and p.identifier().UUIDString() == skip_addr)

    # 1. Custom service — accept by service membership alone.
    custom = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_(SERVICE_UUID)]
    )
    for p in custom or []:
        if _ok(p):
            return _wrap(p)

    # 2. Generic HID service — require an exact name match.
    hid = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_("1812")]
    )
    for p in hid or []:
        if _ok(p) and is_our_device_name(p.name()):
            return _wrap(p)

    return None


async def discover_target(skip_addr: str | None = None):
    """Return a connectable target, or None.

    The daemon only ever targets the device this system already holds — it
    never scans for a nearby device by name, so it can't grab a stranger's or
    the wrong nearby unit. On macOS that's the system-connected peripheral (the
    firmware advertises as an HID keyboard, so once paired the OS auto-connects
    and holds it — HID-grabbed devices are invisible to scans anyway). On other
    platforms it's a previously-pinned address in the cache file. If the device
    isn't held/pinned, we log and wait rather than scanning. ``skip_addr`` skips
    a peripheral whose handle just failed to connect.
    """
    if sys.platform == "darwin":
        dev = await retrieve_connected_macos(skip_addr=skip_addr)
        if dev is None:
            log("Device not held by OS; waiting (not scanning by name)")
        return dev

    address = load_cached_address()
    if not address:
        log("No pinned address cached; waiting (not scanning by name)")
    return address


def read_chime_setting() -> str:
    """Read the `chime` option from the config file. One of: off|on.

    Defaults to "off" (the device stays silent) so existing setups are
    unaffected until the user opts in.
    """
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "chime":
                    val = val.strip().lower()
                    if val in ("off", "on"):
                        return val
    except OSError:
        pass
    return "off"


def read_events_setting() -> str:
    """`events = on|off` in the config: forward Claude Code hook events
    ("done" / "needs" / "clear") to the board. Default on."""
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "events":
                    val = val.strip().lower()
                    if val in ("off", "0", "false", "no"):
                        return "off"
                    return "on"
    except OSError:
        pass
    return "on"


# --- Claude Code events -------------------------------------------------------
# Claude Code hooks (Stop / Notification / UserPromptSubmit) write one word into
# EVENT_FILE; the daemon watches the file's mtime and forwards the word to the
# board as "ev" on a payload of its own, so the board can show "Done" or
# "Claude needs you" until the user taps it. No sockets, no listeners: a file
# in the user's own config dir is the whole transport.
EVENT_FILE = CONFIG_FILE.parent / "event"
EVENT_WORDS = ("done", "needs", "clear")
EVENT_POLL_S = 0.5
EVENT_MAX_AGE_S = 120        # ignore an event written long before we looked (daemon was down)


class EventInbox:
    """Watches EVENT_FILE and raises `pending` when a fresh event word lands."""

    def __init__(self, path: Path = EVENT_FILE) -> None:
        self.path = path
        self.pending = asyncio.Event()
        self.event: str | None = None
        self._seen_ns = self._mtime_ns()   # whatever is there now is history

    def _mtime_ns(self) -> int:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return 0

    def check(self, now: float | None = None) -> str | None:
        """Poll once: returns the new event word (and sets `pending`) or None."""
        ns = self._mtime_ns()
        if ns == self._seen_ns:
            return None
        self._seen_ns = ns
        now = time.time() if now is None else now
        if ns and now - ns / 1e9 > EVENT_MAX_AGE_S:
            return None
        try:
            word = self.path.read_text().strip().lower()
        except OSError:
            return None
        if word not in EVENT_WORDS:
            log(f"Ignoring unknown event '{word[:20]}' in {self.path}")
            return None
        self.event = word
        self.pending.set()
        return word

    def take(self) -> str | None:
        """Consume the pending event (or None)."""
        self.pending.clear()
        ev, self.event = self.event, None
        return ev

    async def watch(self) -> None:
        while True:
            self.check()
            await asyncio.sleep(EVENT_POLL_S)


# The hooks themselves. Each is a one-liner that drops a word into EVENT_FILE;
# identified for install/remove by HOOK_TAG in the command, never by position.
HOOK_TAG = "claude-usage-monitor/event"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


def _hook_cmd(word: str) -> str:
    return (f'mkdir -p "$HOME/.config/claude-usage-monitor" && '
            f'printf {word} > "$HOME/.config/{HOOK_TAG}"')


CLAWDMETER_HOOKS = {
    # Claude finished its turn -> "Done"
    "Stop": {"hooks": [{"type": "command", "command": _hook_cmd("done")}]},
    # Claude is blocked on a permission prompt (or an agent waits on input) ->
    # "Claude needs you". Deliberately NOT idle_prompt: Claude Code sends that
    # 60 s after every finished turn, which would turn every "Done" into a
    # false "needs you" a minute later.
    "Notification": {"matcher": "permission_prompt|agent_needs_input",
                     "hooks": [{"type": "command", "command": _hook_cmd("needs")}]},
    # The user typed the next prompt -> the board's banner is stale, clear it
    "UserPromptSubmit": {"hooks": [{"type": "command", "command": _hook_cmd("clear")}]},
}


def _is_ours(entry: dict) -> bool:
    return any(HOOK_TAG in (h.get("command") or "") for h in entry.get("hooks", []) if isinstance(h, dict))


def install_claude_hooks(settings_path: Path = CLAUDE_SETTINGS, remove: bool = False) -> str:
    """Merge (or remove) the Clawdmeter hooks in Claude Code's user settings.
    Idempotent; other hooks are left untouched. Returns a one-line summary."""
    data: dict = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text() or "{}")
        except json.JSONDecodeError as e:
            return f"not touching {settings_path}: it is not valid JSON ({e})"
    hooks = data.setdefault("hooks", {})
    changed = 0
    for event, entry in CLAWDMETER_HOOKS.items():
        entries = [e for e in hooks.get(event, []) if not _is_ours(e)]
        if not remove:
            entries.append(entry)
        if entries != hooks.get(event, []):
            changed += 1
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    if changed:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(settings_path)
    what = "removed" if remove else "installed"
    return f"Clawdmeter hooks {what} in {settings_path} ({changed} event(s) changed)"


def read_menubar_setting() -> str:
    """`menubar = on|off` from the config (macOS only). Default on: a status
    icon in the menu bar showing connected / waiting / error and the last
    update time. `off` runs the daemon headless as before."""
    val = ""
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, v = line.split("=", 1)
                if key.strip().lower() == "menubar":
                    val = v.strip().lower()
    except OSError:
        pass
    return "off" if val in ("off", "0", "false", "no") else "on"


def read_clock_setting() -> str:
    """Read the `clock` option from the config file. One of: off|auto|12|24.

    Defaults to "off" (no clock; the device keeps showing "Usage") so existing
    setups are unaffected until the user opts in.
    """
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "clock":
                    val = val.strip().lower()
                    if val in ("off", "auto", "12", "24"):
                        return val
    except OSError:
        pass
    return "off"


def add_chime_field(payload: dict) -> None:
    """Add "c":1 to the payload when the config opts in, so the firmware may
    sound the session-reset chime. Omitted entirely when chime is off."""
    if read_chime_setting() == "on":
        payload["c"] = 1


def detect_hour_format() -> int:
    """Best-effort 12h/24h detection for the host. Returns 12 or 24 (default 24)."""
    # macOS: the explicit System Settings toggle lives in NSGlobalDomain.
    for key, result in (("AppleICUForce24HourTime", 24), ("AppleICUForce12HourTime", 12)):
        try:
            out = subprocess.run(["defaults", "read", "-g", key],
                                 capture_output=True, text=True, timeout=3)
            if out.stdout.strip() == "1":
                return result
        except (OSError, subprocess.SubprocessError):
            pass
    # Fallback to the C locale's time format (may be C/24h under launchd).
    try:
        import locale
        locale.setlocale(locale.LC_TIME, "")
        fmt = locale.nl_langinfo(locale.T_FMT)
        if "%p" in fmt or "%r" in fmt or "%I" in fmt:
            return 12
    except (ImportError, locale.Error, AttributeError):
        pass
    return 24


def add_clock_fields(payload: dict) -> None:
    """Add wall-clock fields to the payload when the config opts in.

    "t"  = local wall-clock epoch (UTC epoch shifted by the tz offset) so the
           device can show the time without an RTC.
    "tf" = 12 or 24, the hour format the device should render.
    """
    clock = read_clock_setting()
    if clock == "off":
        return
    tf = 24 if clock == "24" else 12 if clock == "12" else detect_hour_format()
    payload["t"] = int(time.time()) + time.localtime().tm_gmtoff
    payload["tf"] = tf


async def poll_api(token: str, config_dir: Path = DEFAULT_CONFIG_DIR) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(API_URL, headers=headers, json=API_BODY)
    except httpx.HTTPError as e:
        log(f"API call failed: {e}")
        _note_net_failure(e)
        return None
    _note_net_ok()
    if resp.status_code in (401, 403):
        log(f"API HTTP {resp.status_code} (token expired/invalid)")
        raise TokenExpired()
    if resp.status_code >= 400:
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    def hdr(name: str, default: str = "0") -> str:
        return resp.headers.get(name, default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except ValueError:
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        try:
            return int(round(float(util) * 100))
        except ValueError:
            return 0

    # Pro/Max accounts expose 5h/7d windows; Enterprise/overage use a single
    # spending-limit model reported via overage-utilization.
    if resp.headers.get("anthropic-ratelimit-unified-5h-utilization"):
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
            "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
            "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
            "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
            "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
            "acct": "pro",
            "ok": True,
        }
    else:
        reset_ts = hdr("anthropic-ratelimit-unified-overage-reset")
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-overage-utilization")),
            "sr": reset_minutes(reset_ts),
            "w": 0,
            "wr": 0,
            "st": hdr("anthropic-ratelimit-unified-status", "unknown"),
            "acct": "ent",
            **_billing_period_info(now, reset_ts),
            "ok": True,
        }
        bounds = _period_bounds(reset_ts)
        if bounds is not None:
            payload["tok"] = count_tokens_since(bounds[0], config_dir)
        spend = await fetch_spend_usd(token)
        if spend is not None:
            payload["cost"] = round(spend, 2)
    add_chime_field(payload)   # adds "c":1 iff the config opts in
    add_clock_fields(payload)   # adds "t" + "tf" iff the config opts in
    return payload


def _period_bounds(reset_ts: str) -> tuple[float, float] | None:
    """(period_start, period_end) epoch seconds for the Enterprise billing period,
    or None if `reset_ts` isn't usable. Shared by `_billing_period_info` and the
    local token counter below, so both agree on what "this billing period" means.

    Billing periods are assumed calendar-monthly: period_end is the reset
    timestamp, period_start is the same day/time one calendar month earlier.

    The rate-limit headers expose only the reset timestamp, not the period
    length, so the monthly window is an assumption — but a documented one:
    Enterprise spend-limit `period` "the only value today is monthly"
    (Claude Enterprise Admin API reference). The doc notes period is an open
    string that may gain other values later; revisit this if so.
    """
    try:
        period_end = float(reset_ts)
    except ValueError:
        return None
    if period_end <= 0:
        # reset_ts defaults to "0" when the overage-reset header is absent.
        # fromtimestamp(0) is 1970; stepping a month back lands in 1969, and
        # datetime.timestamp() raises OSError for pre-1970 dates on Windows.
        # Benign on macOS/Linux, but guard here too to keep the daemons parallel.
        return None
    dt_end = datetime.datetime.fromtimestamp(period_end)
    prev_month = dt_end.month - 1 or 12
    prev_year = dt_end.year if dt_end.month > 1 else dt_end.year - 1
    prev_day = min(dt_end.day, calendar.monthrange(prev_year, prev_month)[1])
    dt_start = dt_end.replace(year=prev_year, month=prev_month, day=prev_day)
    period_start = dt_start.timestamp()
    if period_end - period_start <= 0:
        return None
    return period_start, period_end


def _billing_period_info(now: float, reset_ts: str) -> dict:
    """Fraction of billing period elapsed (tp, 0-100) and period length in days (pd)."""
    bounds = _period_bounds(reset_ts)
    if bounds is None:
        return {"tp": 0, "pd": 30}
    period_start, period_end = bounds
    period_len = period_end - period_start
    pct_val = (now - period_start) / period_len * 100
    total_days = int(round(period_len / 86400))
    dt_end = datetime.datetime.fromtimestamp(period_end)
    rd = f"{dt_end.strftime('%b')} {dt_end.day}"
    return {
        "tp": max(0, min(100, int(round(pct_val)))),
        "pd": total_days,
        "rd": rd,
    }


def count_tokens_since(period_start: float, config_dir: Path) -> int:
    """Tokens (input + output + cache write + cache read) from this config dir's
    local Claude Code transcripts (`<config_dir>/projects/**/*.jsonl` —
    recursive, so subagent transcripts under `<session>/subagents/` count too)
    since `period_start`. Pure local file read — no network, no API key.

    Approximates "tokens used this billing period" from what Claude Code itself
    logged on this machine; it can't see other machines, chat usage, or
    transcripts Claude Code has already pruned. Real $ spend comes from
    fetch_spend_usd, not from here.
    """
    total_tokens = 0
    seen_message_ids: set[str] = set()
    projects_dir = config_dir / "projects"
    if not projects_dir.is_dir():
        return 0
    # rglob, not glob("*/*.jsonl"): subagent (Task tool) transcripts live one level
    # deeper, at <project>/<session>/subagents/agent-*.jsonl. A shallow glob misses
    # every subagent's real, separately-billed token usage entirely.
    for jsonl_path in projects_dir.rglob("*.jsonl"):
        try:
            if jsonl_path.stat().st_mtime < period_start:
                continue  # whole file predates the period — skip parsing it
        except OSError:
            continue
        try:
            with jsonl_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("type") != "assistant":
                        continue
                    ts = entry.get("timestamp")
                    if not ts:
                        continue
                    try:
                        entry_epoch = datetime.datetime.strptime(
                            ts[:19], "%Y-%m-%dT%H:%M:%S"
                        ).replace(tzinfo=datetime.timezone.utc).timestamp()
                    except ValueError:
                        continue
                    if entry_epoch < period_start:
                        continue
                    msg = entry.get("message") or {}
                    mid = msg.get("id")
                    if mid:
                        # Claude Code logs one JSONL line per content block of a
                        # multi-tool-call response (apiBlockIndex 0, 1, 2, ...),
                        # but every line repeats the SAME message-level `usage`
                        # (usage belongs to the whole API response, not a block).
                        # Counting every line multiplies tokens/cost by the
                        # number of tool calls in the turn — dedupe on message id.
                        if mid in seen_message_ids:
                            continue
                        seen_message_ids.add(mid)
                    usage = msg.get("usage") or {}
                    total_tokens += (
                        usage.get("input_tokens", 0)
                        + usage.get("output_tokens", 0)
                        + usage.get("cache_creation_input_tokens", 0)
                        + usage.get("cache_read_input_tokens", 0)
                    )
        except OSError:
            continue
    return total_tokens


class PlanSelector:
    """Decide which config dir's plan is "active" across polls.

    "Active" = the plan whose session % rose most recently (recent API activity).
    A rise stamps a monotonic poll counter, so the choice is sticky and a window
    reset (a drop to 0) isn't mistaken for use. Before any rise is seen (startup)
    the highest current session % wins. Mirrors the Linux bash daemon.
    """

    def __init__(self) -> None:
        self.prev_s: dict[Path, int] = {}
        self.last_active: dict[Path, int] = {}
        self.seq = 0

    def choose(self, sessions: dict[Path, int]) -> Path:
        """Update state from this cycle's {dir: session_pct} and return the active dir."""
        self.seq += 1
        for d, s in sessions.items():
            if d in self.prev_s and s > self.prev_s[d]:
                self.last_active[d] = self.seq
            self.prev_s[d] = s
        # Most recent activity wins; ties (and the startup case) break by highest %.
        return max(sessions, key=lambda d: (self.last_active.get(d, 0), sessions[d]))


# Module-level so the active-plan state survives reconnects.
_SELECTOR = PlanSelector()


async def poll_active(selector: PlanSelector = _SELECTOR) -> tuple[dict | None, bool]:
    """Poll every configured config dir; return ``(active_payload, all_dead)``.

    ``active_payload`` — the active plan's payload dict, or None when no dir
    yields a usable payload this cycle. A single configured dir (the default)
    collapses to exactly the old single-poll path.

    ``all_dead`` — True when *every* configured dir lacked a usable token this
    cycle (file/Keychain empty, or a 401/expired token), so the caller can
    signal "No data". False when at least one token authenticated — including a
    transient non-auth poll failure worth retrying silently rather than idling.

    Pure free-ride: a 401 (TokenExpired) means that dir's token has expired and
    only Claude Code (its owner) can re-seed it — we never refresh it ourselves.
    """
    global _last_dead_reason
    dirs = read_config_dirs()
    payloads: dict[Path, dict] = {}
    sessions: dict[Path, int] = {}
    any_live = False
    saw_expired = False
    for d in dirs:
        token = read_token_for(d)
        if not token:
            log(f"No token in {d}; skipping")
            continue
        try:
            payload = await poll_api(token, d)
        except TokenExpired:
            log(f"Token in {d} expired/invalid; skipping")
            saw_expired = True
            continue
        # Authenticated: a transient None here isn't an auth failure, so the
        # dir counts as live and we stay silent rather than idling the device.
        any_live = True
        if payload is not None:
            payloads[d] = payload
            sessions[d] = int(payload.get("s", 0) or 0)
    if not payloads:
        if not any_live:
            _last_dead_reason = LOGIN_EXPIRED if saw_expired else LOGIN_MISSING
        return None, not any_live
    active = selector.choose(sessions)
    if len(dirs) > 1:
        log(f"Active plan: {active} (s={sessions[active]})")
    return payloads[active], False


async def poll_active_payload(selector: PlanSelector = _SELECTOR) -> dict | None:
    """The active plan's payload, or None when no dir yields one this cycle.

    Thin wrapper over :func:`poll_active` for callers that don't need the
    all-dead flag.
    """
    payload, _dead = await poll_active(selector)
    return payload


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        # start_notify awaits CoreBluetooth's CCCD-write confirmation, which
        # never arrives if the peripheral doesn't ACK the subscribe (a
        # half-open link after the OS auto-connects the HID). Unbounded, that
        # await wedges the whole daemon between "Connected" and the first poll
        # — the device then shows nothing until a manual restart. Bound it: the
        # subscription is only an optional device-initiated refresh nudge (we
        # poll every POLL_INTERVAL regardless), so on timeout we proceed.
        try:
            await asyncio.wait_for(
                self.client.start_notify(REQ_CHAR_UUID, self._on_refresh),
                timeout=10,
            )
        except (BleakError, ValueError) as e:
            log(f"Refresh subscription unavailable: {e}")
        except asyncio.TimeoutError:
            log("Refresh subscription timed out; polling without it")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        try:
            await self.client.write_gatt_char(RX_CHAR_UUID, data, response=False)
            return True
        except BleakError as e:
            log(f"Write failed: {e}")
            return False


def _is_encryption_error(exc: BaseException) -> bool:
    """True if a connect error is a macOS bonding/encryption mismatch.

    macOS reports a stale bond as CBErrorDomain Code=15 ("Failed to encrypt
    the connection..."). Match on the message text so we don't depend on how
    bleak wraps the underlying CoreBluetooth error.
    """
    s = str(exc).lower()
    return "code=15" in s or "encrypt" in s


# blueutil talks to Bluetooth via IOBluetooth, which on recent macOS needs its
# OWN Bluetooth TCC grant (separate from the daemon's CoreBluetooth grant).
# Without it, blueutil *hangs* instead of erroring — so every call is bounded
# by a timeout and a hang is reported as a permission problem, not a crash.
BLUEUTIL_TIMEOUT = 8


def _blueutil(*args: str) -> str | None:
    """Run `blueutil <args>`, returning stdout, or None on failure/timeout.

    A timeout almost always means blueutil lacks Bluetooth permission (it
    blocks rather than failing), so we surface that cause explicitly.
    """
    try:
        return subprocess.run(
            ["blueutil", *args],
            capture_output=True, text=True,
            timeout=BLUEUTIL_TIMEOUT, check=True,
        ).stdout
    except subprocess.TimeoutExpired:
        log(f"blueutil {' '.join(args)} timed out — it likely lacks Bluetooth "
            "permission. Grant it under System Settings > Privacy & Security > "
            "Bluetooth (run `blueutil --paired` once from Terminal to prompt).")
        return None
    except (subprocess.SubprocessError, OSError) as e:
        log(f"blueutil {' '.join(args)} failed: {e}")
        return None


def unpair_macos() -> bool:
    """Forget a stale macOS bond for DEVICE_NAME so the device can re-pair.

    A Code=15 "failed to encrypt" connect error means macOS holds bonding
    keys that no longer match the ESP32's (e.g. after a firmware reflash or
    the on-device bond-clear gesture). The firmware pairs "just works" (no
    MITM), so once the stale bond is gone the next connect re-bonds silently
    with no GUI prompt.

    CoreBluetooth exposes no unpair API, so we shell out to `blueutil`. The
    daemon only knows the peripheral's CoreBluetooth UUID, not the BD_ADDR
    that blueutil needs, so we map by name via `blueutil --paired`. Returns
    True if a bond was removed. Mirrors the Linux daemon's `bluetoothctl
    remove` self-heal.
    """
    if not shutil.which("blueutil"):
        log("Stale bond detected but `blueutil` is not installed; cannot "
            "auto-recover. Run `brew install blueutil`, or forget "
            f"'{DEVICE_NAME}' in System Settings > Bluetooth and reconnect.")
        return False

    out = _blueutil("--paired")
    if out is None:
        return False

    # Each line looks like:
    #   address: 28-84-85-55-5c-3d, ... name: "Clawdmeter", ...
    addr = None
    for line in out.splitlines():
        nm = re.search(r'name:\s*"([^"]*)"', line)
        if nm and is_our_device_name(nm.group(1)):
            m = re.search(r"address:\s*([0-9a-fA-F:-]+)", line)
            if m:
                addr = m.group(1)
                break
    if not addr:
        log(f"No paired '{DEVICE_NAME}' found to unpair (already forgotten?)")
        return False

    if _blueutil("--unpair", addr) is None:
        return False
    log(f"Unpaired stale bond for '{DEVICE_NAME}' [{addr}]; re-pairing on "
        "next connect")
    return True


async def connect_and_run(target, stop_event: asyncio.Event, tray_state=None) -> bool:
    """Connect to a target and poll until disconnected or stopped.

    ``target`` is either an address string (Linux) or a BLEDevice carrying
    live CoreBluetooth details (macOS). Returns True if the connection was
    used successfully (so the caller keeps the cached address), False if the
    connection failed and the cache should be invalidated.
    """
    display = target if isinstance(target, str) else target.address
    log(f"Connecting to {display}...")
    client = BleakClient(target)
    try:
        # Bound the connect the same way #84 bounded the refresh subscribe.
        # On macOS the OS auto-connects the firmware's HID link, so
        # CoreBluetooth can hand us a half-open peripheral whose GATT connect
        # handshake never completes. BleakClient's own timeout governs
        # discovery, not connectPeripheral, so an unbounded await here wedges
        # the single-threaded daemon forever at "Connecting..." (observed ~13h,
        # device stuck on stale data). wait_for raises TimeoutError, which the
        # handler below already treats as a connection failure -> drop the
        # cached address and rescan.
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
    except (BleakError, asyncio.TimeoutError) as e:
        log(f"Connection failed: {e}")
        if sys.platform == "darwin" and _is_encryption_error(e):
            log("Encryption failed — likely a stale macOS bond; self-healing")
            unpair_macos()
        return False

    if not client.is_connected:
        log("Connection failed (no error but not connected)")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()

    last_poll = 0.0
    used_successfully = False
    last_payload: dict | None = None
    inbox = EventInbox()
    inbox_task = asyncio.ensure_future(inbox.watch())
    try:
        while client.is_connected and not stop_event.is_set() \
                and not (tray_state is not None and tray_state.paused):
            now = time.time()
            elapsed = now - last_poll
            if inbox.pending.is_set():
                ev = inbox.take()
                if ev and read_events_setting() == "on":
                    # Ride on the last numbers so the board keeps them; the
                    # firmware treats a payload without "s" as event-only.
                    ev_payload = dict(last_payload) if last_payload else {}
                    ev_payload["ev"] = ev
                    log(f"Claude Code event -> device: {ev}")
                    await session.write_payload(ev_payload)
            if session.refresh_requested.is_set() or elapsed >= POLL_INTERVAL:
                session.refresh_requested.clear()
                # Pure free-ride: read whatever access token(s) Claude Code
                # currently holds across the configured config dirs and NEVER
                # refresh them ourselves. Claude Code (the token's owner) does all
                # refreshing; refreshing here would race its rotation and feed the
                # OAuth endpoint's rate limit (429). When no dir has a usable token
                # we signal "No data" so the device idles instead of holding stale
                # numbers until the CLI re-seeds it.
                payload, dead = await poll_active()
                if payload is not None:
                    note_token_ok()
                    if await session.write_payload(payload):
                        last_poll = time.time()
                        used_successfully = True
                        last_payload = payload
                        if tray_state:
                            tray_state.set_connected(last_poll)
                            if spend_problem():
                                # Usage is flowing but the $ figure is stuck:
                                # say so instead of a clean green.
                                reason, fix, anchor = spend_problem()
                                tray_state.set_error(reason, fix, action=f"faq:{anchor}")
                elif dead:
                    # No live token in any config dir (missing, or a 401/expired
                    # token) -> show "No data" now instead of stale numbers. Guard
                    # last_poll on the write result (like the data path) so a
                    # failed beat retries next tick instead of throttling what may
                    # be a healthy link for a full POLL_INTERVAL.
                    log("No usable token; signalling no-data to device — run "
                        "`claude auth login` or use the CLI to let Claude Code renew it")
                    if note_no_token():
                        log("Posted the 'run claude auth login' desktop notification")
                    if tray_state:
                        tray_state.set_error(_last_dead_reason, LOGIN_FIX, action="login")
                    if await session.write_payload({"ok": False}):
                        last_poll = time.time()
                else:
                    # Transient poll failure (a live token that didn't answer this
                    # cycle) -> stay silent and retry after a short backoff. Not
                    # every TICK: a persistent failure (proxy, TLS, outage) would
                    # otherwise hammer the API ~12x a minute.
                    log(f"No usable config dir this cycle; retrying in {POLL_RETRY_AFTER_FAIL}s")
                    last_poll = time.time() - POLL_INTERVAL + POLL_RETRY_AFTER_FAIL
                    if tray_state and network_problem():
                        reason, fix, anchor = network_problem()
                        tray_state.set_error(reason, fix, action=f"faq:{anchor}")

            # Sleep until the next tick, a device refresh request, or a hook event.
            waiters = [asyncio.ensure_future(session.refresh_requested.wait()),
                       asyncio.ensure_future(inbox.pending.wait())]
            try:
                await asyncio.wait(waiters, timeout=TICK, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waiters:
                    w.cancel()
        if tray_state is not None and tray_state.paused and client.is_connected:
            # Menu-bar Disconnect: macOS keeps the board's HID (keyboard) link up on
            # its own, so our GATT disconnect alone leaves the board showing the
            # last numbers until its 90 s freshness window lapses. A final
            # {"ok": false} beat flips it to the idle screen immediately.
            await session.write_payload({"ok": False})
    finally:
        inbox_task.cancel()
        try:
            await client.disconnect()
        except BleakError:
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


async def main(tray_state=None) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    if tray_state is not None:
        tray_state.loop = loop
        tray_state.stop_event = stop_event

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    # Process signals can only be owned by the main thread. Under the menu-bar
    # front end this loop runs in a worker thread and menubar_macos routes
    # SIGTERM/SIGINT to stop_event for us.
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _stop)
            except NotImplementedError:
                signal.signal(sig, _stop)

    log("=== Claude Usage Tracker Daemon (BLE, macOS) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")

    backoff = 1
    skip_addr: str | None = None  # macOS: a peripheral to skip for one cycle
    was_paused = False
    while not stop_event.is_set():
        # Menu-bar "Disconnect": hold here (link already dropped by connect_and_run)
        # until "Reconnect" clears the flag. Nothing is polled meanwhile.
        if tray_state is not None and tray_state.paused:
            if not was_paused:
                log("Paused by user; not reconnecting until Reconnect is chosen")
                was_paused = True
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            continue
        if was_paused:
            log("Resumed by user")
            was_paused = False
            backoff = 1

        # Apply any pending skip exactly once, then clear it so the next
        # cycle re-tries retrieveConnected (the device may have recovered).
        target = await discover_target(skip_addr=skip_addr)
        skip_addr = None
        if not target:
            if tray_state:
                # No board yet — still tell the user if the login is the problem,
                # so a fresh install without `claude auth login` shows red, not amber.
                problem = login_problem()
                if problem:
                    tray_state.set_error(problem, LOGIN_FIX, action="login")
                elif bluetooth_problem():
                    # Permission denied / radio off: red with a one-click fix,
                    # instead of amber "waiting" that never resolves.
                    tray_state.set_error(*bluetooth_problem(), action="bluetooth")
                else:
                    tray_state.set_waiting()
            log(f"Device not found, retrying in {backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
            continue

        addr = target if isinstance(target, str) else target.address
        ok = await connect_and_run(target, stop_event, tray_state)
        if tray_state:
            tray_state.set_waiting()   # link dropped (or never came up); back to waiting
        if not ok:
            if sys.platform == "darwin":
                # No string cache to drop; instead skip this stale handle on
                # the next retrieveConnected so the scan fallback is reachable.
                skip_addr = addr
            else:
                log("Invalidating cached address")
                SAVED_ADDR_FILE.unlink(missing_ok=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("--install-hooks", "--remove-hooks"):
        print(install_claude_hooks(remove=sys.argv[1] == "--remove-hooks"))
        sys.exit(0)
    try:
        if sys.platform == "darwin" and read_menubar_setting() == "on":
            try:
                import menubar_macos   # sibling module (brew/tarball) or daemon/ on sys.path (repo)
            except ImportError:
                menubar_macos = None
            if menubar_macos is not None:
                menubar_macos.run(daemon_main=main, log=log)
                sys.exit(0)
            log("menubar = on but pystray/Pillow aren't installed; running headless")
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
