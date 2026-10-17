#!/usr/bin/env python3
"""Sandbox integration test for the dashboard's cleanup engine + instant patch.

Runs the real server.py in a throwaway directory against fake recommendations
and fake junk paths, then checks:
  - security guards (Host/Origin/input validation)
  - deletion safety (protected paths, traversal, sudo skips, unknowns)
  - data.json is patched in place after deletion (no full rescan)

Usage:  python3 tests/cleanup_test.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8901
BASE = f"http://127.0.0.1:{PORT}"
fails = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        fails.append(name)


def req(path, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method,
                               headers=headers or {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}


def main():
    T = tempfile.mkdtemp(prefix="msd-cleanup-test-")
    OUT = tempfile.mkdtemp(prefix="msd-outside-test-")  # outside the fake HOME
    os.makedirs(T + "/junk/npm/sub", exist_ok=True)
    os.makedirs(T + "/junk/npm2", exist_ok=True)
    os.makedirs(T + "/junk/review", exist_ok=True)
    os.makedirs(T + "/important", exist_ok=True)
    os.makedirs(OUT + "/sub", exist_ok=True)
    with open(T + "/junk/npm/a.bin", "wb") as f:
        f.write(os.urandom(100_000))
    with open(T + "/junk/npm/sub/c.bin", "wb") as f:
        f.write(os.urandom(50_000))
    with open(T + "/junk/npm2/d.bin", "wb") as f:
        f.write(os.urandom(30_000))
    with open(T + "/junk/review/big.bin", "wb") as f:
        f.write(os.urandom(1_000_000))
    with open(T + "/important/keep.txt", "w") as f:
        f.write("keep me")
    with open(OUT + "/sub/out.bin", "wb") as f:
        f.write(os.urandom(40_000))

    shutil.copy(PROJ + "/mac_storage_dashboard/server.py", T + "/server.py")
    # stub scanner: inert on import (the server imports it to rebuild insights),
    # only touches a marker when actually executed as a rescan
    with open(T + "/scanner.py", "w") as f:
        f.write(
            "import pathlib\n"
            "def human(n): return f'{n} B'\n"
            "def disk_info(): return {'total': 1000000, 'used': 500000, 'free': 500000, 'percent_used': 50}\n"
            "def build_insights(recs, safe_total, disk, mem, snaps, unaccounted):\n"
            "    return [{'severity': 'ok', 'title': 'stub insight', 'text': f'safe={safe_total}', 'cmd': None}]\n"
            "if __name__ == '__main__':\n"
            "    pathlib.Path('rescan_marker').write_text('rescanned')\n"
            "    pathlib.Path('scan_state.json').write_text('{\"status\":\"done\",\"message\":\"done\"}')\n"
        )

    data = {
        "generated_at": "2026-10-15T00:00:00+00:00",
        "scan_seconds": 1.0,
        "disk": {"total": 1000000, "used": 800000, "free": 200000, "percent_used": 80},
        "memory": {"free_percent": 50, "swap_used": 0, "swap_total": 0},
        "snapshots": [],
        "unaccounted": 0,
        "home": {"path": T, "total": 3_150_000, "children": [
            {"name": "junk", "path": T + "/junk", "size": 3_150_000, "category": "other",
             "junk": 1_150_000, "safe_junk": 150_000, "children": []},
        ]},
        "outside": [{"name": OUT, "size": 40_000, "children": [{"name": "sub", "size": 40_000}]}],
        "recommendations": {"safe": [
            {"id": "fake_npm", "tier": "safe", "title": "npm cache", "subtitle": "junk/npm",
             "size": 150_000, "sources": [T + "/junk/npm"], "source_sizes": {T + "/junk/npm": 150_000},
             "deletable": True},
            {"id": "fake_partial", "tier": "safe", "title": "partial", "subtitle": "junk/npm2",
             "size": 30_001, "sources": [T + "/junk/npm2", "/Library/Updates"],
             "source_sizes": {T + "/junk/npm2": 30_000, "/Library/Updates": 1}, "deletable": True},
            {"id": "fake_sys", "tier": "safe", "title": "System updates", "subtitle": "/Library/Updates",
             "size": 123, "sources": ["/Library/Updates"], "source_sizes": {"/Library/Updates": 123},
             "deletable": False},
            {"id": "fake_root", "tier": "safe", "title": "evil", "subtitle": "/",
             "size": 1, "sources": ["/"], "source_sizes": {"/": 1}, "deletable": False},
            {"id": "fake_traversal", "tier": "safe", "title": "traversal", "subtitle": "..",
             "size": 1, "sources": [T + "/junk/../important"], "source_sizes": {}, "deletable": False},
        ], "review": [
            {"id": "fake_review", "tier": "review", "title": "Backups", "subtitle": "junk/review",
             "size": 1_000_000, "sources": [T + "/junk/review"],
             "source_sizes": {T + "/junk/review": 1_000_000}, "deletable": True},
            {"id": "fake_outside", "tier": "review", "title": "Outside junk", "subtitle": OUT,
             "size": 40_000, "sources": [OUT + "/sub"], "source_sizes": {OUT + "/sub": 40_000},
             "deletable": True},
        ]},
        "totals": {"safe": 150_000 + 30_001 + 123 + 1 + 1, "review": 1_040_000},
        "cleanup_nodes": [
            {"name": "junk", "label": "~/junk", "size": 1_150_000, "safe": 150_000,
             "review": 1_000_000, "items": ["fake_npm", "fake_review"], "tier": "review"},
        ],
        "big_files": [{"path": T + "/junk/npm/a.bin", "size": 100_000, "mtime": 0}],
        "insights": [{"severity": "ok", "title": "old", "text": "old", "cmd": None}],
        "stats": {"home_files": 5, "home_errors": 0, "rec_count": 7, "dashboard_dir": T},
        "permissions": {},
    }
    with open(T + "/data.json", "w") as f:
        json.dump(data, f)

    env = {**os.environ, "HOME": T}  # the fake home makes the tree patch testable
    proc = subprocess.Popen([sys.executable, "server.py", str(PORT)], cwd=T, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                if req("/api/status")[0] == 200:
                    break
            except Exception:
                time.sleep(0.25)
        else:
            check("server starts", False)
            return

        # --- security guards -------------------------------------------------
        st, _ = req("/api/status", headers={"Host": "evil.com"})
        check("Host guard blocks evil.com", st == 403, st)
        st, _ = req("/api/cleanup", "POST", {"ids": ["fake_npm"]},
                    {"Content-Type": "application/json", "Origin": "http://evil.com"})
        check("Origin guard blocks cross-origin POST", st == 403, st)
        st, _ = req("/api/cleanup", "POST", {"ids": "not-a-list"})
        check("non-list ids rejected (400)", st == 400, st)
        st, _ = req("/api/cleanup", "POST", {"ids": []})
        check("empty ids rejected (400)", st == 400, st)

        # --- deletion --------------------------------------------------------
        st, j = req("/api/cleanup", "POST",
                    {"ids": ["fake_npm", "fake_review", "fake_partial", "fake_outside",
                             "fake_sys", "fake_root", "fake_traversal", "nope_unknown"]})
        check("cleanup accepted", st == 200 and j.get("started") is True, j)

        s = {}
        for _ in range(80):
            _, s = req("/api/cleanup/status")
            if not s.get("running"):
                break
            time.sleep(0.2)

        check("npm dir deleted", not os.path.exists(T + "/junk/npm"))
        check("npm2 dir deleted", not os.path.exists(T + "/junk/npm2"))
        check("review dir deleted", not os.path.exists(T + "/junk/review"))
        check("outside sub dir deleted", not os.path.exists(OUT + "/sub"))
        check("important/keep.txt untouched", os.path.exists(T + "/important/keep.txt"))
        check("System /Library/Updates untouched (never deleted)",
              not any(r.get("path") == "/Library/Updates" for r in s.get("results", [])),
              s.get("results"))
        check("4 paths deleted ok", len([r for r in s.get("results", []) if r.get("ok")]) == 4, s.get("results"))
        check("freed > 0 bytes", s.get("freed", 0) > 0, s.get("freed"))
        sk = {x.get("id"): x.get("reason") for x in s.get("skipped", [])}
        check("fake_sys skipped (sudo)", "sudo" in (sk.get("fake_sys") or ""), sk)
        check("fake_root skipped (protected)", "protected" in (sk.get("fake_root") or ""), sk)
        check("unknown id skipped", sk.get("nope_unknown") == "unknown item", sk)

        # --- instant patch instead of a full rescan ---------------------------
        check("no rescan was started (marker absent)", not os.path.exists(T + "/rescan_marker"))
        check("cleanup status says patched", s.get("patched") is True, s.get("patched"))

        d = json.load(open(T + "/data.json"))
        rec_ids = {r["id"] for t in ("safe", "review") for r in d["recommendations"][t]}
        check("deleted safe item gone", "fake_npm" not in rec_ids)
        check("deleted review item gone", "fake_review" not in rec_ids)
        check("deleted outside item gone", "fake_outside" not in rec_ids)
        check("partially deleted item kept with remaining source",
              "fake_partial" in rec_ids and
              next(r for r in d["recommendations"]["safe"] if r["id"] == "fake_partial")["size"] == 1)
        check("skipped items kept", {"fake_sys", "fake_root", "fake_traversal"} <= rec_ids)

        check("totals.review zeroed", d["totals"]["review"] == 0, d["totals"])
        check("totals.safe reduced to leftovers", d["totals"]["safe"] == 126, d["totals"])
        check("cleanup treemap node dropped when empty", d["cleanup_nodes"] == [], d["cleanup_nodes"])
        check("rec_count updated", d["stats"]["rec_count"] == 4, d["stats"])

        freed_home = sum(r["freed"] for r in s["results"] if r.get("ok") and T in r.get("path", ""))
        junk_node = d["home"]["children"][0]
        check("home total reduced by freed bytes(in home)", d["home"]["total"] == 3_150_000 - freed_home,
              (d["home"]["total"], freed_home))
        check("home node size reduced", junk_node["size"] == 3_150_000 - freed_home, junk_node["size"])
        check("home node junk hatching cleared", junk_node["junk"] == 0 and junk_node["safe_junk"] == 0,
              (junk_node["junk"], junk_node["safe_junk"]))

        freed_out = sum(r["freed"] for r in s["results"] if r.get("ok") and OUT in r.get("path", ""))
        check("outside entry reduced", d["outside"][0]["size"] == max(0, 40_000 - freed_out), d["outside"])
        check("outside child reduced", d["outside"][0]["children"][0]["size"] == max(0, 40_000 - freed_out),
              d["outside"][0]["children"])
        check("deleted big file removed", d["big_files"] == [], d["big_files"])

        check("disk refreshed from scanner", d["disk"] == {"total": 1000000, "used": 500000,
                                                           "free": 500000, "percent_used": 50}, d["disk"])
        check("insights rebuilt via scanner", d["insights"] == [{"severity": "ok", "title": "stub insight",
                                                                 "text": "safe=126", "cmd": None}], d["insights"])
        check("patched_at written", isinstance(d.get("patched_at"), str) and d.get("patched_freed", 0) > 0,
              (d.get("patched_at"), d.get("patched_freed")))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
        shutil.rmtree(T, ignore_errors=True)
        shutil.rmtree(OUT, ignore_errors=True)

    print("\n" + ("ALL PASSED" if not fails else f"{len(fails)} FAILURES: {fails}"))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
