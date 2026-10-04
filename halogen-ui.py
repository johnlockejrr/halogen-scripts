#!/usr/bin/env python3
"""
halogen-ui — a small web console for halogen.sh.

    ./halogen-ui.py --script ./halogen.sh
    # then open http://127.0.0.1:8780

Reads the profiles out of halogen.sh itself, so the script stays the single
source of truth: add a case arm there and it shows up here.

What it does:
  - lists profiles, with the checkpoint / pool / NPU models each one uses
  - greys out any profile whose weight files are missing, and says which
  - refuses to start a second server while one is running
  - starts a profile detached, streams its log, stops it again
  - polls /health, /v1/models and /cache on the server
  - shows host memory, fragmentation and GPU use beside it

Standard library only. Binds to loopback by default; it can start and stop
containers, so do not expose it without putting auth in front.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATE = {
    "proc": None,        # Popen of the detached halogen.sh
    "profile": None,
    "started": None,
    "logfile": None,
    "run_id": None,
    "stop_evt": None,
}
HIST: "History | None" = None
LOCK = threading.Lock()



# ----------------------------------------------------------------- history
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY,
  profile TEXT, checkpoint TEXT, pool TEXT, slots TEXT, ctx TEXT,
  vision INT, npu TEXT, image_tag TEXT, engine_version TEXT,
  started_at REAL, stopped_at REAL, duration_s REAL,
  weights_gib REAL, held_gib REAL, pin_gbs REAL, pin_stalls INT,
  ready_s REAL, logfile TEXT, stop_reason TEXT
);
CREATE TABLE IF NOT EXISTS reqs (
  id INTEGER PRIMARY KEY, run_id INT, ts REAL,
  out_tokens INT, secs REAL, tps REAL, rounds INT, commit_rate REAL,
  prompt INT, cached INT, cached_pct REAL, prefill_s REAL,
  pool_used INT, pool_total INT, closed_by TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, run_id INT, ts REAL, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS samples (
  id INTEGER PRIMARY KEY, run_id INT, ts REAL,
  mem_avail_mib INT, order9 INT, pool_used INT, pool_total INT,
  hit_rate REAL, disk_records INT
);
CREATE INDEX IF NOT EXISTS reqs_run ON reqs(run_id);
CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
CREATE INDEX IF NOT EXISTS samples_run ON samples(run_id);
"""


