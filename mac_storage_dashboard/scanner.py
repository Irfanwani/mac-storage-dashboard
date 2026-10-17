#!/usr/bin/env python3
"""
Mac Storage Dashboard — scanner.

Maps your disk with a single-pass Python walker (pure os.scandir, no external
tools since macOS privacy protection silently breaks `du` for some folders),
classifies reclaimable space into "safe" / "review", and writes data.json.

Usage:  python3 scanner.py
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
HERE = Path(__file__).resolve().parent                          # code + web assets
DATA_DIR = Path(os.environ.get("DASHBOARD_DATA_DIR") or HERE)   # scan output + state
DATA_JSON = DATA_DIR / "data.json"
STATE_JSON = DATA_DIR / "scan_state.json"
LOG_TXT = DATA_DIR / "scan_log.txt"

MIN_REC = 200 * 1024 * 1024       # minimum size for a recommendation item
MIN_TREE = 150 * 1024 * 1024      # minimum size for a treemap child
BIG_FILE = 300 * 1024 * 1024      # big-file collector threshold
DOWNLOAD_BIG = 80 * 1024 * 1024   # downloads candidate threshold

VERSION = "1.0.0"

_iot = 512  # st_blocks are 512-byte units on macOS

# ---------------------------------------------------------------------------
# state / logging
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_state: dict = {"status": "idle", "message": "", "started_at": None,
                "finished_at": None, "error": None}
_last_state_write = 0.0


def _write_state() -> None:
    tmp = STATE_JSON.with_suffix(".tmp")
    with _lock:
        snapshot = dict(_state)
    try:
        tmp.write_text(json.dumps(snapshot, ensure_ascii=True))
        tmp.replace(STATE_JSON)
    except OSError:
        pass


def set_state(status: str | None = None, message: str | None = None,
              error: str | None = None, force: bool = False) -> None:
    global _last_state_write
    now = time.time()
    with _lock:
        if status is not None:
            _state["status"] = status
        if message is not None:
            _state["message"] = message
        if status == "running" and not _state["started_at"]:
            _state["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if status in ("done", "error"):
            _state["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if error is not None:
            _state["error"] = error
    if force or now - _last_state_write > 1.5:
        _last_state_write = now
        _write_state()


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_TXT, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n:.0f} B"
        n /= 1000.0
    return f"{n:.1f} TB"


def sh(cmd, timeout: float = 20.0, shell: bool = False) -> str:
    try:
        r = subprocess.run(cmd, shell=shell, capture_output=True, text=True,
                           timeout=timeout, errors="replace")
        return r.stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# walker
# ---------------------------------------------------------------------------

PROJECT_MARKERS = {"package.json", "pubspec.yaml", "go.mod", "Cargo.toml",
                   "Gemfile", "requirements.txt", "pom.xml", "build.gradle",
                   "build.gradle.kts", "Podfile", "composer.json", "Package.swift",
                   "pyproject.toml", "flake.nix", "CMakeLists.txt", "mix.exs"}

BUILD_DIRS = {"node_modules", ".next", ".turbo", ".parcel-cache", ".svelte-kit",
              ".nuxt", "venv", ".venv"}


class Walker:
    """Single-pass size walker with collectors."""

    def __init__(self, root: str, *, tree=None, detail=None, junk=None,
                 builds=None, bigfiles=None, downloads=None, markers=None,
                 pycache=None, collect_progress=None):
        self.root = os.path.abspath(root)
        self.root_dev = os.stat(self.root).st_dev if os.path.exists(self.root) else 0
        self.tree = tree                # {relpath: (depth, size)} for depth<=2
        self.detail = detail or set()   # dirs whose direct children we record
        self.junk = junk or set()       # exact dir paths -> size
        self.builds = builds if builds is not None else []
        self.bigfiles = bigfiles if bigfiles is not None else []
        self.downloads = downloads if downloads is not None else []
        self.markers = markers if markers is not None else set()
        self.pycache = pycache if pycache is not None else []
        self.child_sums: dict[str, list] = {}
        self.junk_sizes: dict[str, int] = {}
        self.denied_paths: set[str] = set()
        self.total = 0
        self._live_bytes = 0
        self.files = 0
        self.errors = 0
        self._seen_inodes: set = set()
        self._progress = collect_progress

    def walk(self) -> int:
        size, _ = self._walk(self.root, 0, None, False, False)
        self.total = size
        return size

    def _walk(self, d: str, depth: int, parent: str | None,
              in_build: bool, in_downloads: bool) -> tuple[int, float]:
        size = 0
        maxmt = 0.0
        children: list = []
        is_downloads = in_downloads or (parent == self.root and os.path.basename(d) == "Downloads")
        try:
            entries = os.scandir(d)
        except PermissionError:
            if len(self.denied_paths) < 200:
                self.denied_paths.add(os.path.normpath(d))
            self.errors += 1
            return 0, 0
        except OSError:
            self.errors += 1
            return 0, 0
        with entries:
            for e in entries:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        if st.st_dev != self.root_dev:
                            continue
                        sub, mt = self._walk(e.path, depth + 1, d, in_build or e.name in BUILD_DIRS, is_downloads)
                        size += sub
                        children.append((e.name, sub, mt))
                        if mt > maxmt:
                            maxmt = mt
                        if e.name in BUILD_DIRS and not in_build:
                            if e.name == "node_modules" or e.name in BUILD_DIRS:
                                self.builds.append({"name": e.name, "path": e.path,
                                                    "size": sub, "mtime": mt})
                        elif e.name == "__pycache__" and not in_build and len(self.pycache) < 1000:
                            self.pycache.append({"path": e.path, "size": sub})
                        if self.tree is not None and depth + 1 <= 2:
                            rel = os.path.relpath(e.path, self.root)
                            self.tree[rel] = (depth + 1, sub)
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        if st.st_nlink > 1:
                            key = (st.st_dev, st.st_ino)
                            if key in self._seen_inodes:
                                continue
                            self._seen_inodes.add(key)
                        sz = st.st_blocks * _iot if st.st_blocks > 0 else st.st_size
                        size += sz
                        self.files += 1
                        self._live_bytes += sz
                        if st.st_mtime > maxmt:
                            maxmt = st.st_mtime
                        if e.name in PROJECT_MARKERS and depth <= 2:
                            self.markers.add(os.path.dirname(e.path))
                        if not in_build:
                            if sz >= BIG_FILE and d != self.root:
                                self.bigfiles.append({"path": e.path, "size": sz,
                                                      "mtime": st.st_mtime})
                            if is_downloads and sz >= DOWNLOAD_BIG:
                                self.downloads.append({"path": e.path, "size": sz,
                                                       "mtime": st.st_mtime})
                        if self._progress and self.files % 4000 == 0:
                            self._progress()
                except OSError:
                    self.errors += 1
        norm = os.path.normpath(d)
        if norm in self.junk:
            self.junk_sizes[norm] = size
        if norm in self.detail:
            self.child_sums[norm] = children
        if self._progress:
            self._progress()
        return size, maxmt


# ---------------------------------------------------------------------------
# system info
# ---------------------------------------------------------------------------

def sys_info() -> dict:
    info = {
        "model": sh(["sysctl", "-n", "hw.model"]),
        "chip": sh(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "os": sh(["sw_vers", "-productVersion"]),
        "hostname": sh(["scutil", "--get", "ComputerName"]) or os.uname().nodename,
        "user": os.environ.get("USER", ""),
        "home": str(HOME),
    }
    prof = sh(["system_profiler", "SPHardwareDataType", "-detailLevel", "mini"], 25)
    m = re.search(r"Model Name:\s*(.+)", prof)
    if m:
        info["model_name"] = m.group(1).strip()
    m = re.search(r"Chip:\s*(.+)", prof)
    if m:
        info["chip"] = m.group(1).strip()
    try:
        info["ram_bytes"] = int(sh(["sysctl", "-n", "hw.memsize"]) or 0)
    except ValueError:
        info["ram_bytes"] = 0
    return info


def disk_info() -> dict:
    out = sh(["df", "-k", "/System/Volumes/Data"])
    if not out:
        out = sh(["df", "-k", "/"])
    parts = None
    for line in out.splitlines()[1:]:
        cols = line.split()
        if len(cols) >= 4:
            parts = cols
            break
    if not parts:
        return {}
    total, used, avail = (int(parts[1]) * 1024, int(parts[2]) * 1024, int(parts[3]) * 1024)
    pct = round(used / total * 100) if total else 0
    return {"total": total, "used": used, "free": avail, "percent_used": pct}


def memory_info() -> dict:
    out = sh(["vm_stat"])
    page = 16384
    m = re.search(r"page size of (\d+) bytes", out)
    if m:
        page = int(m.group(1))

    def val(label: str) -> int:
        m2 = re.search(r"^" + re.escape(label) + r":\s+(\d+)\.", out, re.M)
        return int(m2.group(1)) * page if m2 else 0

    total = 0
    try:
        total = int(sh(["sysctl", "-n", "hw.memsize"]) or 0)
    except ValueError:
        pass
    wired = val("Pages wired down")
    active = val("Pages active")
    inactive = val("Pages inactive")
    compressed = val("Pages occupied by compressor")
    free = val("Pages free") + val("Pages speculative")
    swap_total = swap_used = 0
    sw = sh(["sysctl", "-n", "vm.swapusage"])
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", sw)
    if m:
        swap_total = int(float(m.group(1)) * 1024 * 1024)
        swap_used = int(float(m.group(2)) * 1024 * 1024)
    free_pct_str = sh(["memory_pressure", "-Q"], 10)
    m = re.search(r"free percentage:\s*(\d+)", free_pct_str)
    free_pct = int(m.group(1)) if m else (round(free / total * 100) if total else 0)
    return {"total": total, "wired": wired, "active": active, "inactive": inactive,
            "compressed": compressed, "free": free, "free_percent": free_pct,
            "swap_total": swap_total, "swap_used": swap_used}


def snapshots_info() -> list:
    out = sh(["tmutil", "listlocalsnapshots", "/"], 15)
    return [ln.strip() for ln in out.splitlines() if "com.apple.TimeMachine" in ln]


# ---------------------------------------------------------------------------
# permissions (macOS TCC / Full Disk Access)
# ---------------------------------------------------------------------------

PROTECTED_CANARIES = [
    ("Desktop", "~/Desktop"),
    ("Documents", "~/Documents"),
    ("Downloads", "~/Downloads"),
    ("Mail", "~/Library/Mail"),
    ("Messages", "~/Library/Messages"),
    ("Safari", "~/Library/Safari"),
    ("Photos Library", "~/Pictures/*.photoslibrary"),
    ("iPhone/iPad backups", "~/Library/Application Support/MobileSync"),
    ("iCloud Drive", "~/Library/Mobile Documents"),
]


def parent_app() -> str:
    """Best guess at the app macOS will attribute the scan to (for the FDA hint)."""
    forced = os.environ.get("DASHBOARD_PARENT_APP")
    if forced:
        return forced
    try:
        out = sh(["ps", "-axo", "pid=,ppid=,comm="], 10)
        procs: dict[int, tuple[int, str]] = {}
        for line in out.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
                procs[int(parts[0])] = (int(parts[1]), parts[2])
        pid = os.getpid()
        seen: set[int] = set()
        while pid in procs and pid not in seen:
            seen.add(pid)
            ppid, comm = procs[pid]
            m = re.search(r"(.+?\.app)/Contents/MacOS/", comm)
            if m:
                name = os.path.basename(m.group(1)).removesuffix(".app")
                if "helper" not in name.lower():   # skip Electron/Chromium helper processes
                    return name
            pid = ppid
    except Exception:
        pass
    # detached sessions (tmux/screen) lose the process chain — fall back to env
    tp = os.environ.get("TERM_PROGRAM")
    if tp:
        return {
            "Apple_Terminal": "Terminal", "iTerm.app": "iTerm2", "vscode": "Visual Studio Code",
            "WarpTerminal": "Warp", "Hyper": "Hyper", "WezTerm": "WezTerm", "kitty": "kitty",
            "ghostty": "Ghostty", "Tabby": "Tabby", "Alacritty": "Alacritty",
            "tmux": "your terminal app",
        }.get(tp, tp)
    return "your terminal app"


def check_permissions() -> dict:
    """Try to read macOS-protected folders to detect missing Full Disk Access."""
    denied = []
    import glob as _g
    for label, pattern in PROTECTED_CANARIES:
        matches = _g.glob(os.path.expanduser(pattern))
        for path in matches:
            if not os.path.exists(path):
                continue
            try:
                with os.scandir(path):
                    pass
            except PermissionError:
                denied.append({"label": label, "path": path})
            except OSError:
                pass
            break  # one match per canary is enough
    return {"denied": denied, "likely_full_disk_access": len(denied) == 0}


def is_deletable_path(p: str) -> bool:
    """True if the dashboard's server could delete this path without sudo."""
    try:
        p = os.path.normpath(p)
        if not os.path.exists(p):
            return False
        parent = os.path.dirname(p) or "/"
        if not os.access(parent, os.W_OK):
            return False
        if os.path.isdir(p) and not os.access(p, os.W_OK):
            return False
        return True
    except OSError:
        return False


