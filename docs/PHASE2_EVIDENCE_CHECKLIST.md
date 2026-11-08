# Phase 2 Evidence Checklist (owner: Shafin)

How the team proves that a **physical** helper laptop really ran part of the model. Mark every row
`PASS`, `FAIL` or `NOT RUN`. Never write PASS for something that was not observed in this session's logs.

## What counts as proof

| Signal | Means | Proof of inference? |
|---|---|---|
| `/nodes` shows `online`, `rpc_ready: true` | heartbeat arrives; helper's own TCP self-check passed | No |
| TCP 50052 connects from the master | something listens there | No |
| `--list-devices` shows `RPCn: <helper>:50052` | llama.cpp RPC answers with a compatible build | No (only "RPC-compatible") |
| Master log: layers assigned to `RPCn` **and** nonzero `RPCn[...] model buffer size` | model **weights** were sent to that helper | Half (allocation) |
| Helper log: `[graph_compute]` count rises during a **new** chat request | that helper computed for that request | Half (compute) |

A helper **participated** only when both halves hold for the same running session.
KV-cache or compute buffers on `RPCn` without a model buffer do not count. Warmup calls do not count.

## Machine map (fill in locally; placeholders only in anything shared or committed)

| Role | Person | Hostname | Tailscale IPv4 | node_id | RPC index |
|---|---|---|---|---|---|
| Master | `<name>` | `<MASTER-HOST>` | `100.x.x.M` | (not a worker) | CUDA0 |
| Helper 1 | `<name>` | `<HELPER1-HOST>` | `100.x.x.A` | `node-xxxxxxxx` | RPC0 |
| Helper 2 | `<name>` | `<HELPER2-HOST>` | `100.x.x.B` | `node-yyyyyyyy` | RPC1 |

The order of `node_ids` in `/start` (or of IPs in `Check-RPC-Workers.ps1` / `--rpc`) **is** the RPC0, RPC1 order.
Check that the RPC0 and RPC1 IPs belong to two different hosts, and that neither is the master or `127.0.0.1`.

## Log paths

| Where | File | Written by |
|---|---|---|
| Helper | `.runtime\logs\helper-rpc.log` | `ggml-rpc-server ... 2>&1 \| Tee-Object` with `$env:GGML_RPC_DEBUG='1'` |
| Master | `.runtime\logs\master-remote.log` | manual `llama-server ... -lv 5 2>&1 \| Tee-Object` (README) |
| Master | `.runtime\logs\rpc-preflight-*.txt` | `scripts\Check-RPC-Workers.ps1` |
| Master | `.runtime\logs\chat-*.json` | `scripts\Test-Chat.ps1` |
| Both | `.runtime\logs\evidence\` | `scripts\Collect-Inference-Evidence.ps1` |

PowerShell 5.1 `Tee-Object` writes UTF-16; both scripts read that correctly. All of `.runtime\` is git-ignored.
The API launcher (`/start`) keeps llama-server output only in memory, so **allocation evidence currently needs
the manual master run**, where the output is saved to `master-remote.log`.

## Steps: one helper (Gate P2-C)

1. Helper: start RPC with debug logging (README "helper" section). Keep it running.
2. Master: preflight.
   `.\scripts\Check-RPC-Workers.ps1 -HelperIp <HELPER1_IP>` gives PASS on every row, including `Master CUDA0`.
3. Master: launch llama-server with `--rpc <HELPER1_IP>:50052 --device CUDA0,RPC0 --tensor-split 1,1 -lv 5`, saved to `master-remote.log`.
4. Master: `.\scripts\Collect-Inference-Evidence.ps1 -Mode Master -ExpectRpc 1` gives `MASTER ALLOCATION PASS`.
5. Wait until warmup is finished (`/health` is ok). Helper: `-Mode HelperSnapshot -Label before`.
6. Master: one **new** request: `.\scripts\Test-Chat.ps1`. Keep the saved `chat-*.json` (it must contain an answer).
7. Helper: `-Mode HelperSnapshot -Label after`, then `-Mode HelperCompare` gives `HELPER COMPUTE PASS`.
8. Helper sends the team only the printed `HELPER ...` lines. The master sends only the `MASTER ALLOCATION ...` line.

## Steps: two helpers (Gate P2-D)

Same as above with `-HelperIp <HELPER1_IP>,<HELPER2_IP>`, `--rpc <H1>:50052,<H2>:50052 --device CUDA0,RPC0,RPC1
--tensor-split 1,1,1`, `-ExpectRpc 2`. **Both** helpers take before/after snapshots around the **same** request,
and **both** must show `HELPER COMPUTE PASS`. If one helper runs out of memory or is skipped, record that row as
`FAIL` or `NOT RUN` with the reason. A one-helper success is not a two-helper pass.

## Steps: API-controlled demo (Gate P2-E)

1. Restart the coordinator only after code integration. Its memory is cleared: make a new invite and re-enroll both helpers.
2. `GET /nodes` (admin) and save the list **without** credentials (the API never returns them; still check).
3. `POST /start` with `model_id = qwen2.5-0.5b-instruct-q4_k_m` and `node_ids = [RPC0 node, RPC1 node]`.
   Then poll `GET /status` until `ready`. `starting` is not ready.
4. Helpers take `before` snapshots. Then `POST /chat` (non-streaming) and save the JSON. Then helpers take `after` + `HelperCompare`.
5. `POST /stop`, and check that `/status` says `stopped`.
6. Allocation evidence needs the launcher output saved to a log (see "Log paths"). Until that exists, P2-E can
   show compute deltas but **not** model-weight allocation, so record allocation as `NOT RUN` for the API path.

## Result matrix

| Gate | Check | Result | Evidence (sanitized) |
|---|---|---|---|
| P2-A | Shared demo model ID accepted by API and launcher | PASS | `/start` reads `launcher.MODEL_ALLOWLIST` since `e127f2d` (tested) |
| P2-A | Partial selection refused with reasons | FAIL | `/start` still starts the survivors when at least one worker is usable (expected-failure test) |
| P2-A | Refusal message names each rejected worker | FAIL | `main.py` calls `.items()` on the `rejected` tuple; the 400 shows a Python error instead (expected-failure test) |
| P2-A | Unit and integration suites | PASS | see the handoff report for counts |
| P2-B | Local API/engine start/status/chat/stop (mocked engine) | NOT RUN | |
| P2-C | Preflight (TCP + list-devices + CUDA0), one helper | NOT RUN | |
| P2-C | Master model-weight allocation on RPC0 | NOT RUN | |
| P2-C | Helper 1 fresh graph_compute delta | NOT RUN | Shafin's local helper log so far: 0 graph calls, probe connections only |
| P2-C | Chat JSON with an answer | NOT RUN | |
| P2-D | RPC0/RPC1 are two different physical hosts | NOT RUN | |
| P2-D | Model-weight allocation on RPC0 **and** RPC1 | NOT RUN | |
| P2-D | Fresh graph_compute delta on **both** helpers, same request | NOT RUN | |
| P2-E | `/start` -> `ready` -> `/chat` -> `/stop` with real helpers | NOT RUN | |

## Known limitations (do not hide them)

- Reported free RAM was about 2.54 GB (Itmam) and 1.87 GB (Shafin). These are hints, not guarantees, and RAM is
  **not** pooled. Only the 0.5B model is in scope. 7B/8B are not claimed.
- The launcher hardcodes `CUDA0`. A master without an NVIDIA GPU fails at startup (clear error, no fallback).
- More helpers can be slower than the master GPU alone for this tiny model. Phase 2 claims participation, not speed.
