#!/bin/bash
# Builds "dist/Mac Storage Dashboard.app" — a double-clickable launcher that
# starts the dashboard server, then exits. Grant Full Disk Access to THIS app
# (not your terminal) once, and macOS asks on its behalf from then on.
set -e
cd "$(dirname "$0")"
REPO="$(pwd -P)"
APP="dist/Mac Storage Dashboard.app"
SCRIPT="$(mktemp -t mac-storage-dashboard).applescript"

# NOTE: \$ escapes below keep bash from expanding AppleScript/shell variables
# at build time; the compiled applet expands them when it runs.
cat > "$SCRIPT" <<APPLESCRIPT
on run
	set embeddedRepo to "$REPO"

	-- Locate the project folder. Normally the app lives in <repo>/dist/.
	set appPath to POSIX path of (path to me)
	if appPath does not end with "/" then set appPath to appPath & "/"
	set repoPath to ""
	try
		set repoPath to do shell script "cd " & quoted form of (appPath & "../..") & " && pwd -P"
	end try
	if repoPath is "" or not my fileExists(repoPath & "/server.py") then
		set repoPath to embeddedRepo
	end if
	if not my fileExists(repoPath & "/server.py") then
		display dialog "Can't find the dashboard files (server.py, scanner.py).

Keep this app inside the 'mac-storage-dashboard' folder — next to server.py — or rebuild it with build_app.command." buttons {"OK"} default button "OK" with title "Mac Storage Dashboard" with icon caution
		return
	end if

	-- Find a usable python3.
	set py to ""
	try
		set py to do shell script "if command -v python3 >/dev/null 2>&1; then command -v python3; elif [ -x /opt/homebrew/bin/python3 ]; then echo /opt/homebrew/bin/python3; elif [ -x /usr/local/bin/python3 ]; then echo /usr/local/bin/python3; fi"
	end try
	if py is "" then
		display dialog "Python 3 was not found on this Mac.

Install Apple's command line tools with:

    xcode-select --install

then open this app again." buttons {"OK"} default button "OK" with title "Mac Storage Dashboard" with icon caution
		return
	end if

	-- Start the server detached; it keeps running and opens the dashboard in
	-- your browser. (If it's already running, the server itself just brings
	-- the browser back to it.) Stop it from the dashboard's ⏻ Stop button.
	do shell script "cd " & quoted form of repoPath & "; nohup env DASHBOARD_PARENT_APP=\"Mac Storage Dashboard\" " & quoted form of py & " server.py >> " & quoted form of (repoPath & "/scan_log.txt") & " 2>&1 &"
end run

on fileExists(p)
	try
		do shell script "test -f " & quoted form of p
		return true
	on error
		return false
	end try
end fileExists
APPLESCRIPT

rm -rf "$APP"
mkdir -p dist
osacompile -o "$APP" "$SCRIPT"
rm -f "$SCRIPT"

# friendlier identity in System Settings → Privacy & Security → Full Disk Access
PB=/usr/libexec/PlistBuddy
PLIST="$APP/Contents/Info.plist"
$PB -c "Set :CFBundleIdentifier com.macstoragedashboard.app" "$PLIST" 2>/dev/null \
  || $PB -c "Add :CFBundleIdentifier string com.macstoragedashboard.app" "$PLIST"
$PB -c "Set :CFBundleName Mac Storage Dashboard" "$PLIST" 2>/dev/null \
  || $PB -c "Add :CFBundleName string Mac Storage Dashboard" "$PLIST"
$PB -c "Set :CFBundleShortVersionString 1.0.0" "$PLIST" 2>/dev/null \
  || $PB -c "Add :CFBundleShortVersionString string 1.0.0" "$PLIST"
codesign --force -s - "$APP" 2>/dev/null || true

echo "Built: $APP"
echo
echo "Next steps:"
echo "  1. Double-click the app — the dashboard opens in your browser."
echo "     (Stop it any time with the Stop button in the dashboard.)"
echo "  2. First time only: System Settings → Privacy & Security → Full Disk Access,"
echo "     click +, add 'Mac Storage Dashboard', then launch the app again."
echo
echo "Keep the app inside this project folder (or move app + folder together)."