def doctor() -> int:
    """`python3 scanner.py --check` — report permission readiness."""
    import platform
    print(f"Mac Storage Dashboard {VERSION} — permission check")
    print(f"Python {platform.python_version()}  (needs 3.9+)")
    app = parent_app()
    perm = check_permissions()
    ok = True
    print(f"\nHost app that macOS will ask to grant Full Disk Access: \033[1m{app}\033[0m\n")
    for label, pattern in PROTECTED_CANARIES:
        import glob as _g
        matches = _g.glob(os.path.expanduser(pattern))
        if not matches:
            print(f"  –  {label:22} (not present, skipped)")
            continue
        path = matches[0]
        try:
            with os.scandir(path):
                pass
            print(f"  \033[32m✓\033[0m  {label:22} readable")
        except PermissionError:
            ok = False
            print(f"  \033[31m✗\033[0m  {label:22} PERMISSION DENIED  ({path})")
        except OSError as exc:
            print(f"  –  {label:22} skipped ({exc.__class__.__name__})")
    print()
    if ok and not perm["denied"]:
        print("\033[32mAll protected folders are readable — scans will be accurate.\033[0m")
    else:
        print("Some folders are blocked by macOS privacy protection (TCC).")
        print("To get accurate sizes, grant Full Disk Access:")
        print("  1. Open  System Settings → Privacy & Security → Full Disk Access")
        print(f"  2. Enable \033[1m{app}\033[0m (add it with + if not listed)")
        print(f"  3. Fully quit and reopen {app}, then run the scan again")
        print("  The dashboard keeps working without it — blocked folders are simply skipped.")
    d = disk_info()
    if d:
        print(f"\nDisk: {d['total']/1e9:.0f} GB total · {d['used']/1e9:.0f} GB used · "
              f"{d['free']/1e9:.1f} GB free ({d['percent_used']}%)")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# junk specs
