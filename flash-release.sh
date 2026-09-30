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
BOARD=""
PORT=""
NAME=""        # --name <suffix>: advertise as "Clawdmeter-<suffix>" (or "-" to clear)
while [ $# -gt 0 ]; do
    case "$1" in
        --name|-n) NAME="${2:-}"; [ $# -ge 2 ] && shift ;;
        --name=*)  NAME="${1#--name=}" ;;
        -h|--help) BOARD="" ; NAME=""; break ;;
        *) if [ -z "$BOARD" ]; then BOARD="$1"; else PORT="$1"; fi ;;
    esac
    shift
done

ENVS="waveshare_amoled_216 waveshare_amoled_18 waveshare_amoled_216_c6 waveshare_amoled_18_c6 waveshare_amoled_206 waveshare_lcd_154 waveshare_lcd_4"

if [ -z "$BOARD" ] && [ -z "$NAME" ]; then
    echo "Usage: $0 <board-env> [serial-port] [--name <suffix>]"
    echo "       $0 --name <suffix> [serial-port]        # rename an already-flashed board"
    echo "Board envs:"
    for e in $ENVS; do echo "  $e"; done
    echo "--name gives the board a unique Bluetooth name, Clawdmeter-<suffix> (1-7 letters,"
    echo "digits, '-' or '_'), shown on its pairing screen. '--name -' restores plain Clawdmeter."
    exit 1
fi
if [ -n "$BOARD" ]; then
    case " $ENVS " in
        *" $BOARD "*) ;;
        *) echo "Error: unknown board env '$BOARD'"; exit 1 ;;
    esac
fi
if [ -n "$NAME" ] && [ "$NAME" != "-" ]; then
    case "$NAME" in
        *[!A-Za-z0-9_-]*|???????????*) 
            echo "Error: --name must be 1-7 characters: letters, digits, '-' or '_' (got '$NAME')"; exit 1 ;;
    esac
    if [ ${#NAME} -gt 7 ]; then echo "Error: --name must be at most 7 characters (got '$NAME')"; exit 1; fi
fi
case "$BOARD" in
    *_c6) CHIP=esp32c6 ;;
    *)    CHIP=esp32s3 ;;
esac

BIN="$SCRIPT_DIR/clawdmeter-firmware-$BOARD.factory.bin"
if [ -n "$BOARD" ] && [ ! -f "$BIN" ]; then
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

# esptool needs Python >= 3.10. Prefer an explicit CLAWDMETER_PYTHON (the
# Homebrew wrapper sets it to the formula's venv), then the daemon venv
# install-mac.sh made; otherwise make a small one next to this script.
PY="${CLAWDMETER_PYTHON:-$SCRIPT_DIR/daemon/.venv/bin/python}"
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
"$PY" -c "import serial" >/dev/null 2>&1 || "$PY" -m pip install --quiet pyserial

if [ -n "$BOARD" ]; then
    echo "Flashing $BOARD ($CHIP) on $PORT ..."
    "$PY" -m esptool --chip "$CHIP" --port "$PORT" --baud 921600 write_flash 0x0 "$BIN"
    echo "Done. The board reboots into the splash; tap the screen for the Usage view."
fi

if [ -n "$NAME" ]; then
    # The firmware's serial console takes `name <suffix>` (persisted in NVS,
    # advertising restarts at once) and answers NAME_OK / NAME_ERR on one line.
    # After a flash the native-USB port disappears while the chip reboots, so
    # wait for it to come back before talking.
    "$PY" - "$PORT" "$NAME" <<'PYEOF'
import sys, time, serial
port, suffix = sys.argv[1], sys.argv[2]
deadline = time.time() + 25
ser = None
while time.time() < deadline:
    try:
        ser = serial.Serial(port, 115200, timeout=1)
        break
    except (serial.SerialException, OSError):
        time.sleep(0.5)
if ser is None:
    sys.exit(f"Error: {port} did not come back after the reboot; unplug/replug and re-run with --name")
time.sleep(2.5)                       # let setup() finish (BLE init) before the command
ser.reset_input_buffer()
ser.write(f"name {suffix}\n".encode()); ser.flush()
reply = None
end = time.time() + 6
while time.time() < end:
    line = ser.readline().decode(errors="replace").strip()
    if line.startswith(("NAME_OK", "NAME_ERR")):
        reply = line
        break
ser.close()
if reply is None:
    sys.exit("Error: no answer from the board (is the firmware v0.3.0 or newer?)")
if reply.startswith("NAME_ERR"):
    sys.exit("Error: " + reply[len("NAME_ERR "):])
shown = reply.split(" ", 1)[1]
print(f"Bluetooth name set to {shown}. It shows on the board's pairing screen; a Mac that is")
print("already paired keeps the old name until you re-pair (or rename it locally).")
PYEOF
fi
