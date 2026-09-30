#!/usr/bin/env python3
"""
halogen-watch — tell you exactly when to /compact.

Follows the halogen container log, reconstructs each conversation from the
prompt-cache chain, and warns before prompt + max_tokens hits HALOGEN_CTX.

    ./halogen-watch.py                    # auto-detect container, follow
    ./halogen-watch.py -c 2abe978f895c    # explicit container
    ./halogen-watch.py --stdin < log.txt  # replay a saved log
    ./halogen-watch.py --quiet            # only NOTICE and above

Exit with Ctrl-C. Sends a desktop notification (notify-send) on WARN/CRIT.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections import deque

# --------------------------------------------------------------------- parse

# serve_api: mtp 285 tok in 6.43s = 44.30 t/s | ... | prompt 224397 (223868
# cached, 99.8%), prefill 1.20s (529 new) | ... | pool 524288/524288 100% |
# max_tokens clamped 32768 -> 32371 | think on
RE_PROMPT = re.compile(r"\bprompt (\d+)(?:\s*\((\d+) cached)?")
RE_POOL = re.compile(r"\bpool (\d+)/(\d+)")
RE_CLAMP = re.compile(r"max_tokens clamped (\d+) -> (\d+)")
RE_CANNOT_GROW = re.compile(r"cannot grow|nothing holds a move")
RE_CTXFAIL = re.compile(r"does not fit: prompt is (\d+) tokens")
RE_GEN = re.compile(r"generated (\d+) tokens")

C = {
    "ok": "\033[32m", "notice": "\033[36m", "warn": "\033[33m",
    "crit": "\033[31m", "dim": "\033[2m", "bold": "\033[1m", "off": "\033[0m",
}


def notify(title, body, urgency="normal"):
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "-u", urgency, title, body],
                       check=False)


def health(port):
    """Read ctx and caps from the live server; fall back to defaults."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                    timeout=3) as r:
            h = json.load(r)
        return (int(h.get("ctx") or h.get("max_ctx") or 262144),
                int(h.get("max_tokens_cap") or 65536),
                int(h.get("max_tokens_default") or 16384))
    except Exception:
        return 262144, 65536, 16384


def find_container():
    try:
        out = subprocess.run(
            ["podman", "ps", "--format", "{{.ID}} {{.Image}}"],
            capture_output=True, text=True, check=True).stdout
    except Exception:
        return None
    for line in out.splitlines():
        cid, _, image = line.partition(" ")
        if "halogen" in image:
            return cid
    return None


# ----------------------------------------------------------------- lineages

