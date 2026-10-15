#!/bin/bash
# Builds "dist/Mac Storage Dashboard.app" — a self-contained launcher.
# Grant Full Disk Access to the app itself (not your terminal), and macOS
# attributes protected-folder access to "Mac Storage Dashboard" from then on.
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 was not found on this Mac."
  echo
  echo "Install Apple's command line tools with:"
  echo "    xcode-select --install"
  echo
  echo "Then double-click this file again."
  echo
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi

exec python3 -m mac_storage_dashboard app --out dist