class History:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()
        self.lock = threading.Lock()
        # any run left open by a crash is closed on startup
        with self.lock:
            self.db.execute(
                "UPDATE runs SET stopped_at=COALESCE(stopped_at, started_at),"
                " stop_reason=COALESCE(stop_reason,'interrupted') WHERE stopped_at IS NULL")
            self.db.commit()

    def begin(self, profile: str, info: dict, logfile: str) -> int:
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO runs (profile,checkpoint,pool,slots,ctx,vision,npu,"
                "started_at,logfile) VALUES (?,?,?,?,?,?,?,?,?)",
                (profile, info.get("checkpoint"), str(info.get("pool")),
                 str(info.get("slots")), str(info.get("ctx")),
                 int(bool(info.get("vision"))), ",".join(info.get("npu") or []),
                 time.time(), logfile))
            self.db.commit()
            return cur.lastrowid

    def end(self, run_id: int, reason: str):
        with self.lock:
            self.db.execute(
                "UPDATE runs SET stopped_at=?, duration_s=?-started_at, stop_reason=?"
                " WHERE id=? AND stopped_at IS NULL",
                (time.time(), time.time(), reason, run_id))
            self.db.commit()

    def set(self, run_id: int, **cols):
        if not cols:
            return
        sets = ",".join(f"{k}=?" for k in cols)
        with self.lock:
            self.db.execute(f"UPDATE runs SET {sets} WHERE id=?",
                            (*cols.values(), run_id))
            self.db.commit()

    def add_req(self, run_id: int, r: dict):
        with self.lock:
            self.db.execute(
                "INSERT INTO reqs (run_id,ts,out_tokens,secs,tps,rounds,commit_rate,"
                "prompt,cached,cached_pct,prefill_s,pool_used,pool_total,closed_by)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, time.time(), r.get("out"), r.get("secs"), r.get("tps"),
                 r.get("rounds"), r.get("commit"), r.get("prompt"), r.get("cached"),
                 r.get("cached_pct"), r.get("prefill_s"), r.get("pool_used"),
                 r.get("pool_total"), r.get("closed_by")))
            self.db.commit()

    def add_event(self, run_id: int, kind: str, detail: str):
        with self.lock:
            self.db.execute("INSERT INTO events (run_id,ts,kind,detail) VALUES (?,?,?,?)",
                            (run_id, time.time(), kind, detail[:500]))
            self.db.commit()

    def add_sample(self, run_id: int, s: dict):
        with self.lock:
            self.db.execute(
                "INSERT INTO samples (run_id,ts,mem_avail_mib,order9,pool_used,"
                "pool_total,hit_rate,disk_records) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, time.time(), s.get("mem"), s.get("order9"), s.get("pool_used"),
                 s.get("pool_total"), s.get("hit_rate"), s.get("records")))
            self.db.commit()

    def runs(self, limit=60) -> list[dict]:
        q = """SELECT r.*,
                 (SELECT COUNT(*) FROM reqs q WHERE q.run_id=r.id) AS n_req,
                 (SELECT SUM(out_tokens) FROM reqs q WHERE q.run_id=r.id) AS tokens,
                 (SELECT ROUND(AVG(tps),1) FROM reqs q WHERE q.run_id=r.id) AS avg_tps,
                 (SELECT MAX(prompt) FROM reqs q WHERE q.run_id=r.id) AS max_prompt,
                 (SELECT COUNT(*) FROM events e WHERE e.run_id=r.id
                    AND e.kind IN ('error','clamped','cannot_grow')) AS n_err
               FROM runs r ORDER BY r.id DESC LIMIT ?"""
        with self.lock:
            return [dict(x) for x in self.db.execute(q, (limit,))]

    def run(self, run_id: int) -> dict:
        with self.lock:
            r = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not r:
                return {}
            reqs = [dict(x) for x in self.db.execute(
                "SELECT * FROM reqs WHERE run_id=? ORDER BY id DESC LIMIT 500", (run_id,))]
            evs = [dict(x) for x in self.db.execute(
                "SELECT * FROM events WHERE run_id=? ORDER BY id DESC LIMIT 200", (run_id,))]
            sam = [dict(x) for x in self.db.execute(
                "SELECT * FROM samples WHERE run_id=? ORDER BY id", (run_id,))]
        return {"run": dict(r), "reqs": reqs, "events": evs, "samples": sam}


# Lines halogen writes that are worth keeping.
RE_MTP = re.compile(
    r"mtp (?P<out>\d+) tok in (?P<secs>[\d.]+)s = (?P<tps>[\d.]+) t/s"
    r"(?:.*?(?P<rounds>\d+) rounds, commit (?P<commit>[\d.]+)/round)?"
    r".*?prompt (?P<prompt>\d+)(?: \((?P<cached>\d+) cached, (?P<cpct>[\d.]+)%\))?"
    r"(?:, prefill (?P<prefill>[\d.]+)s)?"
    r".*?(?:pool (?P<pused>\d+)/(?P<ptot>\d+))?"
    r"(?:.*?closed at \d+ by (?P<closed>[a-z_ ]+))?")
RE_POOL = re.compile(r"pool (?P<used>\d+)/(?P<tot>\d+)")
RE_HTTP = re.compile(r'"(?:POST|GET) (?P<path>\S+) HTTP/1.1" (?P<code>\d{3})')
RE_VER = re.compile(r"serve_api: version (?P<v>[\d.]+), engine version")
RE_CKPT = re.compile(r"halogen: (?P<p>/models/\S+\.hgn) carries its own")
RE_MEM = re.compile(r"memory: (?P<w>[\d.]+) GiB of weights locked in RAM.*?"
                    r"(?P<all>[\d.]+) GiB in all")
RE_PIN = re.compile(r"pinned [\d.]+ GiB in \d+ range\(s\) in [\d.]+ s "
                    r"\((?P<gbs>[\d.]+) GB/s\)")
RE_STALL = re.compile(r"(?P<n>\d+) compaction stalls during that step")
RE_READY = re.compile(r"halogen: engine listening after (?P<s>\d+)s")
RE_IMG = re.compile(r"using \S+:(?P<tag>[\d.]+)")