class Lineages:
    """
    Group requests into conversations using the prompt cache chain: a request
    reporting `prompt N (M cached)` continues whichever lineage last ended at
    about M tokens. Subagents therefore separate from the parent naturally.
    """

    def __init__(self, tol=64):
        self.tol = tol
        self.by_tip = {}      # tip size -> lineage id
        self.lin = {}         # id -> {"sizes": deque, "last": ts}
        self.next_id = 1

    def add(self, total, cached):
        lid = None
        if cached:
            best, bestd = None, self.tol + 1
            for tip, cand in self.by_tip.items():
                d = abs(tip - cached)
                if d < bestd:
                    best, bestd = cand, d
            if best is not None and bestd <= self.tol:
                lid = best
        if lid is None:
            lid = self.next_id
            self.next_id += 1
            self.lin[lid] = {"sizes": deque(maxlen=12), "last": 0.0}
        self.by_tip = {t: c for t, c in self.by_tip.items() if c != lid}
        self.by_tip[total] = lid
        rec = self.lin[lid]
        rec["sizes"].append(total)
        rec["last"] = time.time()
        return lid, rec

    @staticmethod
    def growth(sizes):
        """Median per-turn growth over the recent window."""
        if len(sizes) < 3:
            return None
        s = list(sizes)
        deltas = sorted(b - a for a, b in zip(s, s[1:]) if b > a)
        if not deltas:
            return None
        return deltas[len(deltas) // 2]


# -------------------------------------------------------------------- report

def level(used_frac, clamped):
    if clamped or used_frac >= 0.88:
        return "crit"
    if used_frac >= 0.75:
        return "warn"
    if used_frac >= 0.60:
        return "notice"
    return "ok"


ORDER = {"ok": 0, "notice": 1, "warn": 2, "crit": 3}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--container")
    ap.add_argument("-p", "--port", type=int, default=8731)
    ap.add_argument("--budget", type=int, default=0,
                    help="client max_tokens; default = server max_tokens_default")
    ap.add_argument("--stdin", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    args = ap.parse_args()

    ctx, cap, default_budget = health(args.port)
    budget = args.budget or default_budget
    print(f"{C['bold']}halogen-watch{C['off']}  ctx={ctx}  cap={cap}  "
          f"assumed client budget={budget}")
    print(f"{C['dim']}compact when headroom approaches zero "
          f"(headroom = ctx - prompt - budget){C['off']}\n")

    if args.stdin:
        stream = sys.stdin
        proc = None
    else:
        cid = args.container or find_container()
        if not cid:
            sys.exit("no halogen container found; pass -c <id>")
        print(f"{C['dim']}following container {cid}{C['off']}\n")
        proc = subprocess.Popen(
            ["podman", "logs", "-f", "--tail", "200", cid],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            bufsize=1)
        stream = proc.stdout

    lins = Lineages()
    last_level = {}
    pool_seen = None

    try:
        for line in stream:
            m = RE_CTXFAIL.search(line)
            if m:
                p = int(m.group(1))
                msg = (f"REJECTED: prompt {p:,} + budget leaves no room "
                       f"(ctx {ctx:,}). /compact now.")
                print(f"{C['crit']}{C['bold']}✘ {msg}{C['off']}")
                if not args.no_notify:
                    notify("halogen: request rejected", msg, "critical")
                continue

            mp = RE_POOL.search(line)
            if mp:
                pool_seen = (int(mp.group(1)), int(mp.group(2)))

            grow = bool(RE_CANNOT_GROW.search(line))
            mc = RE_CLAMP.search(line)
            if grow and not RE_PROMPT.search(line):
                print(f"{C['warn']}⚠ pool region cannot grow"
                      f"{' — max_tokens ' + mc.group(1) + ' -> ' + mc.group(2) if mc else ''}"
                      f"{C['off']}")
                continue

            m = RE_PROMPT.search(line)
            if not m or "prefill" not in line:
                continue
            total = int(m.group(1))
            cached = int(m.group(2)) if m.group(2) else 0

            lid, rec = lins.add(total, cached)
            headroom = ctx - total - budget
            frac = (total + budget) / ctx
            lv = level(frac, bool(mc))

            g = Lineages.growth(rec["sizes"])
            turns = f"~{headroom // g} turns" if g and g > 0 and headroom > 0 else "?"

            bar_w = 28
            filled = min(bar_w, int(frac * bar_w))
            bar = "█" * filled + "·" * (bar_w - filled)

            if not (args.quiet and lv == "ok"):
                extra = ""
                if mc:
                    extra += f"  {C['warn']}clamped {mc.group(1)}→{mc.group(2)}{C['off']}"
                if pool_seen and pool_seen[0] >= pool_seen[1]:
                    extra += f"  {C['warn']}pool FULL{C['off']}"
                print(f"{C[lv]}[{lv.upper():6}]{C['off']} conv{lid}  "
                      f"{bar} {frac*100:5.1f}%  "
                      f"prompt {total:>7,}  headroom {headroom:>7,}  "
                      f"{C['dim']}{turns}{C['off']}{extra}")

            if ORDER[lv] >= ORDER["warn"] and ORDER[lv] > ORDER.get(last_level.get(lid), "ok" and 0):
                if not args.no_notify:
                    notify(
                        f"halogen: conversation {lid} at {frac*100:.0f}%",
                        f"prompt {total:,} of {ctx:,}. {turns} left — /compact soon.",
                        "critical" if lv == "crit" else "normal")
            last_level[lid] = lv
    except KeyboardInterrupt:
        pass
    finally:
        if proc:
            proc.terminate()


if __name__ == "__main__":
    main()
