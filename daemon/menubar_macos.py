#!/usr/bin/env python3
"""macOS menu-bar status icon for the Clawdmeter daemon — port of tray_windows.py.

Provides:
  TrayState        — thread-safe scalar bridge (daemon loop writes, menu bar reads)
  effective_state  — pure: connected / stale / waiting / error for the current state
  header_text      — pure: the status line shown at the top of the menu
  run()            — entry: pystray.Icon on the main thread, the daemon's asyncio
                     loop in a supervised background thread

The daemon (claude_usage_daemon.main) is unchanged in logic; it only calls the
TrayState setters at existing branch points when it's handed a TrayState.

Ships in the release tarball and the Homebrew formula next to
claude_usage_daemon.py; the brand mark comes from logo_80.png (installed
alongside) or, in a repo checkout, ../assets/logo_80.png. Disable with
`menubar = off` in ~/.config/claude-usage-monitor/config.

Run tests: python -m pytest daemon/tests/test_macos_menubar.py -x -q
"""

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Connected but nothing successfully sent for this long -> amber "stale". This is
# the signature of the daemon running fine while every poll fails (a proxy
# certificate, an API outage): the device idles and, without this, the icon
# would stay green on a "connected" that last moved an hour ago.
STALE_AFTER_S = 180

# Troubleshooting page, served by GitHub Pages from the fork's docs/ folder.
FAQ_URL = "https://alvarr55.github.io/Clawdmeter/troubleshooting.html"

# Deep link to System Settings > Privacy & Security > Bluetooth (macOS 13+).
BT_SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth"

# What clicking the login "Fix:" line runs in a new Terminal window: the
# command is shown pre-typed and waits for Enter, so the user sees what is
# about to run and stays in control of when the browser sign-in starts.
LOGIN_SHELL_CMD = "clear; echo 'Press Enter to run:  claude auth login'; read -r; claude auth login"

LOG_CANDIDATES = (
    "/opt/homebrew/var/log/clawdmeter.log",                # brew services (Apple Silicon)
    "/usr/local/var/log/clawdmeter.log",                   # brew services (Intel)
    os.path.expanduser("~/Library/Logs/claude-usage-daemon.out.log"),  # install-mac.sh LaunchAgent
)


class TrayState:
    """Shared state bridging the daemon's asyncio loop to the menu bar.

    The loop writes via the set_* methods; the menu bar only reads the scalar
    attributes. Attribute assignment of simple scalars is atomic, so no lock.
    `loop` / `stop_event` are populated by the daemon's main() at startup so
    Quit can stop it via loop.call_soon_threadsafe (asyncio.Event isn't
    thread-safe; never set it from the menu-bar thread directly).
    """

    def __init__(self) -> None:
        self.state: str = "waiting"          # "connected" | "waiting" | "error"
        self.reason: str = ""                # error reason
        self.fix: str = ""                   # one-line remedy shown under the error
        self.action: str = ""                # "login" | "bluetooth" | "faq:<anchor>" | "": what clicking the fix does
        self.last_sync: float | None = None  # time.time() of the last successful write
        self.paused: bool = False            # user chose Disconnect: drop the link, don't reconnect
        self.loop = None
        self.stop_event = None

    def set_connected(self, ts: float) -> None:
        """After write_payload returned True. ts = time.time()."""
        self.state = "connected"
        self.reason = ""
        self.fix = ""
        self.action = ""
        self.last_sync = ts

    def set_waiting(self) -> None:
        """No device connected yet / link dropped. The daemon never scans, so
        this reads "waiting for the OS-connected device", not "scanning"."""
        self.state = "waiting"
        self.reason = ""
        self.fix = ""
        self.action = ""

    def set_error(self, why: str, fix: str = "", action: str = "") -> None:
        """Actionable failure only: no usable Claude Code login, or CoreBluetooth
        refused (permission denied / radio off). `action` names the one-click
        remedy the menu offers: "login" opens Terminal with `claude auth login` typed,
        "bluetooth" opens the Bluetooth privacy pane, "faq:<anchor>" opens
        that entry of the troubleshooting page."""
        self.state = "error"
        self.reason = why
        self.fix = fix
        self.action = action


def effective_state(ts: TrayState, now: float | None = None) -> str:
    """Icon state: 'paused' | 'connected' | 'stale' | 'waiting' | 'error'.

    'paused' (user chose Disconnect) wins over everything. 'stale' is derived
    here, not set by the daemon: connected, but the last successful send is
    older than STALE_AFTER_S.
    """
    if ts.paused:
        return "paused"
    if ts.state == "connected":
        now = time.time() if now is None else now
        if ts.last_sync is not None and now - ts.last_sync > STALE_AFTER_S:
            return "stale"
        return "connected"
    return ts.state


