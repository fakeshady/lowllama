# Phase 2 Evidence Checklist (owner: Shafin)

How the team proves that a **physical** helper laptop really ran part of the model. Mark every row
`PASS`, `FAIL` or `NOT RUN`. Never write PASS for something that was not observed in this session's logs.
The session steps are in `docs/PHASE2_DEMO_RUNBOOK.md`; the release gate is `docs/ENGINE_COMPLETION_GATE.md`.

## What counts as proof

Five different things, always labelled separately:

| Signal | Means | Proof of inference? |
|---|---|---|
| `/nodes` shows `online` | heartbeat arrived within 10 s | No |
| `/nodes` shows `rpc_ready: true` | the helper's own TCP self-check passed | No |
| `--list-devices` shows `RPCn: <helper>:50052` | llama.cpp RPC answers with a compatible build ("RPC-compatible") | No |
| Master log: layers on `RPCn` **and** nonzero `RPCn` model buffer | model **weights** were sent to that helper | Half (allocation) |
| Helper log: `[graph_compute]` count rises across **one new** chat | that helper computed for that request | Half (compute) |

A helper **participated** only when both halves hold for the same running session. Two helpers computed the
**same prompt** only when both compute windows contain the same chat time (`-Mode SamePrompt`).
KV-cache or compute buffers on `RPCn` without a model buffer do not count. Warmup calls do not count.

## Machine map (fill in locally; placeholders only in anything shared or committed)

| Role | Person | Hostname | Tailscale IPv4 | node_id | RPC index |
|---|---|---|---|---|---|
| Master | Itmam | `<MASTER-HOST>` | `100.x.x.M` | (not a worker) | CUDA0 |
| Helper 1 | Rodel | `<HELPER1-HOST>` | `100.x.x.A` | `node-xxxxxxxx` | RPC0 |
| Helper 2 | Shafin | `<HELPER2-HOST>` | `100.x.x.B` | `node-yyyyyyyy` | RPC1 |

The order of `node_ids` in `/start` (or of IPs in `Check-RPC-Workers.ps1` / `--rpc`) **is** the RPC0, RPC1 order.
RPC0 and RPC1 must be two different hosts, and neither may be the master or loopback; the scripts enforce this.

## Log paths