class LogWatcher(threading.Thread):
    """Follows the run's log, files per-request rows and notable events."""

    def __init__(self, hist: History, run_id: int, logfile: Path, stop_evt):
        super().__init__(daemon=True)
        self.h, self.run_id, self.lf, self.stop = hist, run_id, logfile, stop_evt

    def run(self):
        pos, stalls = 0, 0
        while not self.stop.is_set():
            try:
                if self.lf.exists():
                    with open(self.lf, "rb") as fh:
                        fh.seek(pos)
                        chunk = fh.read()
                        pos = fh.tell()
                    for line in chunk.decode("utf-8", "replace").splitlines():
                        self.line(line)
            except Exception:
                pass
            self.stop.wait(1.0)

    def line(self, line: str):
        h, rid = self.h, self.run_id
        if "serve_api: mtp" in line:
            m = RE_MTP.search(line)
            if m:
                g = m.groupdict()
                h.add_req(rid, {
                    "out": int(g["out"]), "secs": float(g["secs"]),
                    "tps": float(g["tps"]),
                    "rounds": int(g["rounds"]) if g["rounds"] else None,
                    "commit": float(g["commit"]) if g["commit"] else None,
                    "prompt": int(g["prompt"]),
                    "cached": int(g["cached"]) if g["cached"] else 0,
                    "cached_pct": float(g["cpct"]) if g["cpct"] else 0.0,
                    "prefill_s": float(g["prefill"]) if g["prefill"] else None,
                    "pool_used": int(pm.group("used")) if (pm := RE_POOL.search(line)) else None,
                    "pool_total": int(pm.group("tot")) if pm else None,
                    "closed_by": (g["closed"] or "").strip() or None})
            return
        m = RE_HTTP.search(line)
        if m and m.group("code") != "200":
            h.add_event(rid, "error", f'{m.group("code")} {m.group("path")}')
            return
        for kind, needle in (("clamped", "max_tokens clamped"),
                             ("cannot_grow", "cannot grow"),
                             ("warning", "WARNING")):
            if needle in line:
                h.add_event(rid, kind, line.strip())
                break
        for rx, col, cast in ((RE_VER, "engine_version", str),
                              (RE_IMG, "image_tag", str),
                              (RE_READY, "ready_s", float),
                              (RE_PIN, "pin_gbs", float)):
            m = rx.search(line)
            if m:
                h.set(rid, **{col: cast(list(m.groupdict().values())[0])})
        m = RE_MEM.search(line)
        if m:
            h.set(rid, weights_gib=float(m.group("w")), held_gib=float(m.group("all")))
        m = RE_STALL.search(line)
        if m:
            h.set(rid, pin_stalls=int(m.group("n")))


class Sampler(threading.Thread):
    """Periodic snapshot of host memory and the server's own cache counters."""

    def __init__(self, hist: History, run_id: int, port: str, stop_evt, every=30):
        super().__init__(daemon=True)
        self.h, self.rid, self.port, self.stop, self.every = hist, run_id, port, stop_evt, every

    def run(self):
        while not self.stop.is_set():
            self.stop.wait(self.every)
            if self.stop.is_set():
                break
            try:
                r = resources()
                c = http_json(f"http://127.0.0.1:{self.port}/cache") or {}
                pool = c.get("pool") or {}
                self.h.add_sample(self.rid, {
                    "mem": r["mem_mib"].get("MemAvailable"),
                    "order9": r.get("order9_blocks"),
                    "pool_used": pool.get("used"), "pool_total": pool.get("positions"),
                    "hit_rate": c.get("token_hit_rate"),
                    "records": (c.get("disk") or {}).get("records")})
            except Exception:
                pass