def header_text(ts: TrayState, now: float | None = None) -> str:
    """Menu status line for the current state."""
    st = effective_state(ts, now)
    if st == "paused":
        return "Disconnected (paused) — choose Reconnect to resume"
    if st in ("connected", "stale"):
        when = time.strftime("%H:%M", time.localtime(ts.last_sync)) if ts.last_sync else "never"
        return f"Connected · last update {when}" if st == "connected" \
            else f"Connected · no update since {when}"
    if st == "waiting":
        return "Waiting for device…"
    return f"Error: {ts.reason}"


def find_logo() -> Path | None:
    """logo_80.png next to this file (brew / tarball) or ../assets/ (repo checkout)."""
    here = Path(__file__).resolve().parent
    for cand in (here / "logo_80.png", here.parent / "assets" / "logo_80.png"):
        if cand.is_file():
            return cand
    return None


def find_log() -> str | None:
    """The most recently written of the known daemon log locations, if any."""
    existing = [p for p in LOG_CANDIDATES if os.path.isfile(p)]
    if not existing:
        return None
    return max(existing, key=os.path.getmtime)


def run(daemon_main, log) -> None:
    """Menu-bar entry. `daemon_main` is claude_usage_daemon.main (accepts
    tray_state=); `log` is its logger. Blocks until Quit or SIGTERM/SIGINT.

    pystray / Pillow imports live here, not at module top, so the pure helpers
    stay importable (and testable) without them.
    """
    import asyncio

    import pystray
    from pystray import Menu, MenuItem
    from PIL import Image

    try:
        from . import icon_assets          # repo layout: daemon/ is a package
    except ImportError:
        import icon_assets                 # brew / tarball layout: flat siblings

    logo = find_logo()
    if logo is None:
        log("menubar: logo_80.png not found next to the daemon; running headless")
        asyncio.run(daemon_main())
        return

    base = Image.open(logo).convert("RGBA")
    bubbles = icon_assets.build_state_icons(base, size=44)   # 22 pt @2x for the menu bar
    images = {
        "connected": bubbles["connected"],
        "stale": bubbles["scanning"],
        "waiting": bubbles["scanning"],
        "error": bubbles["error"],
        "paused": icon_assets.bubble_icon(base, icon_assets.PAUSED_BUBBLE, size=44),
    }

    ts = TrayState()
    icon = pystray.Icon("Clawdmeter", images["waiting"], "Clawdmeter")
    quit_requested = threading.Event()

    def _run_daemon() -> None:
        # Supervised: a crash logs, flips the icon to error, and restarts the
        # loop with capped backoff. A clean return means Quit — stay down.
        backoff = 2
        while not quit_requested.is_set():
            try:
                asyncio.run(daemon_main(tray_state=ts))
                return
            except Exception as e:  # last-resort thread guard
                import traceback
                log(f"Daemon thread crashed: {e!r}")
                log(traceback.format_exc())
                ts.set_error(f"daemon crashed: {type(e).__name__}")
            if quit_requested.wait(timeout=backoff):
                return
            backoff = min(backoff * 2, 30)

    daemon_thread = threading.Thread(target=_run_daemon, name="clawdmeter-daemon", daemon=True)
    daemon_thread.start()

    def _shutdown(icon_ref) -> None:
        # Signal the asyncio loop from its own thread, wait for the clean GATT
        # disconnect (so the device returns to its waiting screen instead of
        # freezing on stale numbers), then stop the icon and let the process exit.
        quit_requested.set()
        if ts.loop is not None and ts.stop_event is not None:
            ts.loop.call_soon_threadsafe(ts.stop_event.set)
            daemon_thread.join(timeout=6.0)
        icon_ref.stop()

    def _on_quit(icon_ref, _item) -> None:
        _shutdown(icon_ref)

    def _on_open_log(_icon_ref, _item) -> None:
        path = find_log()
        if path:
            subprocess.Popen(["open", path])

    def _on_open_faq(_icon_ref, _item) -> None:
        _open_faq()

    def _on_login(_icon_ref, _item) -> None:
        # The login itself is Claude Code's browser OAuth flow and must stay
        # interactive; the most we can automate is opening Terminal with the
        # command typed and waiting for Enter. (Terminal's `do script` always
        # executes, so the wait is the `read -r` inside LOGIN_SHELL_CMD.) The
        # first click asks the user to let Python control Terminal.
        subprocess.Popen(["osascript",
                          "-e", f'tell application "Terminal" to do script "{LOGIN_SHELL_CMD}"',
                          "-e", 'tell application "Terminal" to activate'])

    def _on_open_bt_settings(_icon_ref, _item) -> None:
        # Lands on the exact pane where "Python" (Homebrew's interpreter, which
        # is what macOS asked about) can be switched back on. The daemon keeps
        # retrying CoreBluetooth on its own, so no restart is needed after.
        subprocess.Popen(["open", BT_SETTINGS_URL])

    def _open_faq(anchor: str = "") -> None:
        subprocess.Popen(["open", f"{FAQ_URL}#{anchor}" if anchor else FAQ_URL])

    def _on_fix(icon_ref, item) -> None:
        # Clicking the "Fix: …" line performs the remedy it describes.
        if ts.action == "login":
            _on_login(icon_ref, item)
        elif ts.action == "bluetooth":
            _on_open_bt_settings(icon_ref, item)
        elif ts.action.startswith("faq:"):
            _open_faq(ts.action[4:])

    # Amber states get a help line too: the two "nothing is wrong, but nothing
    # works" situations that fill the FAQ, one click from the matching entry.
    HELP_LINES = {
        "waiting": ("Board not connecting? See the FAQ", "pairing"),
        "stale": ("No data arriving? See the FAQ", "nodata"),
    }

    def _on_help(_icon_ref, _item) -> None:
        entry = HELP_LINES.get(effective_state(ts))
        if entry:
            _open_faq(entry[1])

    def _on_toggle_pause(icon_ref, _item) -> None:
        # The daemon loop polls this flag: within one TICK it drops the BLE link
        # (board shows its waiting screen) and holds off reconnecting until cleared.
        ts.paused = not ts.paused
        log("menubar: Disconnect requested" if ts.paused else "menubar: Reconnect requested")
        icon_ref.update_menu()

    icon.menu = Menu(
        MenuItem(lambda _item: header_text(ts), None, enabled=False),
        # Second line, only in the error state: the exact remedy, and the one
        # click that performs it: the login line opens Terminal with
        # `claude auth login` typed, the Bluetooth line opens the privacy pane.
        MenuItem(lambda _item: f"Fix: {ts.fix}", _on_fix,
                 enabled=lambda _item: bool(ts.action),
                 visible=lambda _item: effective_state(ts) == "error" and bool(ts.fix)),
        MenuItem(lambda _item: HELP_LINES.get(effective_state(ts), ("", ""))[0], _on_help,
                 visible=lambda _item: effective_state(ts) in HELP_LINES),
        Menu.SEPARATOR,
        MenuItem(lambda _item: "Reconnect" if ts.paused else "Disconnect", _on_toggle_pause),
        MenuItem("Troubleshooting FAQs", _on_open_faq),
        MenuItem("Open log", _on_open_log, enabled=lambda _item: find_log() is not None),
        Menu.SEPARATOR,
        MenuItem("Quit Clawdmeter", _on_quit),
    )

    # launchd / brew services stop us with SIGTERM; route it through the same
    # clean shutdown as Quit. Python-level signal handlers only run when the
    # main thread is executing Python bytecode — and here the main thread lives
    # inside macOS's native event loop (icon.run), so a plain signal.signal
    # handler would NEVER fire: the process would ignore SIGTERM and its icon
    # would linger. set_wakeup_fd makes the C-level handler write the signal
    # number to a pipe the moment it arrives, native loop or not; a watcher
    # thread reads it and performs the shutdown (icon.stop() is thread-safe).
    sig_r, sig_w = os.pipe()
    os.set_blocking(sig_w, False)
    signal.set_wakeup_fd(sig_w, warn_on_full_buffer=False)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_a: None)   # keep the process alive; the pipe does the work

    def _signal_watcher() -> None:
        try:
            os.read(sig_r, 1)
        except OSError:
            return
        log("Signal received; shutting down")
        _shutdown(icon)

    threading.Thread(target=_signal_watcher, name="clawdmeter-signals", daemon=True).start()

    def _refresh(icon_ref: pystray.Icon) -> None:
        icon_ref.visible = True
        prev = {"state": None, "header": None}
        while not quit_requested.is_set():
            st = effective_state(ts)
            header = header_text(ts)
            if st != prev["state"]:
                icon_ref.icon = images[st]
            if st != prev["state"] or header != prev["header"]:
                icon_ref.title = header
                icon_ref.update_menu()
                prev["state"], prev["header"] = st, header
            time.sleep(1.0)

    # Menu-bar-only app: without this the process is a regular GUI app and
    # macOS shows Python's rocket icon in the Dock for as long as it runs.
    # Accessory policy = no Dock tile, no app menu, still allowed a status item.
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception as e:  # pragma: no cover - cosmetic; never block startup on it
        log(f"menubar: could not hide the Dock icon: {e!r}")

    log("menubar: status icon enabled (menubar = off in the config to disable)")
    icon.run(setup=_refresh)   # blocks the main thread until icon.stop()
