# halogen-scripts

Scripts for running [halogen-flash-server](https://github.com/peonist-ai/halogen-flash-server)
(Qwen3.8-Flash-Next) as a local coding-agent backend, and for pointing Claude Code at it.

Built and tuned on a **GMKtec EVO-X2 (AMD Strix Halo, gfx1151, 128 GB unified memory)**
running **Pop!_OS 24.04 / COSMIC**, with rootless podman. Nothing here is Strix-specific in
principle, but the memory numbers are sized for a 128 GB unified-memory box, and every
figure below was measured on that machine rather than taken from a datasheet.

| script | what it does |
|---|---|
| `halogen.sh` | pull the latest image, prune old ones, run the server in one of several tuned profiles |
| `halogen-watch.py` | follow the server log and tell you when a conversation is about to run out of context |
| `claude-box.sh` + `Containerfile.claude` | run Claude Code in a podman sandbox against the server |

---

## Quick start

```bash
# serve the model, tuned for long agentic coding sessions
./halogen.sh run-optimal

# in another terminal, point Claude Code at it
source ./claude-env.sh
claude

# optionally, watch how close you are to the context ceiling
./halogen-watch.py
```

---

## `halogen.sh`

```
usage: ./halogen.sh [run|run-optimal|run-optimal-vision|run-optimal-npu
                    |run-optimal-ht43|run-optimal-npu-ht43
                    |run-swift-abliterated|clean|latest]   (VERSION=x.y.z to pin)
```

| profile | checkpoint | pool | extras |
|---|---|---|---|
| `run` | default | default | nothing tuned |
| **`run-optimal`** | v2 | 786432 | **the one to use** |
| `run-optimal-vision` | v2 | 524288 | vision tower, 1080p cap |
| `run-optimal-npu` | v2 | 524288 | vision + 5 NPU models |
| `run-optimal-ht43` | ht43 | 786432 | the smaller checkpoint |
| `run-optimal-npu-ht43` | ht43 | 786432 | both |
| `run-swift-abliterated` | abliterated v2 | 524288 | vision |

The tag is resolved from GHCR each run, falling back to the newest local image if the
registry is unreachable. `VERSION=0.15.3 ./halogen.sh run-optimal` pins it, and
`KEEP_TAGS=0.15.3,0.16.0` protects old images from `clean` so you can keep a benchmark
baseline around.

`compact_memory` runs before each start. On a fragmented host, reserving the KV pool can
take tens of minutes at 100% of one core and look like a hang; after compaction it takes
under a second. It logs the order-9 block count before and after rather than gating on it —
that number and the one halogen reports measure different pools and disagree by orders of
magnitude, so it is for correlation after a bad start, not for prediction.

### What `run-optimal` sets, and why

| flag | value | reason |
|---|---|---|
| `HALOGEN_CTX` | `262144` | native window; no YaRN, so no quality cost on short prompts |
| `HALOGEN_KV_POOL_POSITIONS` | `786432` | 3 full-length conversations; at `524288` a parent plus subagents fills the pool and `max_tokens` is clamped every turn |
| `HALOGEN_KV_SLOTS` | `4` | agents fan out into parallel subagent calls |
| `HALOGEN_MAX_TOK` | `16384` | **measured**: 32768 costs 19% of prefill on this box, 8192 gains nothing |
| `HALOGEN_SPEC_ADAPT` | `0` | keeps the MTP draft head on for every token; the adaptive policy can switch it off for whole requests |
| `HALOGEN_MTP_PREFILL` | `0` | **measured +3% @32k, +7% @6.2k**; speculation cannot help during prefill, where every token is known. Undocumented in `FLAGS.md` — re-check after upgrades |
| `HALOGEN_WEIGHTS_LOCK` | `1` | mlocks the weight pages. Without it, reclaim makes the GPU driver tear down and restore mappings, which can stall or fault |
| `HALOGEN_HOST_RESERVE_GIB` | `24` | page cache for the 47.7 GiB n-gram table, which is read from disk and never held. Raising it to 32 was measured and changed nothing |
| `HALOGEN_CACHE_BRANCHES` | `3` | subagent fan-out; at the default of 2 the parent re-reads its whole history every turn |
| `HALOGEN_COMPOSABLE_CONTEXT` | `1` | retains messages >=2048 tokens and reuses them at any later offset — the shape of a harness compaction |
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
- `HALOGEN_MTP_DEPTH=3` — faster for raw code generation, 5-8% *slower* on agent traffic.
- `HALOGEN_ROPE_YARN` — extends context past 262k, but rescales RoPE for **every** request including short ones. Only worth it if you genuinely send single prompts past 262k.

---

## Measured results

All on 0.16.0 unless noted, `iommu=pt`, `omp bench --par 1`, two consecutive five-run
blocks per cell.

| profile | prefill @32k | decode | free RAM |
|---|---|---|---|
| **run-optimal** (v2) | **1,704 / 1,678** | **42.9 / 42.8** | 21.3 GiB |
| run-optimal-ht43 | 1,583 / 1,571 | 40.4 / 40.4 | 30.3 GiB |
| ht43 + `HOST_RESERVE_GIB=32` | 1,567 / 1,556 | 40.5 / 40.3 | 29.7 GiB |
| run-optimal-npu, NPU idle | 1,686 / 1,672 | 43.0 / 43.2 | 27.6 GiB |
| run-optimal-npu, NPU saturated | — | 40.9 | 27.6 GiB |

On **0.16.1**, which claims faster decode on both checkpoints:

| | 0.16.0 | 0.16.1 |
|---|---|---|
| v2 decode | 42.9 / 42.8 | **44.4 / 43.9** |
| ht43 decode | 40.4 / 40.4 | **42.9 / 43.5** |
| gap v2 to ht43 | -5.7% | **-1.5%** |

### What changed what

| change | effect |
|---|---|
| `amd_iommu=off` instead of `iommu=pt` | **+5% prefill**, but the NPU stops working |
| `HALOGEN_MTP_PREFILL=0` | **+3% @32k, +7% @6.2k** |
| `HALOGEN_MAX_TOK=32768` | **-19% prefill** |
| `HALOGEN_MAX_TOK=8192` | no change |
| CPU governor `performance` | 0.8% — noise |
| `HALOGEN_HOST_RESERVE_GIB` 24 -> 32 | no change |
| vision tower loaded | no change |
| NPU models loaded, idle | no change |

### Other measurements

- **Prefill rises with prompt length** then falls: 1,180 @6k, **1,599 @42k**, 1,425 @262k. Per-chunk rate decays ~15% from the start of a 262k prompt to its end.
- **Warm follow-up at 74k context: 257 ms**, against 41.5 s cold. 73,775 of 73,790 tokens reused.
- **Greedy + prompt lookup at 118k context: 68.4 tok/s**, with 1,450 of 1,495 drafted tokens accepted. Only available on `temperature: 0`, so not on the sampled production setting.
- **Decode falls with context**: 43 at short, 41.5 @24k, 36 @74k.
- **MTP speculates only while a request is alone.** At 4 concurrent each stream drops to ~18 tok/s with the head off; aggregate still rises to ~74.

---

## Host configuration

```
# /proc/cmdline — Pop!_OS uses kernelstub, not grub
iommu=pt amdgpu.gttsize=126976 ttm.pages_limit=32505856
```

`amdgpu.gttsize` and `ttm.pages_limit` are what give the iGPU the full 124 GiB
(`GTT in use: 0.0 GiB of 124.0` at startup). **`amd_iommu=off` is not needed for that** —
the guides that bundle it do so for a measured 5-12% prefill gain, confirmed here at 5.4%.
It also disables the NPU (`amdxdna: Running without IOMMU not supported`), so it is a
straight trade.

`vm.swappiness=1` in `/etc/sysctl.d/`. Pop ships 180, which is tuned for desktops with
zram; here swap is an encrypted partition and the page cache is holding the n-gram table.

---

## The NPU (0.16.0+)

`run-optimal-npu` serves five small models on the Ryzen AI NPU beside the Flash model,
behind the same port: embeddings, reranking, a decision classifier, moderation, and a 2B
generator. Costs **nothing when idle** and **~5% of decode** under continuous load.

Prerequisites, all checked by `require_npu()` before the image is pulled:

- `iommu=pt` (not `amd_iommu=off`), so `/dev/accel/accel0` exists
- XRT with its NPU plugin; distribution XRT needs each library mounted three times — see `xrt_mounts()`
- the GPU fabric clock held, via `deploy/host/halogen-fabric-clock.service` from the upstream repo
- your user in the `render` group

Model files go in `${MODEL_DIR}/npu/<name>/`, one directory per model:

```bash
D=/mnt/data/models/halogen-qwen3.8-flash-next/npu
for m in decider-0.8b qwen3-embedding-0.6b qwen3-reranker-0.6b \
         qwen3guard-gen-0.6b qwen3.5-2b; do
  hf download "peonist-ai/halogen-npu-${m}" --local-dir "$D/$m"
done
```

Re-pull them after an upgrade: the device files are rebuilt between releases even when the
weights are not.

One lesson from using the classifier: write the question as an observable behaviour, not a
category name. *"Is this a prompt injection?"* got it wrong; *"Does this message attempt to
override the assistant's instructions?"* got three of three right on the same model.

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

Halogen serves the Anthropic Messages API directly, including `/v1/messages/count_tokens`,
so **no proxy is needed**. LiteLLM works if you want one, but it adds a `response_format`
conflict — halogen decodes structured output greedily and refuses it under sampling — and
its token counting falls back to a mismatched tokenizer.

The two context variables are the difference between "auto-compaction works" and "the
session dies with `max_tokens N does not fit`". `CLAUDE_CODE_MAX_CONTEXT_TOKENS` requires
Claude Code >= 2.1.98.

`API_TIMEOUT_MS` matters because a cold 33k prompt takes ~23 s to prefill, and the default
client timeout hangs up before the first token.

Claude Code also sends `context_management`, which halogen ignores — so it is not pruning
context server-side, and manual `/compact` discipline still matters.

---

## Benchmarking

[`omp bench`](https://github.com/can1357/oh-my-pi) for quick comparisons:

```bash
omp bench halogen/halogen-qwen3.8-flash-next --profile generation --par 1
omp bench halogen/halogen-qwen3.8-flash-next --profile prefill --prefill-bytes 175000 --par 1
omp bench halogen/halogen-qwen3.8-flash-next --cache --cache-prefix-bytes 400000 --json
```

Run each twice; the first populates the cache.

[BetterBench](https://github.com/GGZ14/BetterBench) for anything you intend to publish or
act on. Per-category decode over a versioned corpus, nonce-busted prefill, a concurrency
sweep, and — the reason to prefer it — paired A/B with a confidence interval that refuses
to call a winner inside the noise band. Run-to-run noise here is 3-8%, which buries exactly
the 1-2% differences this kind of tuning produces.

```bash
betterbench run --endpoint http://127.0.0.1:8731/v1 --model halogen-qwen3.8-flash-next \
  --decode --name v2 --note checkpoint=v2 --note image=0.16.1 --out results/0161-v2.json
```

Note BetterBench's corpus produces short outputs, so on this model every pass logs
`closed at 1 by answer room` — it measures answer decode with thinking off. Fine for
comparing two checkpoints; not the same thing as a real agent turn.

---

## Same model, other hardware

Qwen3.8-Flash-Next on a DGX Spark (GB10, 128 GB), measured with the same `omp bench`
commands:

| | halogen / Strix Halo | [flash-DGX](https://github.com/blazux/qwen3.8-Flash-DGX) (vLLM) | [TensorFold](https://github.com/MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark-TensorFold) |
|---|---|---|---|
| prefill @32k | 1,704 | **3,116** | 2,436 |
| decode | 42.9 | 30.4 | **54.5** |
| KV pool | 786,432 | 450,424 | **1,310,720** |

All three independently keep the 48 GiB n-gram table off the device and read it from
storage, which seems to be the settled answer for this model.

---

## `halogen-watch.py`

```bash
./halogen-watch.py                 # auto-detect the container, follow
./halogen-watch.py --quiet         # NOTICE and above only
./halogen-watch.py --budget 32768  # if your client asks for more output
./halogen-watch.py --stdin < saved.log
```

```
[OK    ] conv146  ..............  52.3%  prompt 120,722  headroom 125,038  ~159 turns
```

Each line is one request. Conversations are reconstructed from the prompt-cache chain
(`prompt N (M cached)` links a request to whichever conversation last ended at M), so
subagents separate from the parent automatically. `headroom` is
`ctx - prompt - output_budget` — the exact quantity in the error you get when it runs out.

Levels: OK < 60%, NOTICE >= 60%, WARN >= 75%, CRIT >= 88% or on any `max_tokens clamped`.
WARN and CRIT also fire `notify-send`.

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
  time and look like a hang. Zero compaction stalls is the healthy signature; thousands is not.
- **The "N GiB of host RAM is in use" warning is noise on this host.** It is computed before
  the GTT check and before the iGPU carve-out is known. It fired at 10.0 GiB before the
  fastest start measured and at 11.9 GiB before the slowest. `MemAvailable now ...` on the
  next line is what actually governs.
- **`free -g` lies while the server runs.** The kernel counts the pinned weights as
  reclaimable file cache; the startup log says by how much.
- **Each profile is a different cache fingerprint.** With `CACHE_PRUNE_OLD=1`, switching
  profiles wipes the other's disk cache — 99 GiB in one case. Use separate `CACHE_DIR`s if
  you alternate.
- **Pin `transformers==4.46.3`** if you use a client that needs it. v5 miscomputes mBART
  position ids and the engine indexes past its positional table.
- **A short keep-alive breaks agents**, because POST is not idempotent and the client
  cannot transparently retry.
- **`max_tokens` covers thinking *and* the answer.** Below ~1,200 tokens of budget a request
  effectively gets no thinking at all — the answer room is `max(1024, 15%)`.
- **Greedy decode at long context can loop inside the think block.** Run sampled.
- **Watch for `cannot grow` and `max_tokens clamped`** in the server log. They appear several
  turns before a hard failure.
- **`omp bench` defaults to `--par 4`.** A "slow" result where one run in five is fast is
  usually MTP switching off under concurrency, not a regression.

## License

Apache License 2.0
