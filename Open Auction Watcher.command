#!/bin/bash
# First open on a Mac: right-click this file and choose Open if macOS blocks it.
cd "$(dirname "$0")"
APP="Estate-Ez Auction Watcher.app"
if [[ ! -d "$APP" ]]; then
  echo "Could not find ${APP} next to this file."
  echo "Unzip the whole folder and leave this file where it is."
  read -r -p "Press Return to close."
  exit 1
fi
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true
xattr -dr com.apple.quarantine "$(dirname "$0")" 2>/dev/null || true
open "$APP"