# ------------------------------------------------------------------ script
def sh(cmd: list[str], timeout: float = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


class Script:
    """Parses halogen.sh for its profiles and what each one needs."""

    def __init__(self, path: Path):
        self.path = path
        self.text = path.read_text()
        self.vars = self._vars()
        self.profiles = self._profiles()

    def _vars(self) -> dict:
        out = {}
        for k in ("MODEL_DIR", "CACHE_DIR", "PORT", "IMAGE", "REPO", "REGISTRY"):
            m = re.search(rf'^{k}="?([^"\n]+)"?$', self.text, re.M)
            if m:
                out[k] = m.group(1)
        # IMAGE is built from REGISTRY/REPO
        if "IMAGE" in out:
            out["IMAGE"] = (out["IMAGE"]
                            .replace("${REGISTRY}", out.get("REGISTRY", ""))
                            .replace("${REPO}", out.get("REPO", "")))
        return out

    def _func_body(self, name: str) -> str:
        m = re.search(rf"^{name}\(\)\s*\{{(.*?)^\}}", self.text, re.M | re.S)
        return m.group(1) if m else ""

    def _profiles(self) -> list[dict]:
        """Each case arm that calls a run_* function, or `run` directly."""
        out = []
        case = re.search(r"^case .*?^esac", self.text, re.M | re.S)
        body = case.group(0) if case else self.text
        for arm in re.finditer(
            r"^\s{2}([a-z0-9|-]+)\)\s*\n(.*?);;", body, re.M | re.S
        ):
            label, inner = arm.group(1), arm.group(2)
            if label in ("clean", "latest"):
                continue
            fn = re.search(r"\b(run(?:_[a-z0-9_]+)?)\s+\"\$tag\"", inner)
            if not fn:
                continue
            fname = fn.group(1)
            info = self.describe(fname)
            info["name"] = label
            info["fn"] = fname
            out.append(info)
        return out

    def describe(self, fname: str) -> dict:
        body = self._func_body(fname) if fname != "run" else ""
        env = dict(re.findall(r"-e\s+(HALOGEN_[A-Z_0-9]+)=([^\s\\]+)", body))
        pool = None
        m = re.search(r"optimal_env\s+(\d+)", body)
        if m:
            pool = int(m.group(1))
            env.update(dict(re.findall(
                r"-e\s+(HALOGEN_[A-Z_0-9]+)=([^\s\\]+)",
                self._func_body("optimal_env"))))
            env["HALOGEN_KV_POOL_POSITIONS"] = str(pool)
        ckpt = env.get("HALOGEN_CHECKPOINT", "")
        npu = env.get("HALOGEN_NPU_MODELS", "")
        return {
            "checkpoint": ckpt.split("/")[-1] if ckpt else "(server default)",
            "checkpoint_path": ckpt,
            "pool": env.get("HALOGEN_KV_POOL_POSITIONS", "(default)"),
            "slots": env.get("HALOGEN_KV_SLOTS", "(default)"),
            "ctx": env.get("HALOGEN_CTX", "(default)"),
            "vision": "HALOGEN_VISION_TOWER" in env,
            "npu": [m for m in npu.split(",") if m] if npu else [],
            "env": env,
        }

    def requirements(self, p: dict) -> list[str]:
        """Files that must exist for this profile, as host paths."""
        md = Path(self.vars.get("MODEL_DIR", "/models"))
        need = []
        if p["checkpoint_path"]:
            need.append(str(md / p["checkpoint_path"].split("/models/")[-1]))
        ng = p["env"].get("HALOGEN_NGRAM_TABLE")
        if ng:
            need.append(str(md / ng.split("/models/")[-1]))
        if p["vision"] and p["env"].get("HALOGEN_VISION_TOWER", "1") != "1":
            need.append(str(md / p["env"]["HALOGEN_VISION_TOWER"].split("/models/")[-1]))
        for m in p["npu"]:
            if not m.startswith("/"):
                need.append(str(md / "npu" / m))
        return need


# ------------------------------------------------------------------ probes
def container(script: Script) -> dict | None:
    img = script.vars.get("IMAGE", "halogen")
    rc, out = sh(["podman", "ps", "--filter", f"ancestor={img}",
                  "--format", "{{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Names}}"], 5)
    if rc != 0:
        return None
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            return {"id": parts[0], "image": parts[1],
                    "status": parts[2], "name": parts[3]}
    return None


def http_json(url: str, timeout: float = 1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r)
    except Exception:
        return None


def resources() -> dict:
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            if k in ("MemTotal", "MemAvailable", "MemFree", "Cached", "SwapTotal", "SwapFree"):
                mem[k] = int(v.split()[0]) // 1024  # MiB
    except Exception:
        pass
    frag = None
    try:
        for line in Path("/proc/buddyinfo").read_text().splitlines():
            if "Normal" in line:
                f = line.split()
                frag = int(f[13]) if len(f) > 13 else None
                break
    except Exception:
        pass
    load = os.getloadavg()
    return {"mem_mib": mem, "order9_blocks": frag,
            "load": [round(x, 2) for x in load]}


_VER = {"latest": None, "at": 0.0, "busy": False}


def _refresh_latest(script: "Script"):
    """Ask the registry for the newest tag. Slow, so it never runs inline."""
    try:
        rc, out = sh(["bash", str(script.path), "latest"], 25)
        m = re.search(r"\bv?\d+\.\d+\.\d+\b", out) if rc == 0 else None
        _VER["latest"] = m.group(0) if m else None
    except Exception:
        pass
    finally:
        _VER["at"] = time.time()
        _VER["busy"] = False


def versions(script: "Script", running_image: str | None) -> dict:
    """Running tag from the container image; newest tag from a cached lookup."""
    run_tag = (running_image.rsplit(":", 1)[1]
               if running_image and ":" in running_image else None)
    rc, out = sh(["podman", "images", "--filter",
                  f"reference={script.vars.get('IMAGE','')}", "--format", "{{.Tag}}"], 5)
    local = []
    if rc == 0:
        local = sorted({t.strip() for t in out.split()
                        if re.fullmatch(r"v?[\d.]+", t.strip())},
                       key=lambda t: [int(x) for x in t.lstrip("v").split(".")])
    if not _VER["busy"] and time.time() - _VER["at"] > 900:
        _VER["busy"] = True
        threading.Thread(target=_refresh_latest, args=(script,), daemon=True).start()
    return {"running": run_tag, "local": local, "latest": _VER["latest"]}


# ------------------------------------------------------------------- run
def start(script: Script, profile: str, logdir: Path) -> tuple[bool, str]:
    with LOCK:
        if container(script):
            return False, "a halogen container is already running"
        if STATE["proc"] and STATE["proc"].poll() is None:
            return False, "a start is already in progress"
        logdir.mkdir(parents=True, exist_ok=True)
        lf = logdir / f"{time.strftime('%Y%m%d-%H%M%S')}-{profile}.log"
        fh = open(lf, "wb")
        # start_new_session so the whole process group can be signalled later
        proc = subprocess.Popen(
            ["bash", str(script.path), profile],
            stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=str(script.path.parent),
        )
        run_id, stop_evt = None, threading.Event()
        if HIST:
            info = next((p for p in script.profiles if p["name"] == profile), {})
            run_id = HIST.begin(profile, info, str(lf))
            LogWatcher(HIST, run_id, lf, stop_evt).start()
            Sampler(HIST, run_id, script.vars.get("PORT", "8731"), stop_evt).start()
        STATE.update(proc=proc, profile=profile, started=time.time(),
                     logfile=str(lf), run_id=run_id, stop_evt=stop_evt)
        return True, str(lf)


def stop(script: Script) -> tuple[bool, str]:
    msgs = []
    c = container(script)
    if c:
        rc, out = sh(["podman", "stop", "-t", "60", c["id"]], 90)
        msgs.append(f"podman stop {c['id'][:12]}: rc={rc}")
    with LOCK:
        p = STATE.get("proc")
        if p and p.poll() is None:
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                msgs.append("signalled the launcher")
            except Exception as e:
                msgs.append(f"launcher: {e}")
        if STATE.get("stop_evt"):
            STATE["stop_evt"].set()
        if HIST and STATE.get("run_id"):
            time.sleep(1.2)                      # let the watcher drain the tail
            HIST.end(STATE["run_id"], "stopped")
        STATE.update(proc=None, profile=None, started=None,
                     run_id=None, stop_evt=None)
    return True, "; ".join(msgs) or "nothing was running"


# ------------------------------------------------------------------- http
PAGE = r"""<!doctype html><meta charset=utf-8><title>halogen</title>
<style>
 body{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f1115;color:#dfe3ea}
 header{padding:14px 22px;border-bottom:1px solid #222733;display:flex;gap:18px;align-items:center}
 h1{font-size:17px;margin:0;font-weight:600}
 .pill{font:12px ui-monospace,monospace;padding:3px 9px;border-radius:999px;border:1px solid #2a3040}
 .up{background:#10301c;border-color:#1d5c34;color:#7ee2a8}
 .down{background:#2c1418;border-color:#5c2030;color:#f0a0ae}
 main{display:grid;grid-template-columns:420px 1fr;gap:18px;padding:18px 22px}
 .card{border:1px solid #222733;border-radius:10px;padding:14px;background:#141720;margin-bottom:16px}
 .card h2{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:#7c879b;margin:0 0 10px}
 .prof{border:1px solid #242a38;border-radius:8px;padding:10px;margin-bottom:9px}
 .prof.bad{opacity:.45}
 .prof b{font:13px ui-monospace,monospace}
 .meta{font:11px ui-monospace,monospace;color:#8792a6;margin-top:4px}
 .miss{font:11px ui-monospace,monospace;color:#f0a0ae;margin-top:4px}
 button{font:12px inherit;padding:5px 12px;border-radius:6px;border:1px solid #2e3648;
        background:#1d2330;color:#dfe3ea;cursor:pointer}
 button:hover:not(:disabled){background:#283040}
 button:disabled{opacity:.4;cursor:not-allowed}
 button.stop{border-color:#5c2030;background:#2c1418;color:#f0a0ae}
 pre{margin:0;font:12px/1.45 ui-monospace,monospace;white-space:pre-wrap;
     max-height:62vh;overflow:auto;background:#0b0d12;padding:12px;border-radius:8px}
 table{width:100%;border-collapse:collapse;font:12px ui-monospace,monospace}
 td{padding:3px 0;border-bottom:1px solid #1c212c}
 td:last-child{text-align:right;color:#9fb0c8}
 .warn{color:#f0c674}
 .run{display:grid;grid-template-columns:1fr auto;gap:2px 10px;padding:7px 8px;
      border:1px solid #242a38;border-radius:7px;margin-bottom:6px;cursor:pointer;
      font:11px ui-monospace,monospace}
 .run:hover{background:#1a1f2b}
 .run .t{color:#9fb0c8}
 .run .s{color:#7c879b}
 .err{color:#f0a0ae}
</style>
<header>
  <h1>halogen</h1>
  <span id=state class="pill down">checking…</span>
  <span id=health class="pill"></span>
  <span id=ver class="pill"></span>
  <span style=flex:1></span>
  <button class=stop id=stopbtn disabled>Stop</button>
</header>
<main>
 <div>
  <div class=card><h2>Profiles</h2><div id=profiles></div></div>
  <div class=card><h2>Host</h2><table id=res></table></div>
  <div class=card><h2>Server</h2><table id=srv></table></div>
  <div class=card><h2>History</h2><div id=hist></div></div>
 </div>
 <div>
  <div class=card><h2>Log <span id=logname style="color:#55607a"></span></h2><pre id=log>—</pre></div>
  <div class=card id=detailcard style=display:none><h2>Run detail <span id=detname style="color:#55607a"></span></h2><div id=detail></div></div>
 </div>
</main>
<script>
let off=0, logEl=document.getElementById('log'), following=true;
logEl.addEventListener('scroll',()=>{following = logEl.scrollTop+logEl.clientHeight >= logEl.scrollHeight-40;});

async function post(u,b){const r=await fetch(u,{method:'POST',body:JSON.stringify(b||{})});return r.json();}

function row(k,v){return `<tr><td>${k}</td><td>${v}</td></tr>`;}

async function tick(){
  const s = await (await fetch('/api/status')).json();
  const st=document.getElementById('state');
  st.textContent = s.container ? `running · ${s.container.status}` : 'stopped';
  st.className = 'pill ' + (s.container?'up':'down');
  const h=document.getElementById('health');
  h.textContent = s.health ? 'API ok' + (s.models.length?` · ${s.models.length} model(s)`:'') : 'API down';
  h.className = 'pill ' + (s.health?'up':'down');
  document.getElementById('stopbtn').disabled = !s.container && !s.launching;

  document.getElementById('profiles').innerHTML = s.profiles.map(p=>`
    <div class="prof ${p.missing.length?'bad':''}">
      <b>${p.name}</b>
      <button style=float:right ${p.missing.length||s.container?'disabled':''}
        onclick="start('${p.name}')">Start</button>
      <div class=meta>${p.checkpoint} · pool ${p.pool} · slots ${p.slots}${p.vision?' · vision':''}${p.npu.length?' · npu '+p.npu.length:''}</div>
      ${p.missing.length?`<div class=miss>missing: ${p.missing.join(', ')}</div>`:''}
    </div>`).join('');

  const m=s.resources.mem_mib||{};
  document.getElementById('res').innerHTML =
    row('MemAvailable', ((m.MemAvailable||0)/1024).toFixed(1)+' GiB') +
    row('MemTotal', ((m.MemTotal||0)/1024).toFixed(1)+' GiB') +
    row('Cached', ((m.Cached||0)/1024).toFixed(1)+' GiB') +
    row('order-9 blocks', (s.resources.order9_blocks??'—') +
       (s.resources.order9_blocks!==null && s.resources.order9_blocks<500?' <span class=warn>low</span>':'')) +
    row('load', (s.resources.load||[]).join(' '));

  const c=s.cache||{}, p=c.pool||{};
  document.getElementById('srv').innerHTML =
    row('profile', s.profile||'—') +
    row('models', (s.models||[]).join(', ')||'—') +
    row('pool used', p.used!==undefined? `${p.used} / ${p.positions}`:'—') +
    row('cache hit rate', c.token_hit_rate!==undefined && c.token_hit_rate!==null ? (c.token_hit_rate*100).toFixed(1)+'%':'—') +
    row('disk records', (c.disk&&c.disk.records)??'—');

  document.getElementById('logname').textContent = s.logfile? ' · '+s.logfile.split('/').pop():'';

  const v=s.versions||{};
  const ve=document.getElementById('ver');
  if(v.running){ ve.textContent='v'+v.running + (v.latest&&v.latest!==v.running?` · ${v.latest} available`:'');
    ve.className='pill '+(v.latest&&v.latest!==v.running?'down':'up'); }
  else { ve.textContent = v.latest? 'latest '+v.latest : ''; ve.className='pill'; }
}

async function start(name){
  const r = await post('/api/start',{profile:name});
  if(!r.ok) alert(r.error);
  off=0; logEl.textContent='';
  tick();
}
document.getElementById('stopbtn').onclick = async()=>{
  if(!confirm('Stop the running server?')) return;
  const r = await post('/api/stop'); await tick();
};

async function logs(){
  const r = await (await fetch('/api/logs?off='+off)).json();
  if(r.text){ logEl.textContent += r.text; off = r.off;
    if(following) logEl.scrollTop = logEl.scrollHeight; }
}
function dur(x){ if(!x) return '—'; const m=Math.floor(x/60), h=Math.floor(m/60);
  return h? `${h}h${m%60}m` : m? `${m}m${Math.round(x%60)}s` : Math.round(x)+'s'; }
function when(t){ return t? new Date(t*1000).toLocaleString(undefined,
  {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—'; }

async function history(){
  const h = await (await fetch('/api/history')).json();
  document.getElementById('hist').innerHTML = (h.runs||[]).map(r=>`
    <div class=run onclick="detail(${r.id})">
      <span><b>${r.profile}</b> <span class=s>${r.checkpoint||''}</span></span>
      <span class=t>${when(r.started_at)}</span>
      <span class=s>${r.n_req||0} req · ${(r.tokens||0).toLocaleString()} tok${r.avg_tps?` · ${r.avg_tps} t/s`:''}${r.n_err?` · <span class=err>${r.n_err} err</span>`:''}</span>
      <span class=t>${r.stopped_at? dur(r.duration_s) : 'running'}</span>
    </div>`).join('') || '<div class=s>no runs yet</div>';
}

async function detail(id){
  const d = await (await fetch('/api/run/'+id)).json();
  if(!d.run) return;
  const r=d.run, q=d.reqs||[], e=d.events||[];
  const tok=q.reduce((a,x)=>a+(x.out_tokens||0),0);
  const tps=q.length? (q.reduce((a,x)=>a+(x.tps||0),0)/q.length).toFixed(1):'—';
  const maxp=q.reduce((a,x)=>Math.max(a,x.prompt||0),0);
  const cachedPct=q.length? (q.reduce((a,x)=>a+(x.cached_pct||0),0)/q.length).toFixed(1):'—';
  document.getElementById('detname').textContent = ` · #${id} ${r.profile}`;
  document.getElementById('detailcard').style.display='';
  document.getElementById('detail').innerHTML = `<table>
    ${row('started', when(r.started_at))}${row('duration', dur(r.duration_s))}
    ${row('checkpoint', r.checkpoint||'—')}${row('engine', r.engine_version||r.image_tag||'—')}
    ${row('pool / slots', (r.pool||'—')+' / '+(r.slots||'—'))}
    ${row('weights / held', (r.weights_gib||'—')+' / '+(r.held_gib||'—')+' GiB')}
    ${row('pin rate', r.pin_gbs? r.pin_gbs+' GB/s':'—')}${row('pin stalls', r.pin_stalls??'—')}
    ${row('ready after', r.ready_s? r.ready_s+' s':'—')}
    ${row('requests', q.length)}${row('output tokens', tok.toLocaleString())}
    ${row('mean decode', tps+' t/s')}${row('largest prompt', maxp.toLocaleString())}
    ${row('mean cache hit', cachedPct+'%')}${row('events', e.length)}
    </table>` +
    (e.length? `<pre style="max-height:22vh;margin-top:10px">${e.slice(0,40).map(x=>
      `[${x.kind}] ${x.detail}`).join('\n').replace(/</g,'&lt;')}</pre>`:'');
}

tick(); logs(); history();
setInterval(history, 10000);
setInterval(tick, 3000);
setInterval(logs, 1000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    script: Script
    logdir: Path
    port: int

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype="application/json"):
        body = (obj if isinstance(obj, bytes)
                else json.dumps(obj).encode() if ctype == "application/json"
                else obj.encode())
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # the page and the API are generated fresh each time; never let a
        # browser serve a cached copy after the script has been updated
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self._send(PAGE, ctype="text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(self.status())
        if self.path == "/api/history":
            return self._send({"runs": HIST.runs() if HIST else []})
        if self.path.startswith("/api/run/"):
            try:
                rid = int(self.path.rsplit("/", 1)[1])
            except ValueError:
                return self._send({"error": "bad id"}, 400)
            return self._send(HIST.run(rid) if HIST else {})
        if self.path.startswith("/api/logs"):
            off = 0
            if "off=" in self.path:
                try:
                    off = int(self.path.split("off=")[1].split("&")[0])
                except ValueError:
                    off = 0
            return self._send(self.logs(off))
        self._send({"error": "not found"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/start":
            name = body.get("profile", "")
            if name not in [p["name"] for p in self.script.profiles]:
                return self._send({"ok": False, "error": "unknown profile"}, 400)
            ok, msg = start(self.script, name, self.logdir)
            return self._send({"ok": ok, "error": None if ok else msg,
                               "logfile": msg if ok else None})
        if self.path == "/api/stop":
            ok, msg = stop(self.script)
            return self._send({"ok": ok, "message": msg})
        self._send({"error": "not found"}, 404)

    # ---- data
    def status(self) -> dict:
        port = self.script.vars.get("PORT", "8731")
        base = f"http://127.0.0.1:{port}"
        c = container(self.script)
        # Only probe the API when something is serving, so a stopped box does
        # not pay three connection timeouts on every poll.
        health = models = cache = None
        if c:
            health = http_json(base + "/health")
            models = http_json(base + "/v1/models")
            cache = http_json(base + "/cache")
        profs = []
        for p in self.script.profiles:
            missing = [r for r in self.script.requirements(p) if not Path(r).exists()]
            profs.append({**{k: p[k] for k in
                             ("name", "checkpoint", "pool", "slots", "vision", "npu")},
                          "missing": [Path(m).name for m in missing]})
        with LOCK:
            launching = bool(STATE["proc"] and STATE["proc"].poll() is None)
            prof, lf = STATE["profile"], STATE["logfile"]
        return {
            "container": c,
            "launching": launching,
            "profile": prof,
            "logfile": lf,
            "health": health is not None,
            "models": [m["id"] for m in (models or {}).get("data", [])],
            "cache": cache or {},
            "resources": resources(),
            "profiles": profs,
            "versions": versions(self.script, (c or {}).get("image")),
        }

    def logs(self, off: int) -> dict:
        with LOCK:
            lf = STATE["logfile"]
        if not lf or not Path(lf).exists():
            return {"text": "", "off": 0}
        size = Path(lf).stat().st_size
        if off > size:
            off = 0
        with open(lf, "rb") as fh:
            fh.seek(off)
            data = fh.read(200_000)
        return {"text": data.decode("utf-8", "replace"), "off": off + len(data)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default="./halogen.sh")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8780)
    ap.add_argument("--logdir", default="~/.halogen-ui/logs")
    ap.add_argument("--db", default="~/.halogen-ui/history.db")
    a = ap.parse_args()

    sp = Path(a.script).expanduser().resolve()
    if not sp.exists():
        raise SystemExit(f"no such script: {sp}")
    global HIST
    HIST = History(Path(a.db).expanduser())
    Handler.script = Script(sp)
    Handler.logdir = Path(a.logdir).expanduser()
    Handler.port = a.port

    print(f"halogen-ui on http://{a.host}:{a.port}  (script {sp})")
    print(f"  profiles: {', '.join(p['name'] for p in Handler.script.profiles)}")
    print(f"  history:  {Path(a.db).expanduser()}")
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"  WARNING: bound to {a.host}, so it is reachable from the network. "
              f"This UI starts and stops containers and has no authentication.")

    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True

    def shutdown(signum=None, frame=None):
        # The container is deliberately left running: this is a console, not a
        # supervisor. The open run row is closed so history shows no phantom.
        print("\nshutting down (the halogen container is left running)", flush=True)
        with LOCK:
            if STATE.get("stop_evt"):
                STATE["stop_evt"].set()
            if HIST and STATE.get("run_id"):
                time.sleep(1.0)              # let the watcher drain the log tail
                HIST.end(STATE["run_id"], "ui exited, server left running")
        try:
            if HIST:
                HIST.db.close()
        except Exception:
            pass
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        shutdown()
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