# ---------------------------------------------------------------------------

def S(path: str) -> str:
    """Expand ~ and return abs path."""
    return os.path.abspath(os.path.expanduser(path))


def rm_line(path: str, sudo: bool = False) -> str:
    return f"{'sudo ' if sudo else ''}rm -rf {shlex.quote(path)}"


def build_specs():
    """Static junk specs (id, tier, title, subtitle, path(s), note, script)."""
    specs = []

    def add(**kw):
        specs.append(kw)

    add(id="npm", tier="safe", title="npm cache", subtitle="~/.npm",
        paths=[S("~/.npm")], note="Rebuilt automatically on the next npm install.",
        script=[rm_line(S("~/.npm/_cacache")), rm_line(S("~/.npm/_logs"))])
    add(id="yarn", tier="safe", title="Yarn cache", subtitle="~/.yarn/cache",
        paths=[S("~/.yarn/cache")], note="Rebuilt automatically on the next yarn install.",
        script=[rm_line(S("~/.yarn/cache"))])
    add(id="pnpm", tier="safe", title="pnpm store", subtitle="~/Library/pnpm + ~/.pnpm-store",
        paths=[S("~/Library/pnpm"), S("~/.pnpm-store")],
        note="Rebuilt on the next pnpm install.",
        script=[rm_line(S("~/Library/pnpm/store")), rm_line(S("~/.pnpm-store"))])
    add(id="pip", tier="safe", title="pip cache", subtitle="~/Library/Caches/pip",
        paths=[S("~/Library/Caches/pip")], note="Re-downloaded on the next pip install.",
        script=[rm_line(S("~/Library/Caches/pip"))])
    add(id="homebrew", tier="safe", title="Homebrew cache", subtitle="~/Library/Caches/Homebrew",
        paths=[S("~/Library/Caches/Homebrew")],
        note="Old downloads & bottles. `brew cleanup` also removes their metadata.",
        script=["brew cleanup -s 2>/dev/null || " + rm_line(S("~/Library/Caches/Homebrew"))])
    add(id="uv", tier="safe", title="uv cache", subtitle="~/.cache/uv",
        paths=[S("~/.cache/uv")], note="Re-downloaded by uv when needed.",
        script=[rm_line(S("~/.cache/uv"))])
    add(id="puppeteer", tier="safe", title="Puppeteer browsers", subtitle="~/.cache/puppeteer",
        paths=[S("~/.cache/puppeteer")], note="Re-downloaded by Puppeteer when needed.",
        script=[rm_line(S("~/.cache/puppeteer"))])
    add(id="cocoapods", tier="safe", title="CocoaPods cache", subtitle="~/Library/Caches/CocoaPods",
        paths=[S("~/Library/Caches/CocoaPods")], note="Re-downloaded on the next pod install.",
        script=[rm_line(S("~/Library/Caches/CocoaPods"))])
    add(id="go_build", tier="safe", title="Go build cache", subtitle="~/Library/Caches/go-build",
        paths=[S("~/Library/Caches/go-build")], note="Rebuilt on the next go build.",
        script=["go clean -cache 2>/dev/null || " + rm_line(S("~/Library/Caches/go-build"))])
    add(id="swiftpm", tier="safe", title="Swift Package Manager caches",
        subtitle="~/Library/Caches/org.swift.swiftpm",
        paths=[S("~/Library/Caches/org.swift.swiftpm")],
        note="Re-resolved on the next build.", script=[rm_line(S("~/Library/Caches/org.swift.swiftpm"))])
    add(id="gradle", tier="safe", title="Gradle caches", subtitle="~/.gradle/caches",
        paths=[S("~/.gradle/caches")], note="Gradle re-downloads dependencies on next build.",
        script=[rm_line(S("~/.gradle/caches"))])
    add(id="maven", tier="safe", title="Maven repository", subtitle="~/.m2/repository",
        paths=[S("~/.m2/repository")], note="Re-downloaded on the next mvn build.",
        script=[rm_line(S("~/.m2/repository"))])
    add(id="xcode_derived", tier="safe", title="Xcode DerivedData",
        subtitle="~/Library/Developer/Xcode/DerivedData",
        paths=[S("~/Library/Developer/Xcode/DerivedData")],
        note="Build intermediates — Xcode regenerates them.",
        script=[rm_line(S("~/Library/Developer/Xcode/DerivedData"))])
    add(id="xcode_devsupport", tier="safe", title="Xcode iOS/watchOS DeviceSupport",
        subtitle="~/Library/Developer/Xcode/* DeviceSupport",
        paths=[S("~/Library/Developer/Xcode/iOS DeviceSupport"),
               S("~/Library/Developer/Xcode/watchOS DeviceSupport"),
               S("~/Library/Developer/Xcode/tvOS DeviceSupport")],
        note="Debug symbols copied from devices — copied again when you plug the device back in.",
        script=[rm_line(S("~/Library/Developer/Xcode/iOS DeviceSupport")),
                rm_line(S("~/Library/Developer/Xcode/watchOS DeviceSupport")),
                rm_line(S("~/Library/Developer/Xcode/tvOS DeviceSupport"))])
    add(id="sim_caches", tier="safe", title="Simulator caches",
        subtitle="~/Library/Developer/CoreSimulator/Caches",
        paths=[S("~/Library/Developer/CoreSimulator/Caches")],
        note="Regenerated by the Simulator.", script=[rm_line(S("~/Library/Developer/CoreSimulator/Caches"))])
    add(id="logs", tier="safe", title="User logs", subtitle="~/Library/Logs",
        paths=[S("~/Library/Logs")], note="Old app logs & crash reports.",
        script=[rm_line(S("~/Library/Logs"))])
    add(id="trash", tier="safe", title="Trash", subtitle="~/.Trash",
        paths=[S("~/.Trash")], note="Files you already deleted — emptied instantly.",
        script=['rm -rf "$HOME/.Trash"; mkdir -p "$HOME/.Trash"'])
    add(id="python_pycache", tier="safe", title="Python __pycache__",
        subtitle="compiled bytecode across your projects", paths=[],
        note="Regenerated automatically by Python.", script=[], dynamic=True)
    add(id="build_outputs", tier="safe", title="JS/dev build output",
        subtitle=".next, .turbo, venv, .venv and friends", paths=[],
        note="Regenerated on the next build/install.", script=[], dynamic=True)
    add(id="ollama", tier="review", title="Ollama models", subtitle="~/.ollama",
        paths=[S("~/.ollama")],
        note="Local LLM weights. Prefer `ollama list` + `ollama rm <model>`; they can be pulled again.",
        script=[rm_line(S("~/.ollama/models"))])
    add(id="docker", tier="review", title="Docker disk image",
        subtitle="~/Library/Containers/com.docker.docker",
        paths=[S("~/Library/Containers/com.docker.docker")],
        note="Images, containers & volumes. Reclaim with `docker system prune -a` then shrink the disk image in Docker → Settings → Resources.",
        script=[])
    add(id="ios_backups", tier="review", title="iPhone/iPad backups",
        subtitle="~/Library/Application Support/MobileSync/Backup",
        paths=[S("~/Library/Application Support/MobileSync/Backup")],
        note="Old device backups. Delete from Finder → your device → Manage Backups.",
        script=[rm_line(S("~/Library/Application Support/MobileSync/Backup"))])
    add(id="xcode_archives", tier="review", title="Xcode Archives",
        subtitle="~/Library/Developer/Xcode/Archives",
        paths=[S("~/Library/Developer/Xcode/Archives")],
        note="Keep archives of builds you still ship; delete the rest.",
        script=[rm_line(S("~/Library/Developer/Xcode/Archives"))])
    add(id="sim_devices", tier="review", title="Simulator devices",
        subtitle="~/Library/Developer/CoreSimulator/Devices",
        paths=[S("~/Library/Developer/CoreSimulator/Devices")],
        note="`xcrun simctl delete unavailable` removes stale ones.",
        script=["xcrun simctl delete unavailable 2>/dev/null || true"])
    add(id="android", tier="review", title="Android SDK & emulators",
        subtitle="~/Library/Android + ~/.android", paths=[S("~/Library/Android"), S("~/.android")],
        note="SDK components and AVD images. Trim with Android Studio's SDK Manager / `avdmanager`.",
        script=[rm_line(S("~/.android/avd"))])
    add(id="steam", tier="review", title="Steam content",
        subtitle="~/Library/Application Support/Steam", paths=[S("~/Library/Application Support/Steam")],
        note="Installed games.", script=[])
    add(id="downloads_old", tier="review", title="Old installers & big files in Downloads",
        subtitle="~/Downloads", paths=[], note="Installers and large downloads you probably don't need anymore.",
        script=[], dynamic=True)
    add(id="node_modules", tier="review", title="node_modules & project dependencies",
        subtitle="across your projects", paths=[],
        note="Reinstall with `npm install` (or yarn/pnpm) when you return to a project.",
        script=[], dynamic=True)
    add(id="vms", tier="review", title="Virtual machines",
        subtitle="~/Virtual Machines.localized & Parallels",
        paths=[S("~/Virtual Machines.localized"), S("~/Parallels")],
        note="VM disk images. Delete unused VMs to reclaim.", script=[])
    add(id="sys_sim_caches", tier="safe", title="Simulator runtime caches (system)",
        subtitle="/Library/Developer/CoreSimulator/Caches",
        paths=[S("/Library/Developer/CoreSimulator/Caches")],
        note="Prebuilt dyld caches for simulator runtimes — Xcode regenerates them when a simulator next boots. Needs sudo.",
        script=[rm_line("/Library/Developer/CoreSimulator/Caches", sudo=True)])
    add(id="system_updates", tier="safe", title="System update leftovers",
        subtitle="/Library/Updates",
        paths=[S("/Library/Updates")],
        note="Staged macOS updates that were already applied. Needs sudo.",
        script=[rm_line("/Library/Updates", sudo=True)])
    add(id="system_assets", tier="review", title="macOS system assets (voices & offline models)",
        subtitle="/System/Volumes/Data/System/Library/AssetsV2",
        paths=[S("/System/Volumes/Data/System/Library/AssetsV2")],
        note="Siri voices, offline speech/translation models, dictionaries and the iOS Simulator runtime. Unused simulator runtimes can go with `xcrun simctl runtime delete`; unused Siri voices via System Settings → Siri & Spotlight.",
        script=[])
    return specs


