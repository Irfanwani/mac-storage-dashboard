#!/usr/bin/env python3
"""mac-storage-dashboard — run the dashboard, or build/install the macOS app.

Commands:
  run        start the local dashboard server (opens in your browser)
  app        build a self-contained "Mac Storage Dashboard.app"
  install    build it and copy it into /Applications (or ~/Applications)
  doctor     check scan health / Full Disk Access (delegates to scanner --check)
  uninstall  remove the installed app (optionally its data folder)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__

PKG_DIR = Path(__file__).resolve().parent
APP_NAME = "Mac Storage Dashboard"
BUNDLE_ID = "com.macstoragedashboard.app"
DATA_DIR_SHELL_DEFAULT = "$HOME/Library/Application Support/Mac Storage Dashboard"
DATA_DIR_DEFAULT = Path.home() / "Library" / "Application Support" / APP_NAME
RUNTIME_FILES = ("server.py", "scanner.py", "index.html", "favicon.svg")

# The applet is intentionally tiny: it hands off to launch.sh in Resources.
# (An applet whose `run` handler does the work directly would stay "busy" —
# starting detached from a shell script keeps the applet instantly quittable.)
APPLET_SCRIPT = '''on run
	set launcherPath to POSIX path of (path to me) & "Contents/Resources/launch.sh"
	do shell script quoted form of launcherPath
end run
'''

# launch.sh: find python3, prepare the data folder, start server.py detached.
LAUNCH_SH = """#!/bin/bash
# Mac Storage Dashboard — launcher invoked by the applet.
# Finds python3, prepares the data folder, then starts the dashboard server
# detached and exits (the server keeps running; stop it from the dashboard).
set -u
RES="$(cd "$(dirname "$0")" && pwd -P)"
BAKED_PY='@@BAKED_PY@@'
DATA="${DASHBOARD_DATA_DIR:-@@DATA_DEFAULT@@}"

PY=""
[ -n "$BAKED_PY" ] && [ -x "$BAKED_PY" ] && PY="$BAKED_PY"
if [ -z "$PY" ] && command -v python3 >/dev/null 2>&1; then PY="$(command -v python3)"; fi
if [ -z "$PY" ] && [ -x /opt/homebrew/bin/python3 ]; then PY=/opt/homebrew/bin/python3; fi
if [ -z "$PY" ] && [ -x /usr/local/bin/python3 ]; then PY=/usr/local/bin/python3; fi
if [ -z "$PY" ]; then
  osascript >/dev/null 2>&1 <<'OSA'
display dialog "Python 3 was not found on this Mac." & return & return & "Install Apple's command line tools with:" & return & return & "    xcode-select --install" & return & return & "then open this app again." buttons {"OK"} default button "OK" with title "Mac Storage Dashboard" with icon caution
OSA
  exit 0
fi

mkdir -p "$DATA" 2>/dev/null
cd "$RES" 2>/dev/null || exit 0
nohup env DASHBOARD_PARENT_APP="Mac Storage Dashboard" DASHBOARD_DATA_DIR="$DATA" "$PY" server.py >> "$DATA/launch.log" 2>&1 &
exit 0
"""

NEXT_STEPS_APP = """
Next steps:
  1. Double-click the app — the dashboard opens in your browser.
     (Stop it any time with the Stop button in the dashboard.)
  2. First time only: System Settings → Privacy & Security → Full Disk Access,
     click +, add "Mac Storage Dashboard", then open the app again.
     (Granting it to the app — not your terminal or browser — is what lets the
     scan see protected folders like Mail, Messages and iPhone backups.)
