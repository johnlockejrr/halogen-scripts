# halogen-scripts

Scripts for running [halogen-flash-server](https://github.com/peonist-ai/halogen-flash-server)
(Qwen3.8-Flash-Next) as a local coding-agent backend, and for pointing Claude Code at it.

Built and tuned on a **GMKtec EVO-X2 (AMD Strix Halo, 128 GB unified memory)** running
**Pop!_OS 24.04 / COSMIC**, with rootless podman. Nothing here is Strix-specific in
principle, but the memory numbers are sized for a 128 GB unified-memory box.

| script | what it does |
|---|---|
| `halogen.sh` | pull the latest image, prune old ones, run the server in one of several tuned profiles |
| `halogen-watch.py` | follow the server log and tell you when a conversation is about to run out of context |
| `claude-box.sh` + `Containerfile.claude` | run Claude Code in a podman sandbox against the server |

---

## Quick start

```bash
# 1. serve the model, tuned for long agentic coding sessions
./halogen.sh run-optimal

# 2. in another terminal, point Claude Code at it
source ./claude-env.sh   # or export the vars below yourself
claude

# 3. optionally, watch how close you are to the context ceiling
./halogen-watch.py
```

---

## `halogen.sh`

```
usage: ./halogen.sh [run|run-optimal|run-optimal-vision|run-vision|run-uncensored|clean|latest]
                    (VERSION=x.y.z to pin)
```

- **`run`** — stock defaults, nothing tuned.
- **`run-optimal`** — the profile for long coding sessions. This is the one to use.
- **`run-optimal-vision`** — same, plus the vision tower, with a smaller KV pool to make room for it.
- **`run-vision`** — stock defaults plus vision.
- **`run-uncensored`** — alternate checkpoint.
- **`clean`** — remove all but the current image tag.
- **`latest`** — print the newest published tag.

The tag is resolved from the GHCR registry each run, falling back to the newest local
image if the registry is unreachable. `VERSION=0.15.1 ./halogen.sh run-optimal` pins it.

The script runs `vm.compact_memory` before starting. This is not cosmetic: on a fragmented
host, reserving the KV pool can take tens of minutes at 100% of one core and look like a
hang. With compaction first, startup is about 3 seconds.

### What `run-optimal` sets, and why

| flag | value | reason |
|---|---|---|
| `HALOGEN_CTX` | `262144` | native window; no YaRN, so no quality cost on short prompts |
| `HALOGEN_KV_POOL_POSITIONS` | `786432` | 3 full-length conversations; at `524288` a parent + subagents fills the pool and `max_tokens` gets clamped every turn |
| `HALOGEN_KV_SLOTS` | `4` | agents fan out into parallel subagent calls |
| `HALOGEN_MAX_TOK` | `16384` | ~9% slower prefill, gives back ~9 GiB of working memory and halves pauses for other conversations |
| `HALOGEN_HOST_RESERVE_GIB` | `24` | protects page cache for the 47.7 GiB n-gram lookup table, which is read from disk and never held in RAM |
| `HALOGEN_CACHE_BRANCHES` | `3` | subagent fan-out; at the default of 2 the parent re-reads its whole history every turn |
| `HALOGEN_COMPOSABLE_CONTEXT` | `1` | retains messages ≥2048 tokens and reuses them at any later offset — exactly the shape of a harness compaction |
| `HALOGEN_TEMPERATURE` | `1.0` | the model card's sampling setting; agent harnesses send no sampling fields, so without this every turn runs greedy |
| `HALOGEN_MAX_TOKENS_DEFAULT` | `16384` | thinking and the answer share one budget |
| `HALOGEN_MAX_TOKENS_CAP` | `32768` | a request reserves prompt + max_tokens on admission; a huge cap lets one request eat the pool |
| `HALOGEN_REASONING_EFFORT` | `xhigh` | the card's guidance for multi-turn agentic work |
| `HALOGEN_MAX_THINKING_TOKENS` | `8192` | bounds thinking explicitly instead of relying on the answer-room floor |
| `HALOGEN_ENGINE_WATCHDOG_S` | `0` | **required** given `--rm` with no restart policy — otherwise the watchdog kills the server and nothing brings it back |
| `HALOGEN_CACHE_DIR` + `DISK_GIB=128` | | prompt cache survives restarts, ~27 KiB/token |
| `HALOGEN_KEEPALIVE_TIMEOUT` | `900` | agents idle between turns; short keep-alives cause `SocketError: terminated` |

Deliberately **not** set:

- `HALOGEN_PREFILL_CHUNK` — the default 32768 is the fastest measured value; lowering it is a pure loss.
- `HALOGEN_ADMIT_CHUNK` — only pays with several conversations generating concurrently, and it gives up the byte-identity property.
- `HALOGEN_MTP_DEPTH=3` — faster for raw code generation, 5–8% *slower* on agent traffic.
- `HALOGEN_ROPE_YARN` — extends context past 262k, but it rescales RoPE for **every** request including short ones. Only worth it if you genuinely send single prompts past 262k.

### Vision

`run-optimal-vision` adds `HALOGEN_VISION_TOWER=1` and drops the pool to `524288` to make
room for the tower (~1.5 GiB). It also caps images at 1920×1080 — a 1440p image costs about
25 s against 12 s at 1080p, and text at 12 pt and up reads correctly at either.

Two things to know: image requests bypass composable context, and switching between the
vision and non-vision profiles changes the engine's configuration fingerprint, so the
on-disk prompt cache from the other profile is pruned on start. Use separate `CACHE_DIR`s
if you alternate often.

---

## Claude Code setup

```bash
export ANTHROPIC_BASE_URL="http://127.0.0.1:8731"
export ANTHROPIC_AUTH_TOKEN="local"
unset ANTHROPIC_API_KEY

# tell Claude Code the real window — without these it assumes a Sonnet-sized
# one and compacts far too late, or not at all
export CLAUDE_CODE_MAX_CONTEXT_TOKENS=250000
export CLAUDE_CODE_AUTO_COMPACT_WINDOW=220000

export CLAUDE_CODE_MAX_OUTPUT_TOKENS=16384
export API_TIMEOUT_MS=1800000
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1

claude
```

Halogen serves the Anthropic Messages API directly (including `/v1/messages/count_tokens`),
so **no proxy is needed**. LiteLLM works if you want one, but it adds a `response_format`
conflict — halogen decodes structured output greedily and refuses it under sampling — and
its token counting falls back to a mismatched tokenizer.

The two context variables are the difference between "auto-compaction works" and "the
session dies with `max_tokens N does not fit`". `CLAUDE_CODE_MAX_CONTEXT_TOKENS` requires
Claude Code ≥ 2.1.98.

`API_TIMEOUT_MS` matters because a cold 33k prompt takes ~23 s to prefill at ~1,450 tok/s,
and the default client timeout hangs up before the first token.

---

## `halogen-watch.py`

```bash
./halogen-watch.py                 # auto-detect the container, follow
./halogen-watch.py --quiet         # NOTICE and above only
./halogen-watch.py --budget 32768  # if your client asks for more output
./halogen-watch.py --stdin < saved.log
```

```
[OK    ] conv146  ██████████████··  52.3%  prompt 120,722  headroom 125,038  ~159 turns
```

Each line is one request. Conversations are reconstructed from the prompt-cache chain
(`prompt N (M cached)` links a request to whichever conversation last ended at M), so
subagents separate from the parent automatically. `headroom` is
`ctx − prompt − output_budget` — the exact quantity in the error you get when it runs out.

Levels: OK < 60%, NOTICE ≥ 60%, WARN ≥ 75%, CRIT ≥ 88% or on any `max_tokens clamped`.
WARN and CRIT also fire `notify-send`.

Short-lived subagents show `?` for the turn estimate — the growth median needs three
samples in one lineage.

---

## `claude-box.sh`

Runs Claude Code in a podman container with `--dangerously-skip-permissions`, so the agent
works unattended inside the sandbox without touching your SSH keys, gpg keys, shell
history, or anything outside the mounted project.

```bash
./claude-box.sh --build          # once
./claude-box.sh                  # sandbox $PWD
./claude-box.sh /path/to/repo
./claude-box.sh --shell
```

- `--userns=keep-id` so files written into the repo belong to you
- `--memory=6g` so a runaway build can't starve the pinned model weights
- a persistent home volume, so `/resume` and installed tooling survive
- reaches the server via `host.containers.internal`

It does **not** restrict outbound network — the agent needs it for package installs. For
egress control, a micro-VM sandbox such as
[gondolin](https://github.com/earendil-works/gondolin) is the better tool.

Pair it with a git worktree for long refactors:

```bash
git worktree add ../proj-refactor -b refactor/thing
cd ../proj-refactor && claude-box.sh
```

---

## Things that cost time to learn

- **Compact memory before starting.** Otherwise pool reservation can stall for a very long
  time and look like a hang.
- **The "N GiB of host RAM is in use" warning is usually noise.** It is computed before the
  GTT check and before the iGPU carve-out is known. The `MemAvailable now ...` line on the
  next line is what actually governs.
- **`free -g` lies while the server runs.** The kernel counts the pinned weights as
  reclaimable file cache; the startup log says by how much.
- **Pin `transformers` if you use a client that needs it.** v5 miscomputes mBART position
  ids and the engine indexes past its positional table.
- **A short keep-alive breaks agents**, because POST is not idempotent and the client
  cannot transparently retry.
- **`max_tokens` covers thinking *and* the answer.** Below ~1,200 tokens of budget a
  request effectively gets no thinking at all.
- **Greedy decode at long context can loop inside the think block.** Run sampled.
- **Watch for `cannot grow` and `max_tokens clamped`** in the server log. They appear
  several turns before a hard failure.

## License

Apache License 2.0