# ---------------------------------------------------------------------------
# insights
# ---------------------------------------------------------------------------

def build_insights(recs: list[dict], safe_total: int, disk: dict, mem: dict,
                   snaps: list, unaccounted: int) -> list[dict]:
    """Dashboard insights derived from scan results (also reused after cleanup)."""
    insights: list[dict] = []

    def ins(sev, title, text, cmd=None):
        insights.append({"severity": sev, "title": title, "text": text, "cmd": cmd})

    pct = disk.get("percent_used", 0)
    if pct >= 90:
        ins("critical", "Your disk is nearly full",
            f"{pct}% used — only {human(disk.get('free', 0))} free. "
            f"{human(safe_total)} can be reclaimed immediately from the Safe list.")
    elif pct >= 75:
        ins("warning", "Your disk is filling up",
            f"{pct}% used — {human(disk.get('free', 0))} free. "
            f"{human(safe_total)} is safely reclaimable.")
    top_safe = sorted([r for r in recs if r["tier"] == "safe"], key=lambda r: -r["size"])[:3]
    if top_safe:
        t = top_safe[0]
        ins("info", f"Quick win: {t['title'].lower()}",
            f"{t['title']} is holding {human(t['size'])} at {t['subtitle']}. {t['note']}",
            (t["script"] or [None])[0])
    trash = next((r for r in recs if r["id"] == "trash"), None)
    if trash:
        ins("warning", "Trash not emptied",
            f"{human(trash['size'])} sitting in Trash — emptying is instant and irreversible-safe.", "rm -rf \"$HOME/.Trash\"/*")
    dl = next((r for r in recs if r["id"] == "downloads_old"), None)
    if dl:
        ins("warning", "Downloads clutter",
            f"{human(dl['size'])} of old installers & large files in ~/Downloads "
            f"({len(dl['breakdown'])}+ items).", "open \"$HOME/Downloads\"")
    nm = next((r for r in recs if r["id"] == "node_modules"), None)
    if nm:
        ins("info", "node_modules everywhere",
            f"{human(nm['size'])} across your projects. {nm['note']}", None)
    docker = next((r for r in recs if r["id"] == "docker"), None)
    if docker:
        ins("warning", "Docker is a heavyweight",
            f"Docker's disk image holds {human(docker['size'])}. Run `docker system prune -a` "
            f"and shrink it in Settings → Resources.", "docker system prune -a")
    xc = [r for r in recs if r["id"].startswith("xcode") or r["id"] in ("sim_devices", "sys_sim_caches")]
    if xc:
        xc_sum = sum(r["size"] for r in xc)
        xc_safe = sum(r["size"] for r in xc if r["tier"] == "safe")
        ins("info", "Xcode tooling",
            f"Xcode-related data is using {human(xc_sum)}; {human(xc_safe)} of it (DerivedData, "
            f"DeviceSupport, simulator caches) regenerates itself.", None)
    sa = next((r for r in recs if r["id"] == "system_assets"), None)
    if sa:
        ins("info", "macOS hides big stuff in system assets",
            f"Siri voices, offline speech/translation models and dictionaries hold {human(sa['size'])} at "
            f"{sa['subtitle']}. Trim unused Siri voices in System Settings → Siri & Spotlight.", None)
        simrt = next((b for b in sa.get("breakdown", []) if "SimulatorRuntime" in b["name"]), None)
        if simrt and simrt["size"] > 5 * 1024**3:
            ins("warning", "iOS Simulator runtime is huge",
                f"The installed iOS Simulator runtime alone holds {human(simrt['size'])}. "
                f"If you don't run the Simulator, delete it and Xcode will still build for real devices.",
                "xcrun simctl runtime list")
    if snaps:
        ins("warning", "Time Machine local snapshots",
            f"{len(snaps)} local snapshot(s) exist and can consume tens of GB invisibly.",
            "sudo tmutil deletelocalsnapshots /")
    if unaccounted > 15 * 1024**3:
        ins("info", "Not measured: " + human(unaccounted),
            "System data, other users, macOS snapshots and virtual memory live outside the scanned "
            "folders. For a full root map run: sudo du -x -d 1 /System/Volumes/Data",
            "sudo du -x -d 1 /System/Volumes/Data 2>/dev/null | sort -rn | head -30")
    mem_free = mem.get("free_percent", 0)
    if mem.get("swap_used", 0) > 2 * 1024**3 or mem_free < 15:
        ins("warning", "Memory pressure",
            f"Only {mem_free}% RAM free; swap in use: {human(mem.get('swap_used', 0))}. "
            f"Restart memory-hungry apps (browsers, Docker, Xcode).", None)
    else:
        ins("ok", "RAM is healthy",
            f"{mem_free}% free, swap {human(mem.get('swap_used', 0))} of "
            f"{human(mem.get('swap_total', 0))}. Disk, not RAM, is your constraint.", None)
    sev_order = {"critical": 0, "warning": 1, "info": 2, "ok": 3}
    insights.sort(key=lambda i: sev_order.get(i["severity"], 9))
    return insights[:10]


