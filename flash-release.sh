#!/bin/bash
# Flash a prebuilt release firmware image with esptool — no PlatformIO needed.
#
#   ./flash-release.sh <board-env> [serial-port]
#
# Uses clawdmeter-firmware-<board-env>.factory.bin next to this script, or
# downloads it from the fork's latest release (pin one with CLAWDMETER_VERSION=vX.Y.Z).
# The image is a merged bootloader+partitions+app, so it's written at 0x0.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="${CLAWDMETER_REPO:-ALVARR55/Clawdmeter}"
VERSION="${CLAWDMETER_VERSION:-latest}"
BOARD="${1:-}"
PORT="${2:-}"

ENVS="waveshare_amoled_216 waveshare_amoled_18 waveshare_amoled_216_c6 waveshare_amoled_18_c6 waveshare_amoled_206 waveshare_lcd_154 waveshare_lcd_4"

if [ -z "$BOARD" ]; then
    echo "Usage: $0 <board-env> [serial-port]"
    echo "Board envs:"
    for e in $ENVS; do echo "  $e"; done
    exit 1
fi
case " $ENVS " in
    *" $BOARD "*) ;;
    *) echo "Error: unknown board env '$BOARD'"; exit 1 ;;
esac
case "$BOARD" in
    *_c6) CHIP=esp32c6 ;;
    *)    CHIP=esp32s3 ;;
esac

BIN="$SCRIPT_DIR/clawdmeter-firmware-$BOARD.factory.bin"
if [ ! -f "$BIN" ]; then
    if [ "$VERSION" = "latest" ]; then
        url="https://github.com/$REPO/releases/latest/download/$(basename "$BIN")"
    else
        url="https://github.com/$REPO/releases/download/$VERSION/$(basename "$BIN")"
    fi
    echo "Downloading $(basename "$BIN") ..."
    curl -fL --progress-bar "$url" -o "$BIN"
fi

if [ -z "$PORT" ]; then
    PORT=$(ls /dev/cu.usbmodem* 2>/dev/null | head -1 || true)
    if [ -z "$PORT" ]; then
        echo "Error: no /dev/cu.usbmodem* device. Plug the board in with a data-capable"
        echo "       USB-C cable (many charger cables are power-only), or pass the port."
        exit 1
    fi
fi

# esptool needs Python >= 3.10. Prefer the daemon venv (install-mac.sh made it
# with a new-enough interpreter); otherwise make a small one next to this script.
PY="$SCRIPT_DIR/daemon/.venv/bin/python"
if [ ! -x "$PY" ]; then
    py_ge_310() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; }
    PYTHON3=""
    for cand in "$(command -v python3.13)" "$(command -v python3.12)" \
                "$(command -v python3.11)" "$(command -v python3.10)" \
                /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3)"; do
        [ -n "$cand" ] && [ -x "$cand" ] || continue
        if py_ge_310 "$cand"; then PYTHON3="$cand"; break; fi
    done
    if [ -z "$PYTHON3" ]; then
        echo "Error: need Python >= 3.10 for esptool. Install with: brew install python"
        exit 1
    fi
    PY="$SCRIPT_DIR/.flash-venv/bin/python"
    [ -x "$PY" ] || "$PYTHON3" -m venv "$SCRIPT_DIR/.flash-venv"
fi
"$PY" -m esptool version >/dev/null 2>&1 || "$PY" -m pip install --quiet esptool

echo "Flashing $BOARD ($CHIP) on $PORT ..."
"$PY" -m esptool --chip "$CHIP" --port "$PORT" --baud 921600 write_flash 0x0 "$BIN"
echo "Done. The board reboots into the splash; tap the screen for the Usage view."
