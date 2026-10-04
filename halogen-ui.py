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
import shutil
import signal
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
}
LOCK = threading.Lock()


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
                  "--format", "{{.ID}}\t{{.Image}}\t{{.Status}}\t{{.Names}}"])
    if rc != 0:
        return None
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            return {"id": parts[0], "image": parts[1],
                    "status": parts[2], "name": parts[3]}
    return None


def http_json(url: str, timeout: float = 2):
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
    gpu = {}
    if shutil.which("rocm-smi"):
        rc, out = sh(["rocm-smi", "--showmeminfo", "vram", "--showuse", "--csv"], 6)
        if rc == 0:
            for line in out.splitlines():
                if "," in line and line[0].isalpha() is False:
                    pass
            gpu["raw"] = out.strip().splitlines()[-3:]
    load = os.getloadavg()
    return {"mem_mib": mem, "order9_blocks": frag,
            "load": [round(x, 2) for x in load], "gpu": gpu}


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
        STATE.update(proc=proc, profile=profile, started=time.time(), logfile=str(lf))
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
        STATE.update(proc=None, profile=None, started=None)
    return True, "; ".join(msgs) or "nothing was running"


# ------------------------------------------------------------------- http
PAGE = """<!doctype html><meta charset=utf-8><title>halogen</title>
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
</style>
<header>
  <h1>halogen</h1>
  <span id=state class="pill down">checking…</span>
  <span id=health class="pill"></span>
  <span style=flex:1></span>
  <button class=stop id=stopbtn disabled>Stop</button>
</header>
<main>
 <div>
  <div class=card><h2>Profiles</h2><div id=profiles></div></div>
  <div class=card><h2>Host</h2><table id=res></table></div>
  <div class=card><h2>Server</h2><table id=srv></table></div>
 </div>
 <div class=card><h2>Log <span id=logname style="color:#55607a"></span></h2><pre id=log>—</pre></div>
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
tick(); logs();
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
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self._send(PAGE, ctype="text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(self.status())
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
            "container": container(self.script),
            "launching": launching,
            "profile": prof,
            "logfile": lf,
            "health": health is not None,
            "models": [m["id"] for m in (models or {}).get("data", [])],
            "cache": cache or {},
            "resources": resources(),
            "profiles": profs,
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
    a = ap.parse_args()

    sp = Path(a.script).expanduser().resolve()
    if not sp.exists():
        raise SystemExit(f"no such script: {sp}")
    Handler.script = Script(sp)
    Handler.logdir = Path(a.logdir).expanduser()
    Handler.port = a.port

    print(f"halogen-ui on http://{a.host}:{a.port}  (script {sp})")
    print(f"  profiles: {', '.join(p['name'] for p in Handler.script.profiles)}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