# ---------------------------------------------------------------------------
# main scan
# ---------------------------------------------------------------------------

def main() -> int:
    t0 = time.time()
    if "--check" in sys.argv:
        return doctor()
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        set_state("running", "Reading system info…", force=True)
        LOG_TXT.write_text("")
        log("scan started")

        info = sys_info()
        disk = disk_info()
        mem = memory_info()
        snaps = snapshots_info()
        perm = check_permissions()
        app_name = parent_app()

        # ---- shared junk / detail config ----------------------------------
        specs = build_specs()

        junk_paths: set[str] = set()
        for sp in specs:
            for p in sp["paths"]:
                junk_paths.add(os.path.normpath(p))
        junk_paths.update({
            os.path.normpath("/Library/Caches"), os.path.normpath("/Library/Logs"),
        })

        detail = {
            os.path.abspath(HOME),
            S("~/Library/Caches"), S("~/.cache"), S("~/.gradle"), S("~/.npm"),
            S("~/.yarn"), S("~/.m2"), S("~/Library/Developer"),
            S("~/Library/Developer/Xcode"), S("~/Library/Developer/Xcode/DerivedData"),
            S("~/Library/Developer/Xcode/iOS DeviceSupport"),
            S("~/Library/Developer/CoreSimulator"), S("~/Library/Developer/CoreSimulator/Devices"),
            S("~/Library/Android"), S("~/.android"), S("~/.ollama"),
            S("~/Library/Containers/com.docker.docker"),
            S("~/Library/Application Support/MobileSync"),
            S("~/.Trash"), S("~/Virtual Machines.localized"),
            S("~/Library/Logs"), S("~/Library/Application Support"),
            S("~/Library/Containers"), S("~/Library/Group Containers"),
            S("~/Library/Mail"), S("~/Pictures"), S("~/Downloads"),
            S("~/Desktop"), S("~/Documents"), S("~/Movies"), S("~/Music"),
            os.path.normpath("/Library/Developer"),
            os.path.normpath("/Library/Developer/CoreSimulator"),
            os.path.normpath("/Library/Developer/CoreSimulator/Caches"),
            os.path.normpath("/System/Volumes/Data/System"),
            os.path.normpath("/System/Volumes/Data/System/Library"),
            os.path.normpath("/System/Volumes/Data/System/Library/AssetsV2"),
            os.path.normpath("/private/var/db"), os.path.normpath("/private/var/vm"),
        }
        import glob as _glob
        for g in _glob.glob(S("~/iCloud Drive (Archive)*")):
            detail.add(g)

        # ---- outside-home walks in parallel -------------------------------
        outside_targets = [
            ("/Applications", "apps"),
            ("/Library", "system"),
            ("/opt/homebrew", "devtools"),
            ("/usr/local", "devtools"),
            ("/private/var", "system"),
            ("/System/Volumes/Data/System", "system"),
        ]
        outside: dict[str, dict] = {}
        outside_junk: dict[str, int] = {}
        outside_child_sums: dict[str, list] = {}
        outside_denied: set[str] = set()

        def walk_outside(pair):
            name, cat = pair
            p = Path(name)
            if not p.exists():
                return None
            root = os.path.abspath(str(p))
            w = Walker(str(p), detail=detail | {root}, junk=junk_paths)
            size = w.walk()
            kids = [{"name": n, "size": s} for n, s, _ in w.child_sums.get(root, [])]
            kids.sort(key=lambda k: -k["size"])
            payload = {"path": name, "size": size, "category": cat,
                       "children": [k for k in kids if k["size"] >= MIN_TREE][:24],
                       "errors": w.errors, "files": w.files}
            return name, payload, w.junk_sizes, w.child_sums, w.denied_paths

        set_state(message="Mapping applications & system folders…")
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            for res in ex.map(walk_outside, outside_targets):
                if res:
                    name, payload, js, cs, denied = res
                    outside[name] = payload
                    outside_junk.update(js)
                    outside_child_sums.update(cs)
                    outside_denied.update(denied)
                    log(f"outside {name}: {human(payload['size'])}")

        # ---- home walk -----------------------------------------------------
        set_state(message="Mapping your home folder… (the big one)")

        tree: dict[str, tuple[int, int]] = {}
        builds: list = []
        bigfiles: list = []
        downloads: list = []
        markers: set = set()
        pycache: list = []

        last = [0.0]

        walker = Walker(str(HOME), tree=tree, detail=detail, junk=junk_paths,
                        builds=builds, bigfiles=bigfiles, downloads=downloads,
                        markers=markers, pycache=pycache)

        def progress2():
            now = time.time()
            if now - last[0] < 1.2:
                return
            last[0] = now
            set_state(message=f"Mapping your home folder… {human(walker._live_bytes)} · "
                              f"{walker.files:,} files")

        walker._progress = progress2
        home_size = walker.walk()
        log(f"home: {human(home_size)} in {time.time()-t0:.0f}s ({walker.files:,} files, {walker.errors} errors)")

        # which top-level home folders were blocked by macOS privacy protection?
        walk_denied_tops: dict[str, int] = {}
        for p in walker.denied_paths:
            rel = os.path.relpath(p, HOME)
            if rel.startswith(".."):
                continue
            top = rel.split(os.sep)[0]
            walk_denied_tops[top] = walk_denied_tops.get(top, 0) + 1
        sys_denied_count = len(outside_denied)

        # ---- assemble recommendations --------------------------------------
        set_state(message="Analyzing reclaimable space…")
        recs: list[dict] = []

        def is_project_junk(p: str) -> bool:
            """Build artifacts only count as 'project junk' outside hidden tool dirs / Library."""
            try:
                rel = os.path.relpath(p, HOME)
            except ValueError:
                return False
            if rel.startswith(".."):
                return False
            seg = rel.split(os.sep)
            if not seg or seg[0].startswith("."):
                return False
            if seg[0] in ("Library", "Applications", "Public"):
                return False
            return True

        def nice_name(name: str) -> str:
            table = {
                "ms-playwright": "Playwright browsers", "electron": "Electron builds",
                "typescript": "TypeScript server", "node-gyp": "node-gyp builds",
                "Homebrew": "Homebrew cache", "pip": "pip cache",
                "CocoaPods": "CocoaPods cache", "go-build": "Go build cache",
                "org.swift.swiftpm": "SwiftPM cache", "huggingface": "HuggingFace models",
                "torch": "PyTorch models", "uv": "uv cache", "puppeteer": "Puppeteer browsers",
                "JetBrains": "JetBrains caches", "Google": "Google software caches",
                "cloudkit": "iCloud CloudKit cache", "com.apple.python": "Python caches",
            }
            if name in table:
                return table[name]
            if name.startswith("com.apple."):
                return "Apple system cache"
            if name.startswith("org."):
                return name + " caches"
            return name + " cache" if not name.lower().endswith("cache") else name

        covered_names: set[str] = set()
        for sp in specs:
            for p in sp["paths"]:
                covered_names.add(os.path.normpath(p))

        for sp in specs:
            size = 0
            breakdown = []
            sources = []
            source_sizes = {}
            for p in sp["paths"]:
                np = os.path.normpath(p)
                sz = walker.junk_sizes.get(np)
                if sz is None:
                    sz = outside_junk.get(np)
                if sz is None and os.path.exists(np):
                    # path might be a file
                    try:
                        st = os.stat(np)
                        sz = st.st_blocks * _iot if st.st_blocks > 0 else st.st_size
                    except OSError:
                        sz = None
                if not sz:
                    continue
                size += sz
                sources.append(p)
                source_sizes[p] = sz
                kids = walker.child_sums.get(np) or outside_child_sums.get(np)
                if kids:
                    kids = sorted(kids, key=lambda k: -k[1])
                    breakdown.extend({"name": n, "size": s} for n, s, _ in kids[:12] if s >= 50 * 1024 * 1024)
                elif sz:
                    breakdown.append({"name": os.path.basename(np), "size": sz})
            if sp["id"] == "python_pycache":
                size = sum(p["size"] for p in pycache)
                agg: dict[str, int] = {}
                for p in pycache:
                    parts = os.path.relpath(p["path"], HOME).split(os.sep)
                    key = os.sep.join(parts[:3])
                    agg[key] = agg.get(key, 0) + p["size"]
                breakdown = sorted(({"name": k, "size": v} for k, v in agg.items()),
                                   key=lambda x: -x["size"])[:12]
                sp["script"] = ['find "$HOME" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true']
                sources = []
            elif sp["id"] == "build_outputs":
                by = {}
                for b in builds:
                    if b["name"] != "node_modules" and is_project_junk(b["path"]):
                        by.setdefault(b["name"], []).append(b)
                size = sum(b["size"] for name, lst in by.items() for b in lst)
                breakdown = sorted(
                    ({"name": f"{name} · {os.path.relpath(lst[0]['path'], HOME).split(os.sep)[0]}",
                      "size": sum(b['size'] for b in lst)} for name, lst in by.items()),
                    key=lambda x: -x["size"])[:12]

                def rml(p):
                    return rm_line(p)
                lines = []
                for name, lst in sorted(by.items(), key=lambda kv: -sum(b["size"] for b in kv[1])):
                    for b in sorted(lst, key=lambda b: -b["size"])[:8]:
                        lines.append(rml(b["path"]))
                sp["script"] = lines[:30]
                sources = [b["path"] for name, lst in by.items() for b in lst][:60]
                source_sizes = {b["path"]: b["size"] for name, lst in by.items() for b in lst}
            elif sp["id"] == "downloads_old":
                cutoff = time.time() - 120 * 86400
                cand = [d for d in downloads
                        if (d["mtime"] < cutoff and d["size"] >= 200 * 1024 * 1024)
                        or os.path.splitext(d["path"])[1].lower() in (".dmg", ".pkg", ".iso", ".xip", ".zip")]
                cand.sort(key=lambda d: -d["size"])
                cand = cand[:40]
                size = sum(d["size"] for d in cand)
                breakdown = [{"name": os.path.basename(d["path"]), "size": d["size"],
                              "age_days": int((time.time() - d["mtime"]) / 86400)}
                             for d in cand[:12]]
                sp["script"] = [rm_line(d["path"]) for d in cand[:25]]
                sources = [d["path"] for d in cand]
                source_sizes = {d["path"]: d["size"] for d in cand}
            elif sp["id"] == "node_modules":
                nms = [b for b in builds if b["name"] == "node_modules" and is_project_junk(b["path"])]
                nms.sort(key=lambda b: -b["size"])
                size = sum(b["size"] for b in nms)
                now = time.time()
                breakdown = [{"name": os.path.relpath(b["path"], HOME).replace("/node_modules", ""),
                              "size": b["size"],
                              "age_days": int((now - b["mtime"]) / 86400) if b["mtime"] else None}
                             for b in nms[:15]]
                sp["script"] = [rm_line(b["path"]) for b in nms[:25]]
                sources = [b["path"] for b in nms]
                source_sizes = {b["path"]: b["size"] for b in nms}
                idle = [b for b in nms if b["mtime"] and (now - b["mtime"]) > 120 * 86400]
                if idle:
                    sp["note"] = (f"{len(nms)} projects · {human(sum(b['size'] for b in idle))} "
                                  f"of it idle for 4+ months.")
            elif sp["id"] == "vms":
                kids = []
                for p in sp["paths"]:
                    for c in walker.child_sums.get(os.path.normpath(p), []):
                        kids.append({"name": c[0], "size": c[1]})
                if kids:
                    breakdown = sorted(kids, key=lambda k: -k["size"])[:12]
            if size >= MIN_REC:
                deletable = bool(sources) and all(is_deletable_path(p) for p in sources)
                recs.append({
                    "id": sp["id"], "tier": sp["tier"], "title": sp["title"],
                    "subtitle": sp["subtitle"], "size": size, "note": sp["note"],
                    "script": sp.get("script", []), "sources": sources,
                    "source_sizes": source_sizes, "breakdown": breakdown,
                    "deletable": deletable,
                })

        # dynamic cache recommendations ------------------------------------
        def dynamic_caches(parent: str, exclude_ids: set[str], min_size: int, cap: int,
                           title_for, id_prefix: str, note_for=lambda n: None):
            parent = os.path.normpath(parent)
            kids = walker.child_sums.get(parent, [])
            out = []
            for name, sz, _ in sorted(kids, key=lambda k: -k[1]):
                p = os.path.join(parent, name)
                if os.path.normpath(p) in covered_names or sz < min_size:
                    continue
                out.append({"name": name, "path": p, "size": sz})
            items = []
            for i, k in enumerate(out[:cap]):
                items.append({
                    "id": f"{id_prefix}-{k['name']}", "tier": "safe",
                    "title": title_for(k["name"]), "subtitle": "~/" + os.path.relpath(k["path"], HOME),
                    "size": k["size"],
                    "note": note_for(k["name"]) or "Regenerated automatically when needed.",
                    "script": [rm_line(k["path"])], "sources": [k["path"]],
                    "source_sizes": {k["path"]: k["size"]}, "breakdown": [],
                    "deletable": is_deletable_path(k["path"]),
                })
            rest = out[cap:]
            if rest and sum(k["size"] for k in rest) >= MIN_REC:
                items.append({
                    "id": f"{id_prefix}-rest", "tier": "safe",
                    "title": "Other caches", "subtitle": "~/" + os.path.relpath(parent, HOME),
                    "size": sum(k["size"] for k in rest),
                    "note": "Smaller caches bundled together.",
                    "script": [rm_line(k["path"]) for k in rest[:20]],
                    "sources": [k["path"] for k in rest],
                    "source_sizes": {k["path"]: k["size"] for k in rest},
                    "breakdown": [{"name": k["name"], "size": k["size"]} for k in rest[:12]],
                    "deletable": all(is_deletable_path(k["path"]) for k in rest),
                })
            return items

        recs.extend(dynamic_caches(S("~/Library/Caches"), set(), 250 * 1024 * 1024, 12,
                                   nice_name, "libcache",
                                   lambda n: "Safe macOS/app cache." if n.startswith("com.apple.") else None))
        recs.extend(dynamic_caches(S("~/.cache"), set(), 150 * 1024 * 1024, 8,
                                   nice_name, "cache"))

        # system caches (/Library/Caches, /Library/Logs) --------------------
        sys_caches = sum(outside_junk.get(os.path.normpath(p), 0)
                         for p in ("/Library/Caches", "/Library/Logs"))
        if sys_caches >= MIN_REC:
            recs.append({"id": "system_caches", "tier": "review",
                         "title": "System caches & logs (needs sudo)", "subtitle": "/Library/Caches, /Library/Logs",
                         "size": sys_caches, "note": "Shared across all users. Deleting needs sudo.",
                         "script": ["sudo rm -rf /Library/Caches/*", "sudo rm -rf /Library/Logs/*"],
                         "sources": ["/Library/Caches", "/Library/Logs"],
                         "source_sizes": {p: outside_junk.get(os.path.normpath(p), 0)
                                          for p in ("/Library/Caches", "/Library/Logs")},
                         "breakdown": [], "deletable": False})

        # ---- totals, attribution ------------------------------------------
        safe_total = sum(r["size"] for r in recs if r["tier"] == "safe")
        review_total = sum(r["size"] for r in recs if r["tier"] == "review")

        # aggregate junk per top-level child (per-source sizes when known)
        junk_by_top: dict[str, int] = {}
        safe_by_top: dict[str, int] = {}
        for r in recs:
            if not r["sources"]:
                continue
            ss = r.get("source_sizes") or {}
            fallback = r["size"] / len(r["sources"])
            for p in r["sources"]:
                rel = os.path.relpath(p, HOME)
                if rel.startswith(".."):
                    continue
                top = rel.split(os.sep)[0]
                amount = ss.get(p, fallback)
                junk_by_top[top] = junk_by_top.get(top, 0) + amount
                if r["tier"] == "safe":
                    safe_by_top[top] = safe_by_top.get(top, 0) + amount

        # ---- home tree for JSON -------------------------------------------
        home_children = []
        by_rel = {rel: v for rel, v in tree.items() if "\x00" not in rel}

        def cat_for(rel: str, is_dir: bool = True) -> str:
            top = rel.split(os.sep)[0]
            name_map = {
                "Desktop": "docs", "Documents": "docs", "Downloads": "docs",
                "Pictures": "media", "Movies": "media", "Music": "media",
                "Library": "devtools", "Applications": "apps", "Public": "other",
                ".npm": "caches", ".yarn": "caches", ".gradle": "caches", ".m2": "caches",
                ".cache": "caches", ".pnpm-store": "caches", ".cargo": "caches", "go": "caches",
                ".ollama": "ai", ".android": "sdk", "Android": "sdk",
                ".docker": "devtools", ".vscode": "devtools", ".cursor": "devtools",
                ".config": "devtools", ".local": "devtools", ".ssh": "devtools",
                "Virtual Machines.localized": "devtools", "Parallels": "devtools",
            }
            first = rel.split(os.sep)[0]
            cat = name_map.get(first)
            if cat is None:
                if os.path.join(HOME, first) in markers:
                    cat = "code"
                elif first.startswith("."):
                    cat = "devtools"
                else:
                    cat = "other"
            if rel.startswith("Library" + os.sep):
                seg2 = rel.split(os.sep)[1]
                if seg2 in ("Developer", "Caches", "Containers", "pnpm"):
                    cat = "caches" if seg2 in ("Developer", "Caches") else "devtools"
                if seg2 == "Android":
                    cat = "sdk"
            return cat

        top_entries = [(rel, v) for rel, v in by_rel.items() if os.sep not in rel]

        def kids_of(node_path: str, fallback_rel: str, min_size: int) -> list:
            """Children of a directory: prefer walker.child_sums (exact, deeper), else tree."""
            found: dict[str, dict] = {}
            np_ = os.path.normpath(node_path)
            for name, sz, _mt in walker.child_sums.get(np_, []):
                p = os.path.join(np_, name)
                found[p] = {"name": name, "path": p, "size": sz, "children": []}
            for krel, (kd, ksize) in by_rel.items():
                if krel.startswith(fallback_rel + os.sep) and os.sep not in krel[len(fallback_rel) + 1:]:
                    p = os.path.join(HOME, krel)
                    if p not in found:
                        found[p] = {"name": krel.split(os.sep)[-1], "path": p, "size": ksize, "children": []}
            return [v for v in sorted(found.values(), key=lambda x: -x["size"]) if v["size"] >= min_size][:32]

        for rel, (depth, size) in sorted(top_entries, key=lambda kv: -kv[1][1]):
            path = os.path.join(HOME, rel)
            node = {"name": rel, "path": path, "size": size, "category": cat_for(rel),
                    "junk": int(junk_by_top.get(rel, 0)), "safe_junk": int(safe_by_top.get(rel, 0)),
                    "children": []}
            minsz = MIN_TREE if size > 2 * 1024**3 else 50 * 1024**2
            for child in kids_of(path, rel, minsz):
                child["category"] = cat_for(os.path.relpath(child["path"], HOME))
                child["junk"] = int(junk_by_top.get(os.path.relpath(child["path"], HOME), 0))
                # one more level for Library children with detail data
                cmin = max(120 * 1024**2, minsz / 2)
                child["children"] = kids_of(child["path"], os.path.relpath(child["path"], HOME), cmin)
                for gc in child["children"]:
                    gc["category"] = cat_for(os.path.relpath(gc["path"], HOME))
                    gc["junk"] = int(junk_by_top.get(os.path.relpath(gc["path"], HOME), 0))
                if len(child["children"]) == 1:
                    child["children"] = []
                node["children"].append(child)
            home_children.append(node)

        # ---- cleanup treemap nodes ----------------------------------------
        cleanup_nodes: dict[str, dict] = {}

        def node_key(path: str) -> tuple[str, str]:
            try:
                rel = os.path.relpath(path, HOME)
            except ValueError:
                return path, path
            if rel.startswith(".."):
                return path, path
            parts = rel.split(os.sep)
            if parts[0] == "Library" and len(parts) > 1:
                key = os.sep.join(parts[:2])
            elif parts[0] == "Library":
                key = "Library"
            else:
                key = parts[0]
            return key, "~/" + key

        for r in recs:
            ss = r.get("source_sizes") or {}
            fallback = (r["size"] / len(r["sources"])) if r["sources"] else 0
            if r["sources"]:
                for p in r["sources"]:
                    key, label = node_key(p)
                    node = cleanup_nodes.setdefault(key, {"name": key, "label": label,
                                                          "size": 0.0, "safe": 0.0, "review": 0.0,
                                                          "items": []})
                    amount = ss.get(p, fallback)
                    node["size"] += amount
                    node[r["tier"]] += amount
                    if r["id"] not in node["items"]:
                        node["items"].append(r["id"])
            elif r["id"] in ("python_pycache", "build_outputs"):
                node = cleanup_nodes.setdefault("projects", {"name": "projects",
                                                             "label": "build output inside your projects",
                                                             "size": 0.0, "safe": 0.0, "review": 0.0,
                                                             "items": []})
                node["size"] += r["size"]
                node[r["tier"]] += r["size"]
                if r["id"] not in node["items"]:
                    node["items"].append(r["id"])
            else:
                key, label = node_key(r["subtitle"] or "items")
                node = cleanup_nodes.setdefault(key, {"name": key, "label": label,
                                                      "size": 0.0, "safe": 0.0, "review": 0.0,
                                                      "items": []})
                node["size"] += r["size"]
                node[r["tier"]] += r["size"]
                if r["id"] not in node["items"]:
                    node["items"].append(r["id"])

        for n in cleanup_nodes.values():
            n["size"] = int(n["size"])
            n["safe"] = int(n["safe"])
            n["review"] = int(n["review"])
            n["tier"] = "safe" if n["safe"] >= n["review"] else "review"

        # ---- outside listing ----------------------------------------------
        outside_list = []
        for name, o in outside.items():
            outside_list.append({"name": name, "size": o["size"],
                                 "children": [{"name": c["name"], "size": c["size"]}
                                              for c in o["children"][:14]]})
        outside_list = [o for o in outside_list if o["size"] >= 100 * 1024 * 1024]

        measured = home_size + sum(o["size"] for o in outside_list)
        unaccounted = max(0, disk.get("used", 0) - measured)

        # ---- big files ------------------------------------------------------
        bigfiles.sort(key=lambda b: -b["size"])
        seen_paths = set()
        bigtop = []
        for b in bigfiles:
            if b["path"] in seen_paths:
                continue
            seen_paths.add(b["path"])
            bigtop.append(b)
            if len(bigtop) >= 22:
                break

        # ---- insights --------------------------------------------------------
        insights = build_insights(recs, safe_total, disk, mem, snaps, unaccounted)

        data = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "scanner_version": VERSION,
            "scan_seconds": round(time.time() - t0, 1),
            "host": info,
            "disk": disk,
            "memory": mem,
            "snapshots": snaps,
            "permissions": {
                "denied": perm["denied"],
                "walk_denied_tops": walk_denied_tops,
                "system_denied_count": sys_denied_count,
                "parent_app": app_name,
                "full_disk_access_likely": (not perm["denied"]) and (not walk_denied_tops),
            },
            "home": {"path": str(HOME), "total": home_size, "children": home_children},
            "outside": outside_list,
            "unaccounted": unaccounted,
            "recommendations": {"safe": [r for r in recs if r["tier"] == "safe"],
                                "review": [r for r in recs if r["tier"] == "review"]},
            "totals": {"safe": safe_total, "review": review_total},
            "cleanup_nodes": sorted(cleanup_nodes.values(), key=lambda n: -n["size"]),
            "big_files": [{"path": b["path"], "size": b["size"], "mtime": b["mtime"]}
                          for b in bigtop],
            "insights": insights,
            "stats": {"home_files": walker.files, "home_errors": walker.errors,
                      "rec_count": len(recs), "dashboard_dir": str(HERE)},
        }
        tmp = DATA_JSON.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(DATA_JSON)
        log(f"done in {time.time()-t0:.0f}s · safe {human(safe_total)} · review {human(review_total)} "
            f"· unaccounted {human(unaccounted)}")
        set_state("done", f"Scan finished in {round(time.time()-t0)}s. "
                          f"{human(safe_total)} safe · {human(review_total)} review.", force=True)
        return 0
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        log("ERROR: " + tb)
        set_state("error", f"Scan failed: {exc}", error=tb.splitlines()[-3] if tb else str(exc), force=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