"""


def _sh(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _sq(s: str) -> str:
    """Single-quote a string for bash."""
    return "'" + s.replace("'", "'\\''") + "'"


def _build_icon(tmp: Path) -> Path | None:
    """Render favicon.svg into AppIcon.icns (best effort)."""
    svg, js = PKG_DIR / "favicon.svg", PKG_DIR / "svg2png.js"
    if not (svg.is_file() and js.is_file()):
        return None
    src = tmp / "icon-src.png"
    r = _sh(["osascript", "-l", "JavaScript", str(js), str(svg), str(src), "1024"])
    if r.returncode != 0 or not src.is_file():
        return None
    iconset = tmp / "AppIcon.iconset"
    iconset.mkdir(exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale, suffix in ((1, ""), (2, "@2x")):
            px = size * scale
            out = iconset / f"icon_{size}x{size}{suffix}.png"
            _sh(["sips", "-z", str(px), str(px), str(src), "--out", str(out)])
    icns = tmp / "AppIcon.icns"
    r = _sh(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)])
    return icns if r.returncode == 0 and icns.is_file() else None


def _edit_plist(app: Path) -> None:
    plist = str(app / "Contents" / "Info.plist")
    pb = "/usr/libexec/PlistBuddy"
    for key, val in (("CFBundleIdentifier", BUNDLE_ID), ("CFBundleName", APP_NAME),
                     ("CFBundleShortVersionString", __version__), ("CFBundleVersion", __version__)):
        if _sh([pb, "-c", f"Set :{key} {val}", plist]).returncode != 0:
            _sh([pb, "-c", f"Add :{key} string {val}", plist])
    # osacompile sets CFBundleIconName (asset-catalog icon) which takes
    # precedence over CFBundleIconFile on modern macOS — drop it so ours is used.
    _sh([pb, "-c", "Delete :CFBundleIconName", plist])
    _sh([pb, "-c", "Delete :CFBundleIconFile", plist])
    _sh([pb, "-c", "Add :CFBundleIconFile string AppIcon", plist])


def _finish_app(app: Path) -> None:
    _sh(["codesign", "--force", "-s", "-", str(app)])
    os.utime(app, None)
    lsreg = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
             "LaunchServices.framework/Support/lsregister")
    if Path(lsreg).exists():
        _sh([lsreg, "-f", str(app)])


def build_app(out_dir: Path, data_dir: str | None = None) -> Path:
    """Build a self-contained "Mac Storage Dashboard.app" inside `out_dir`."""
    for name in RUNTIME_FILES:
        if not (PKG_DIR / name).is_file():
            raise SystemExit(f"missing runtime file: {PKG_DIR / name}")
    out_dir.mkdir(parents=True, exist_ok=True)
    app = out_dir / f"{APP_NAME}.app"

    with tempfile.TemporaryDirectory(prefix="msd-build-") as tmp_s:
        tmp = Path(tmp_s)
        script = tmp / "applet.applescript"
        script.write_text(APPLET_SCRIPT)

        if app.exists():
            shutil.rmtree(app)
        r = _sh(["osacompile", "-o", str(app), str(script)])
        if r.returncode != 0:
            raise SystemExit(f"osacompile failed: {r.stderr.strip()}")

        res = app / "Contents" / "Resources"
        res.mkdir(parents=True, exist_ok=True)
        for name in RUNTIME_FILES:
            shutil.copy2(PKG_DIR / name, res / name)

        py = str(Path(sys.executable).resolve()).replace("'", "'\\''")
        launch = (LAUNCH_SH.replace("@@BAKED_PY@@", py)
                  .replace("@@DATA_DEFAULT@@", data_dir or DATA_DIR_SHELL_DEFAULT))
        lp = res / "launch.sh"
        lp.write_text(launch)
        lp.chmod(0o755)

        icon = _build_icon(tmp)
        if icon:
            shutil.copy2(icon, res / "AppIcon.icns")
            shutil.copy2(icon, res / "applet.icns")

        _edit_plist(app)
        _finish_app(app)
    return app


def cmd_app(args) -> int:
    app = build_app(Path(args.out), data_dir=args.data_dir)
    print(f"Built: {app}")
    print(NEXT_STEPS_APP)
    print("The app is self-contained — move it anywhere (e.g. into /Applications).")
    print(f"Data stays on this Mac in {DATA_DIR_DEFAULT}.")
    return 0


def cmd_install(args) -> int:
    with tempfile.TemporaryDirectory(prefix="msd-install-") as tmp:
        app = build_app(Path(tmp), data_dir=args.data_dir)
        candidates = ([Path(args.apps_dir)] if args.apps_dir
                      else [Path("/Applications"), Path.home() / "Applications"])
        dest = None
        for d in candidates:
            try:
                d.mkdir(parents=True, exist_ok=True)
                target = d / app.name
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(app, target, symlinks=True)
                dest = target
                break
            except OSError as exc:
                print(f"(skipping {d}: {exc})", file=sys.stderr)
        if dest is None:
            print("Could not install the app: no writable Applications folder.", file=sys.stderr)
            return 1
        _finish_app(dest)
    print(f"Installed: {dest}")
    print(NEXT_STEPS_APP)
    print(f"Data stays on this Mac in {DATA_DIR_DEFAULT}.")
    return 0


def cmd_run(args) -> int:
    env = dict(os.environ)
    env.setdefault("DASHBOARD_DATA_DIR", args.data_dir or str(DATA_DIR_DEFAULT))
    cmd = [sys.executable, str(PKG_DIR / "server.py")]
    if args.port:
        cmd.append(str(args.port))
    print(f"Data folder: {env['DASHBOARD_DATA_DIR']}")
    try:
        return subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        return 0


def cmd_doctor(args) -> int:
    env = dict(os.environ)
    env.setdefault("DASHBOARD_DATA_DIR", str(DATA_DIR_DEFAULT))
    return subprocess.call([sys.executable, str(PKG_DIR / "scanner.py"), "--check"], env=env)


def cmd_uninstall(args) -> int:
    apps = ([Path(args.apps_dir)] if args.apps_dir
            else [Path("/Applications"), Path.home() / "Applications"])
    removed = []
    for d in apps:
        app = d / f"{APP_NAME}.app"
        if app.is_dir():
            try:
                shutil.rmtree(app)
                removed.append(str(app))
            except OSError as exc:
                print(f"Could not remove {app}: {exc}", file=sys.stderr)
    if removed:
        print("Removed:")
        for p in removed:
            print(f"  {p}")
    else:
        print("Nothing to remove — app not found in /Applications or ~/Applications.")
    if args.purge_data:
        if DATA_DIR_DEFAULT.is_dir():
            shutil.rmtree(DATA_DIR_DEFAULT)
            print(f"Removed data folder: {DATA_DIR_DEFAULT}")
        else:
            print(f"No data folder at {DATA_DIR_DEFAULT}.")
    else:
        print(f"Scan data was kept in {DATA_DIR_DEFAULT} (use --purge-data to remove it).")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mac-storage-dashboard", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    sp = sub.add_parser("run", help="start the local dashboard server")
    sp.add_argument("--port", type=int, help="port to listen on (default 8899)")
    sp.add_argument("--data-dir", help="folder for data.json / scan state")

    sp = sub.add_parser("app", help='build "Mac Storage Dashboard.app"')
    sp.add_argument("--out", default="dist", help="output folder (default: dist)")
    sp.add_argument("--data-dir", help=argparse.SUPPRESS)

    sp = sub.add_parser("install", help="build and copy the app into /Applications")
    sp.add_argument("--apps-dir", help="install to this folder instead of /Applications")
    sp.add_argument("--data-dir", help=argparse.SUPPRESS)

    sub.add_parser("doctor", help="check scan health / Full Disk Access")

    sp = sub.add_parser("uninstall", help="remove the installed app")
    sp.add_argument("--apps-dir", help="remove from this folder instead of the defaults")
    sp.add_argument("--purge-data", action="store_true", help="also delete the scan data folder")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 0
    handlers = {"run": cmd_run, "app": cmd_app, "install": cmd_install,
                "doctor": cmd_doctor, "uninstall": cmd_uninstall}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
