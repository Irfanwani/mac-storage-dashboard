#!/usr/bin/env python3
"""
Mac Storage Dashboard — local server.

Serves the dashboard and exposes rescan + delete APIs. Binds to localhost only.

Usage:  python3 server.py [port]
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

try:  # scanner.py sits next to us; reused to rebuild insights after cleanup
    import scanner as _scanner
except Exception:  # pragma: no cover — the server still works without it
    _scanner = None

HERE = Path(__file__).resolve().parent
DATA_JSON = HERE / "data.json"
STATE_JSON = HERE / "scan_state.json"
SCANNER = HERE / "scanner.py"

HOME = os.path.expanduser("~")

_scan_proc: subprocess.Popen | None = None
_proc_lock = threading.Lock()

_cleanup_lock = threading.Lock()
_cleanup_state: dict = {"running": False, "started_at": None, "finished_at": None,
                        "planned": 0, "done": 0, "freed": 0, "results": [],
                        "skipped": [], "error": None, "patched": False,
                        "scan_started": False}

# paths we refuse to delete, ever
PROTECTED_EXACT = {
    "/", "/Users", "/Applications", "/Library", "/System", "/usr", "/etc", "/var",
    "/private", "/opt", "/bin", "/sbin", "/tmp", "/private/tmp", "/private/var",
    HOME, str(HERE),
}


def read_state() -> dict:
    try:
        return json.loads(STATE_JSON.read_text())
    except Exception:
        return {"status": "idle", "message": ""}


def scanner_running() -> bool:
    with _proc_lock:
        if _scan_proc is not None and _scan_proc.poll() is None:
            return True
    st = read_state()
    return st.get("status") == "running" and (
        time.time() - (DATA_JSON.stat().st_mtime if DATA_JSON.exists() else 0) < 24 * 3600
    ) and _process_alive_from_state()


def _process_alive_from_state() -> bool:
    """Heuristic: state says running and was touched in the last 60s."""
    try:
        age = time.time() - STATE_JSON.stat().st_mtime
        return age < 120
    except OSError:
        return False


def start_scan() -> bool:
    global _scan_proc
    with _proc_lock:
        if _scan_proc is not None and _scan_proc.poll() is None:
            return False
        _scan_proc = subprocess.Popen(
            [sys.executable, str(SCANNER)],
            cwd=str(HERE),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True


# ---------------------------------------------------------------------------
# cleanup engine (in-app deletion)
# ---------------------------------------------------------------------------

def dir_size(path: str) -> int:
    """Fast allocated-size walk (hardlink-deduped)."""
    total = 0
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if os.path.islink(path):
        return 0
    if os.path.isfile(path):
        return st.st_blocks * 512 if st.st_blocks > 0 else st.st_size
    seen: set = set()
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            est = e.stat(follow_symlinks=False)
                            if est.st_nlink > 1:
                                key = (est.st_dev, est.st_ino)
                                if key in seen:
                                    continue
                                seen.add(key)
                            total += est.st_blocks * 512 if est.st_blocks > 0 else est.st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def deletable_reason(p: str) -> str | None:
    """Why a path cannot be deleted in-app, or None if it's fine."""
    if not os.path.isabs(p):
        return "not an absolute path"
    if "\n" in p or "\x00" in p or p.endswith(os.sep + ".."):
        return "invalid path"
    if os.path.normpath(p) != p:
        return "path not normalized"        # scanner always stores normalized paths
    if p in PROTECTED_EXACT:
        return "protected path"
    if HOME.startswith(p + os.sep):
        return "would delete your home folder"
    if len([x for x in p.split(os.sep) if x]) < 2:
        return "path too shallow"
    if not os.path.exists(p):
        return "already gone"
    parent = os.path.dirname(p) or os.sep
    if not os.access(parent, os.W_OK):
        return "needs sudo — use the cleanup script instead"
    if os.path.isdir(p) and not os.path.islink(p) and not os.access(p, os.W_OK):
        return "needs sudo — use the cleanup script instead"
    return None


def _rm_error(func, path, exc_info):
    try:
        os.chmod(path, 0o700)
        func(path)
    except OSError:
        pass


def rm_path(p: str) -> tuple[bool, str | None]:
    try:
        if os.path.islink(p) or os.path.isfile(p):
            os.remove(p)
        elif os.path.isdir(p):
            shutil.rmtree(p, onerror=_rm_error)
        else:
            os.remove(p)
        return True, None
    except OSError as exc:
        return False, str(exc)


