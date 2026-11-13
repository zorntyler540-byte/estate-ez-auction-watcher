#!/bin/bash
# Build a double-clickable Mac app. Run this on a Mac (Apple silicon or Intel).
# GitHub Actions runs it on macos-latest, which is Apple silicon.
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="Estate-Ez Auction Watcher"
ROOT="$(pwd)"
BROWSERS="${PLAYWRIGHT_BROWSERS_PATH:-$ROOT/.playwright-browsers}"
DIST_APP="$ROOT/dist/${APP_NAME}.app"
MACOS="$DIST_APP/Contents/MacOS"
STAGE="$ROOT/dist/${APP_NAME}"
ZIP="$ROOT/dist/Estate-Ez-Auction-Watcher-mac.zip"

PYTHON="${PYTHON:-python3}"

echo "=== Installing build dependencies ==="
"$PYTHON" -m pip install -r requirements.txt

echo "=== Installing Playwright Chromium ==="
export PLAYWRIGHT_BROWSERS_PATH="$BROWSERS"
"$PYTHON" -m playwright install chromium

echo "=== Building the app ==="
"$PYTHON" -m PyInstaller --noconfirm --clean "Estate-Ez Auction Watcher.spec"

if [[ ! -d "$DIST_APP" ]]; then
  echo "Build finished but the app was not found at:"
  echo "  $DIST_APP"
  exit 1
fi

echo "=== Bundling Chromium and the franchise config ==="
rm -rf "$MACOS/ms-playwright"
cp -R "$BROWSERS" "$MACOS/ms-playwright"
cp config.franchise.json "$MACOS/config.json"

rm -rf "$STAGE"
mkdir -p "$STAGE"
cp -R "$DIST_APP" "$STAGE/"
cp config.franchise.json "$STAGE/config.json"
cp "HOW TO INSTALL MAC.txt" "$STAGE/HOW TO INSTALL.txt"
cp "Open Auction Watcher.command" "$STAGE/Open Auction Watcher.command"
chmod +x "$STAGE/Open Auction Watcher.command"

echo "=== Ad-hoc signature (required on Apple silicon) ==="
codesign --force --deep --sign - "$STAGE/${APP_NAME}.app"

rm -f "$ZIP"
ditto -c -k --keepParent "$STAGE" "$ZIP"

echo
echo "DONE"
echo "  Folder: $STAGE"
echo "  Zip:    $ZIP"
echo "Send the zip. The client unzips it and double-clicks Open Auction Watcher.command."
