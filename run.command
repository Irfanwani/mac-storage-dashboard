#!/bin/bash
# Mac Storage Dashboard — double-click launcher.
# Starts the local server (which opens the dashboard in your browser).
cd "$(dirname "$0")" || exit 1

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

exec python3 -m mac_storage_dashboard run