def empty_dir_contents(p: str) -> tuple[bool, str | None]:
    errs = []
    try:
        with os.scandir(p) as it:
            entries = [e.path for e in it]
    except OSError as exc:
        return False, str(exc)
    for e in entries:
        ok, err = rm_path(e)
        if not ok:
            errs.append(err or e)
    return (not errs), ("; ".join(errs[:3]) if errs else None)


def _load_recs() -> dict[str, dict]:
    try:
        data = json.loads(DATA_JSON.read_text())
    except Exception:
        return {}
    recs = {}
    for tier in ("safe", "review"):
        for r in (data.get("recommendations") or {}).get(tier, []):
            recs[r.get("id")] = r
    return recs


def run_cleanup(ids: list[str]) -> None:
    """Background job: delete the sources of the given recommendation ids."""
    with _cleanup_lock:
        _cleanup_state.update({"running": True, "started_at": time.time(),
                               "finished_at": None, "planned": 0, "done": 0,
                               "freed": 0, "results": [], "skipped": [],
                               "error": None, "patched": False, "scan_started": False})
    try:
        data_mtime_before = DATA_JSON.stat().st_mtime
    except OSError:
        data_mtime_before = None
    recs = _load_recs()
    plan: list[tuple[str, str]] = []   # (path, rec id)
    skipped: list[dict] = []
    seen: set[str] = set()
    for rid in ids:
        rec = recs.get(str(rid))
        if not rec:
            skipped.append({"id": str(rid), "title": str(rid), "reason": "unknown item"})
            continue
        if not rec.get("sources"):
            skipped.append({"id": rec["id"], "title": rec.get("title", rec["id"]),
                            "path": "", "reason": "no deletable paths — use the script"})
            continue
        for p in rec.get("sources", []):
            reason = deletable_reason(p)
            if reason:
                skipped.append({"id": rec["id"], "title": rec.get("title", rec["id"]),
                                "path": p, "reason": reason})
                continue
            if p in seen:
                continue
            seen.add(p)
            plan.append((p, rec["id"]))

    with _cleanup_lock:
        _cleanup_state["planned"] = len(plan)
        _cleanup_state["skipped"] = skipped

    # delete deepest paths first so parents of nested items are handled last
    plan.sort(key=lambda t: -len(t[0]))

    for path, rid in plan:
        freed = dir_size(path)
        if os.path.normpath(path) == os.path.normpath(os.path.join(HOME, ".Trash")):
            ok, err = empty_dir_contents(path)
        else:
            ok, err = rm_path(path)
        with _cleanup_lock:
            _cleanup_state["done"] += 1
            if ok:
                _cleanup_state["freed"] += freed
                _cleanup_state["results"].append({"id": rid, "path": path,
                                                  "freed": freed, "ok": True})
            else:
                _cleanup_state["results"].append({"id": rid, "path": path,
                                                  "freed": 0, "ok": False, "error": err})

    # refresh the dashboard: apply the deletions to data.json directly …
    patched = False
    try:
        with _cleanup_lock:
            results_snapshot = list(_cleanup_state["results"])
        patched = apply_cleanup_to_data(results_snapshot, data_mtime_before)
    except Exception as exc:
        with _cleanup_lock:
            _cleanup_state["error"] = f"patch failed: {exc}"
    with _cleanup_lock:
        _cleanup_state["patched"] = patched
    # … and only fall back to a slow, full rescan if that wasn't possible
    if not patched:
        try:
            started = start_scan()
            with _cleanup_lock:
                _cleanup_state["scan_started"] = started
        except Exception as exc:
            with _cleanup_lock:
                _cleanup_state["error"] = str(exc)
    with _cleanup_lock:
        _cleanup_state["running"] = False
        _cleanup_state["finished_at"] = time.time()


def cleanup_status() -> dict:
    with _cleanup_lock:
        return dict(_cleanup_state)


def _home_node(children: list, path: str):
    """Deepest home-tree node whose path contains `path` (or None)."""
    for n in children or []:
        npth = n.get("path") or ""
        if npth and (path == npth or path.startswith(npth + os.sep)):
            deeper = _home_node(n.get("children") or [], path)
            return deeper or n
    return None