| Where | File | Written by |
|---|---|---|
| Helper | `.runtime\logs\helper-rpc.log` | `ggml-rpc-server ... 2>&1 \| Tee-Object` with `$env:GGML_RPC_DEBUG='1'` |
| Master | `.runtime\logs\master-remote.log` | manual `llama-server ... -lv 5 2>&1 \| Tee-Object` (runbook sections 6-7) |
| Master | `.runtime\logs\api-launcher-<time>-<id>.log` | the launcher, one new file per `/start` (path printed in the coordinator terminal) |
| Master | `.runtime\logs\rpc-preflight-*.txt` | `scripts\Check-RPC-Workers.ps1` |
| Master | `.runtime\logs\chat-*.json` | `scripts\Test-Chat.ps1` |
| Both | `.runtime\logs\evidence\` | `scripts\Collect-Inference-Evidence.ps1` |

PowerShell 5.1 `Tee-Object` writes UTF-16; the launcher writes UTF-8; the script reads both. All of `.runtime\` is
git-ignored. Log patterns follow the format strings inside the pinned b12110 binaries (`llama.dll`, `ggml-rpc.dll`):
`load_tensors: layer %3d assigned to device %s`, `%s: %12s model buffer size = %8.2f MiB` and `using device %s (%s)`.
RPC weight buffers may be named `RPCn[host:port]` or `RPC[host:port]`; the second form is mapped to `RPCn` through the
`using device RPCn (host:port)` line, and anything unmapped is FAIL, never guessed. These rules are executed against
synthetic fixtures in `tests/test_engine_proof_contract.py`; a parse of a **real** master log is still NOT RUN.

## Evidence script rules (what makes a false PASS hard)

- **Master** (`-Mode Master`): PASS needs, on the last model load in the log, at least `-ExpectRpc` RPC devices with
  layers > 0 **and** a model buffer > 0 MiB, on that many distinct hosts. Loopback is `LOOPBACK`, the master's own
  Tailscale address is `SELF`, an RPC device without a known endpoint is `FAIL`. A log last written more than
  120 minutes ago is refused as `STALE`. `-ApiLog` uses the newest `api-launcher-*.log` and prints its name.
- **Helper** (`-Mode HelperSnapshot` / `HelperCompare`, always with the same `-Session`): each snapshot records the
  graph count, log size, a hash of the log's first bytes and the ggml-rpc-server process. Compare gives `PASS` only for
  a positive delta in one window of at most 10 minutes on the same log and the same process. A restart, a rewritten or
  shrunk log, out-of-order snapshots or a long window give `AMBIGUOUS` (exit 2): redo around one chat. Zero delta is
  `FAIL`. Compared snapshots are renamed `*.used.json` and can never be counted again; a new `before` deletes an old
  `after` of the same session.
- **Same prompt** (`-Mode SamePrompt`): PASS only if at least two distinct helpers report PASS for the same session and
  the chat time (`CHAT SENT AT` from `Test-Api-Flow.ps1`, or noted before a manual chat) is inside every window, with
  5 s allowed for clock drift. This is a timing correlation from local logs, not a cryptographic attestation.

## Steps

See `docs/PHASE2_DEMO_RUNBOOK.md`: section 6 (one helper, G5), section 7 (two helpers, G6), section 8 (API, G7).
Each helper sends the team only its printed `... HELPER COMPUTE ...` line; the master sends only the
`MASTER ALLOCATION ...` and `SAME PROMPT PROOF ...` lines.

## Result matrix

| Gate | Check | Result | Evidence (sanitized) |
|---|---|---|---|
| P2-A | Shared demo model ID accepted by API and launcher | PASS | `/start` reads `launcher.MODEL_ALLOWLIST` (tested) |
| P2-A | Partial selection refused with reasons (all-or-nothing) | PASS | tested over HTTP, including one of two laptops going silent |
| P2-A | Unit and integration suites | PASS | see `docs/ENGINE_COMPLETION_GATE.md` G1 for the latest executed run |
| P2-B | API start/status/chat/stop with stand-in engine, incl. script cleanup paths | PASS | `tests/test_backend_integration.py` (mocked engine; not inference) |
| P2-C | Preflight (TCP + list-devices + CUDA0), one helper | NOT RUN | |
| P2-C | Master model-weight allocation on RPC0 | NOT RUN | |
| P2-C | Helper 1 fresh graph_compute delta | NOT RUN | |
| P2-C | Chat JSON with an answer | NOT RUN | |
| P2-D | RPC0/RPC1 are two different physical hosts | NOT RUN | |
| P2-D | Model-weight allocation on RPC0 **and** RPC1 | NOT RUN | |
| P2-D | Fresh graph_compute delta on **both** helpers, same request | NOT RUN | |
| P2-E | `/start` -> `ready` -> `/chat` -> `/stop` with real helpers | NOT RUN | |
| P2-E | Allocation from the API launch log + same-prompt deltas | NOT RUN | |

## Known limitations (do not hide them)

- Reported free RAM (helpers report about 2-2.5 GB free) is a hint, not a guarantee, and RAM is **not** pooled.
  Only the 0.5B model is in scope. 7B/8B are not claimed.
- The launcher hardcodes `CUDA0`. A master without an NVIDIA GPU fails at startup (clear error, no fallback).
- More helpers can be slower than the master GPU alone for this tiny model. Phase 2 claims participation, not speed.
- A graph-count delta correlates a helper with one request under controlled conditions (one chat, short window). It
  does not measure how much of the work the helper did.
- `tests/test_launcher.py` (Itmam) still writes `api-launcher-*.log` files into the real `.runtime\logs` when run.
  They contain fake lines and cannot pass `-Mode Master`, but delete or move them before a session so `-ApiLog`
  finds this run's log. Fix requested from Itmam (patch `LAUNCH_LOG_DIRECTORY` in that test file).
