#!/bin/bash
# One-line macOS install from a GitHub Release — no git clone, no PlatformIO:
#
#   curl -fsSL https://github.com/ALVARR55/Clawdmeter/releases/latest/download/install-from-release.sh | bash
#
# Downloads clawdmeter-daemon-macos.tar.gz into $CLAWDMETER_HOME (default
# ~/.clawdmeter) and runs its interactive installer (install-mac.sh), which
# creates the Python venv, registers the LaunchAgent, and does the one
# foreground run macOS needs to show the Bluetooth permission prompt.
# Re-running upgrades in place: the venv is kept, everything else replaced.
#
#   CLAWDMETER_VERSION=v0.1.0  pin a release instead of latest
#   CLAWDMETER_HOME=/path      install somewhere other than ~/.clawdmeter
set -euo pipefail

REPO="${CLAWDMETER_REPO:-ALVARR55/Clawdmeter}"
VERSION="${CLAWDMETER_VERSION:-latest}"
DEST="${CLAWDMETER_HOME:-$HOME/.clawdmeter}"

if [ "$(uname -s)" != "Darwin" ]; then
    echo "This installer is for macOS. On Linux, clone the repo and run ./install.sh."
    exit 1
fi

if [ "$VERSION" = "latest" ]; then
    base="https://github.com/$REPO/releases/latest/download"
else
    base="https://github.com/$REPO/releases/download/$VERSION"
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

echo "Downloading Clawdmeter daemon ($VERSION) ..."
curl -fsSL "$base/clawdmeter-daemon-macos.tar.gz" -o "$tmp/daemon.tar.gz"
tar -xzf "$tmp/daemon.tar.gz" -C "$tmp"

mkdir -p "$DEST"
cp -R "$tmp/clawdmeter/." "$DEST/"
chmod +x "$DEST/install-mac.sh" "$DEST/flash-release.sh"
echo "Installed files to $DEST"
echo ""

cd "$DEST"
# install-mac.sh asks questions. Under `curl ... | bash` stdin is the script
# itself, so hand the prompts the terminal instead.
if [ -t 0 ]; then
    bash ./install-mac.sh
else
    bash ./install-mac.sh </dev/tty
fi

echo ""
echo "To flash a board with the matching prebuilt firmware (no PlatformIO):"
echo "  cd $DEST && ./flash-release.sh <board-env>      # e.g. waveshare_amoled_216_c6"