def apply_cleanup_to_data(results: list[dict], data_mtime_before: float | None) -> bool:
    """Apply just-finished deletions to data.json — no full rescan needed.

    Subtracts the freed bytes from disk/totals/treemaps/insights and removes
    the deleted items. Returns True when data.json reflects the deletions
    (patched here, or a scan finished meanwhile that already includes them).
    """
    # a scan that landed while we were deleting already reflects reality
    try:
        if data_mtime_before is not None and DATA_JSON.stat().st_mtime != data_mtime_before:
            return True
    except OSError:
        return False

    try:
        data = json.loads(DATA_JSON.read_text())
    except Exception:
        return False

    # group successful deletions by item id
    by_id: dict[str, dict] = {}
    deleted_paths: list[str] = []
    for r in results or []:
        if not r.get("ok"):
            continue
        p = r.get("path") or ""
        if not p:
            continue
        deleted_paths.append(p)
        g = by_id.setdefault(str(r.get("id")), {"paths": [], "freed": 0})
        g["paths"].append(p)
        g["freed"] += int(r.get("freed") or 0)
    if not deleted_paths:
        return True  # nothing removed — nothing to patch

    recs_lists = data.get("recommendations") or {}
    rec_by_id: dict[str, tuple[str, dict]] = {}
    for tier in ("safe", "review"):
        for rec in recs_lists.get(tier, []):
            rec_by_id[rec.get("id")] = (tier, rec)

    # per-path bookkeeping (junk amounts keep the treemap hatching in sync)
    del_rows: list[dict] = []
    for r in results or []:
        if not r.get("ok"):
            continue
        p = r.get("path") or ""
        if not p:
            continue
        freed = int(r.get("freed") or 0)
        rid = str(r.get("id"))
        tier, rec = rec_by_id.get(rid, (None, None))
        if rec is not None:
            ss = rec.get("source_sizes") or {}
            fallback = rec.get("size", 0) / max(1, len(rec.get("sources") or []))
            amt = int(ss.get(p, fallback))
        else:
            amt = freed
        del_rows.append({"rid": rid, "path": p, "freed": freed, "tier": tier,
                         "amount": amt, "safe_amount": amt if tier == "safe" else 0})
    freed_total = sum(x["freed"] for x in del_rows)

    # recommendations + tier totals
    remaining_ids: set[str] = set()
    for tier in ("safe", "review"):
        keep: list[dict] = []
        for rec in recs_lists.get(tier, []):
            rid = rec.get("id")
            g = by_id.get(rid)
            if not g:
                keep.append(rec)
                remaining_ids.add(rid)
                continue
            delset = set(g["paths"])
            rec["sources"] = [p for p in (rec.get("sources") or []) if p not in delset]
            ss = rec.get("source_sizes") or {}
            rec["source_sizes"] = {p: v for p, v in ss.items() if p in rec["sources"]}
            if rec["sources"]:
                rec["size"] = (int(sum(rec["source_sizes"].values())) if rec["source_sizes"]
                               else max(0, int(rec.get("size", 0)) - g["freed"]))
                if rec["size"] > 0:
                    keep.append(rec)
                    remaining_ids.add(rid)
            # items left with nothing fall out of the list entirely
        recs_lists[tier] = keep
        totals = data.get("totals") or {}
        totals[tier] = sum(int(r.get("size", 0)) for r in keep)  # stay consistent with the item list
        data["totals"] = totals
    data["recommendations"] = recs_lists

    # cleanup treemap (estimate-based like the items, so subtract the item amounts)
    amt_by_id: dict[str, int] = {}
    for row in del_rows:
        amt_by_id[row["rid"]] = amt_by_id.get(row["rid"], 0) + row["amount"]
    for node in data.get("cleanup_nodes") or []:
        items = node.get("items") or []
        for rid in list(items):
            amt = amt_by_id.get(rid, 0)
            if not amt:
                continue
            node["size"] = max(0, int(node.get("size", 0)) - amt)
            tier = rec_by_id.get(rid, (None, None))[0]
            if tier:
                node[tier] = max(0, int(node.get(tier, 0)) - amt)
        node["items"] = [i for i in items if i in remaining_ids]
        node["tier"] = "safe" if int(node.get("safe", 0)) >= int(node.get("review", 0)) else "review"
    data["cleanup_nodes"] = sorted(
        [n for n in (data.get("cleanup_nodes") or []) if n["items"] and n["size"] > 0],
        key=lambda n: -n["size"])

    # home tree + outside listings (real sizes, so subtract the measured freed bytes)
    home = data.get("home") or {}
    outside = data.get("outside") or []
    for row in del_rows:
        p, freed = row["path"], row["freed"]
        if not freed:
            continue
        if p == HOME or p.startswith(HOME + os.sep):
            home["total"] = max(0, int(home.get("total", 0)) - freed)
            node = _home_node(home.get("children") or [], p)
            if node:
                node["size"] = max(0, int(node.get("size", 0)) - freed)
                node["junk"] = max(0, int(node.get("junk", 0)) - int(row["amount"]))
                node["safe_junk"] = max(0, int(node.get("safe_junk", 0)) - int(row["safe_amount"]))
        else:
            for e in outside:
                en = e.get("name") or ""
                if en and (p == en or p.startswith(en + os.sep)):
                    e["size"] = max(0, int(e.get("size", 0)) - freed)
                    if p != en:
                        seg = p[len(en):].lstrip(os.sep).split(os.sep)[0]
                        for c in e.get("children") or []:
                            if c.get("name") == seg:
                                c["size"] = max(0, int(c.get("size", 0)) - freed)
                                break
                    break
    data["home"] = home

    # big files that are gone
    data["big_files"] = [b for b in (data.get("big_files") or [])
                         if not any(b.get("path") == p or (b.get("path") or "").startswith(p + os.sep)
                                    for p in deleted_paths)]

    # disk numbers: fresh filesystem state beats arithmetic
    fresh = None
    if _scanner is not None:
        try:
            fresh = _scanner.disk_info() or None
        except Exception:
            fresh = None
    if fresh:
        data["disk"] = fresh
    elif data.get("disk"):
        d = data["disk"]
        d["used"] = max(0, int(d.get("used", 0)) - freed_total)
        d["free"] = int(d.get("free", 0)) + freed_total
        d["percent_used"] = round(d["used"] / d["total"] * 100) if d.get("total") else 0

    # regenerate insights against the patched numbers
    if _scanner is not None and hasattr(_scanner, "build_insights"):
        try:
            data["insights"] = _scanner.build_insights(
                recs_lists.get("safe", []) + recs_lists.get("review", []),
                sum(int(r.get("size", 0)) for r in recs_lists.get("safe", [])),
                data.get("disk") or {}, data.get("memory") or {},
                data.get("snapshots") or [], int(data.get("unaccounted") or 0))
        except Exception:
            pass

    st = data.get("stats") or {}
    st["rec_count"] = len(recs_lists.get("safe", [])) + len(recs_lists.get("review", []))
    data["stats"] = st
    data["patched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    data["patched_freed"] = freed_total

    try:
        tmp = DATA_JSON.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(DATA_JSON)
    except OSError:
        return False
    if _scanner is not None:
        try:
            _scanner.log(f"cleanup applied to data.json: {_scanner.human(freed_total)} freed "
                         f"across {len(deleted_paths)} path(s) — no rescan")
        except Exception:
            pass
    return True


class Handler(BaseHTTPRequestHandler):
    server_version = "MacStorageDashboard/1.0"

    def log_message(self, fmt, *args):  # quieter
        if self.path.startswith("/api/"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ------------------------------------------------------------------ utils
    def _guard(self) -> bool:
        """Only serve localhost; block cross-origin POSTs (DNS-rebinding hygiene)."""
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "::1"):
            self._json({"error": "forbidden"}, 403)
            return False
        origin = self.headers.get("Origin")
        if origin:
            ok = (origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:")
                  or origin.startswith("http://[::1]:"))
            if not ok:
                self._json({"error": "forbidden origin"}, 403)
                return False
        return True

    def _send(self, code: int, body: bytes, ctype: str = "application/json",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n))
        except Exception:
            return {}

    # ------------------------------------------------------------------ routes
    def do_GET(self):
        if not self._guard():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/status":
            st = read_state()
            data_mtime = DATA_JSON.stat().st_mtime if DATA_JSON.exists() else None
            running = scanner_running()
            self._json({"running": running, "state": st, "data_mtime": data_mtime,
                        "server_time": time.time()})
            return

        if path == "/api/cleanup/status":
            self._json(cleanup_status())
            return

        if path in ("/", "/index.html"):
            return self._serve_file("index.html", "text/html; charset=utf-8")

        if path.startswith("/demo/"):
            # demo hooks for docs screenshots: /demo/cleanup, /demo/delete, …
            return self._serve_file("index.html", "text/html; charset=utf-8")

        if path == "/data.json":
            return self._serve_file("data.json", "application/json")

        if path == "/favicon.ico":
            # browsers without SVG icon support still get a valid icon
            return self._serve_file("favicon.svg", "image/svg+xml")

        # static fallback: only files directly inside HERE, whitelisted suffixes
        name = os.path.basename(path)
        if name and Path(HERE / name).is_file() and name.endswith((".html", ".js", ".css", ".json", ".svg", ".png")):
            ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript",
                     "css": "text/css", "json": "application/json",
                     "svg": "image/svg+xml", "png": "image/png"}[name.rsplit(".", 1)[1]]
            return self._serve_file(name, ctype)

        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._guard():
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/shutdown":
            self._json({"stopping": True})

            def _stop():
                time.sleep(0.4)  # let the response flush to the browser
                try:
                    os.remove(HERE / ".server.port")
                except OSError:
                    pass
                os._exit(0)

            threading.Thread(target=_stop, daemon=True).start()
            return

        if path == "/api/rescan":
            if scanner_running():
                self._json({"started": False, "reason": "already running"})
            else:
                ok = start_scan()
                self._json({"started": ok})
            return

        if path == "/api/cleanup":
            if cleanup_status().get("running"):
                self._json({"started": False, "reason": "cleanup already running"}, 409)
                return
            if scanner_running():
                self._json({"started": False, "reason": "a scan is in progress — try again in a moment"}, 409)
                return
            body = self._read_json()
            ids = body.get("ids")
            if not isinstance(ids, list) or not ids or len(ids) > 100:
                self._json({"started": False, "error": "ids must be a non-empty list (max 100)"}, 400)
                return
            ids = [str(i) for i in ids]
            t = threading.Thread(target=run_cleanup, args=(ids,), daemon=True)
            t.start()
            self._json({"started": True, "count": len(ids)})
            return

        if path == "/api/open-settings":
            try:
                subprocess.Popen(["open",
                                  "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
                self._json({"ok": True})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return

        if path == "/api/reveal":
            body = self._read_json()
            p = str(body.get("path") or "")
            if not p or not os.path.isabs(p) or "\x00" in p or "\n" in p:
                self._json({"ok": False, "error": "bad path"}, 400)
                return
            if not os.path.exists(p):
                self._json({"ok": False, "error": "path does not exist"}, 404)
                return
            try:
                subprocess.Popen(["open", "-R", p])
                self._json({"ok": True})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
            return

        self._json({"error": "not found"}, 404)

    def _serve_file(self, name: str, ctype: str):
        f = HERE / name
        if not f.is_file():
            self._json({"error": f"{name} not found — run a scan first"}, 404)
            return
        self._send(200, f.read_bytes(), ctype)


def free_port(start: int) -> int:
    for port in range(start, start + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("no free port found")


def existing_dashboard(port: int) -> bool:
    """True if a Mac Storage Dashboard server already answers on this port."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1.5) as r:
            body = r.read()
            return r.status == 200 and b'"running"' in body
    except Exception:
        return False


def running_port() -> int | None:
    """Port of an already-running dashboard server, if any."""
    candidates: list[int] = []
    try:
        pf = HERE / ".server.port"
        if pf.is_file():
            candidates.append(int(pf.read_text().strip()))
    except (OSError, ValueError):
        pass
    if 8899 not in candidates:
        candidates.append(8899)
    for p in candidates:
        if existing_dashboard(p):
            return p
    return None


def main():
    port_arg = sys.argv[1] if len(sys.argv) > 1 else None
    if port_arg:
        httpd = ThreadingHTTPServer(("127.0.0.1", int(port_arg)), Handler)
        port = int(port_arg)
    else:
        # already running? just bring the browser back to it
        already = running_port()
        if already:
            url = f"http://127.0.0.1:{already}/"
            print(f"Mac Storage Dashboard is already running → {url}")
            try:
                webbrowser.open(url)
            except Exception:
                pass
            return
        try:
            # prefer 8899; SO_REUSEADDR lets us rebind over TIME_WAIT sockets
            httpd = ThreadingHTTPServer(("127.0.0.1", 8899), Handler)
            port = 8899
        except OSError:
            port = free_port(8900)
            httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)

    # record the actual port so the app/launcher can find us
    try:
        (HERE / ".server.port").write_text(str(port))
    except OSError:
        pass

    if not DATA_JSON.exists() and not scanner_running():
        print("No data.json yet — starting a scan in the background…")
        start_scan()

    url = f"http://127.0.0.1:{port}/"
    print(f"Mac Storage Dashboard → {url}")
    print("Press Ctrl+C to stop.")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
