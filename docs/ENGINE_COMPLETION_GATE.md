# Engine Completion Gate (owner: Shafin)

The release gate for the engine. **Frontend work starts only after G1 to G9 are all PASS.** Each item is
`PENDING`, `PASS` or `FAIL` with an evidence pointer (a sanitized summary line or a local ignored path), who confirmed
it and when. Rodel and Itmam send evidence and sign-off as team messages; only Shafin edits this file.

A physical item is never marked PASS from mocked tests, a previous session, ping/TCP success or `rpc_ready`.

| Gate | Requirement | Status | Evidence | Confirmed by | Date |
|---|---|---|---|---|---|
| G1 | Same reviewed `origin/main` on all three laptops; full `pytest tests agent/tests` passes; imports/compile ok; no secrets or unknown changes | PENDING | Shafin's laptop, pre-push: see "G1 runs" below. Needs the pushed commit re-run on all three | | |
| G2 | Itmam: `nvidia-smi`, llama-server build 12110, `--list-devices` shows `CUDA0`, model SHA256 matches. Rodel + Shafin: pinned CPU `ggml-rpc-server` | PENDING | | | |
| G3 | Rodel tailnet kept; helper 50052 scoped to Itmam; Itmam 8000 scoped to helpers; both helpers pass TCP **and** `/health` on Itmam (historical 8000 timeout fixed); fresh invites; `/nodes` has two distinct online + rpc_ready helpers | PENDING | Last known: Shafin -> Itmam ping OK, TCP 8000 TIMEOUT (2026-11-08) | | |
| G4 | `Check-RPC-Workers.ps1 -ExpectHelpers 2` on Itmam: `PREFLIGHT PASS`, CUDA0, RPC0 = Rodel, RPC1 = Shafin (preflight only, not inference) | PENDING | | | |
| G5 | Manual CUDA0,RPC0 with Rodel: real answer; `MASTER ALLOCATION PASS` (Rodel weights + layers); `RODEL HELPER COMPUTE PASS` for a new prompt | PENDING | | | |
| G6 | Manual CUDA0,RPC0,RPC1: real answer; weights + layers on **both** helpers on two hosts; both `HELPER COMPUTE PASS`; `SAME PROMPT PROOF PASS` | PENDING | | | |
| G7 | API: `/start` -> `ready` -> `/chat` -> `/stop` -> stopped, twice (`API FLOW PASS`), with `-ApiLog` allocation PASS and same-prompt PASS; negative cases leave nothing running | PENDING | | | |
| G8 | Operations and security documented and followed: start/stop order, terminals, firewall scope, IP discovery, invite expiry and restart, local-only logs, no token/invite disclosure, no speed or RAM-pooling claims | PENDING | Documented in `docs/PHASE2_DEMO_RUNBOOK.md` and `docs/PHASE2_EVIDENCE_CHECKLIST.md`; "followed" needs the physical session | | |
| G9 | Sign-off: Rodel (API, registry, security), Itmam (CUDA, launcher, agent), Shafin (selection, evidence) | PENDING | | | |

**Engine status: NOT COMPLETE. Frontend: NOT STARTED.**

## G1 runs (append, never overwrite)

| Date | Commit | Machine | Command | Result |
|---|---|---|---|---|
| 2026-11-08 | `02f97e3` (clean) | Shafin, Windows 11, Python 3.12, pytest 9.2.0 | `.venv\Scripts\python.exe -m pytest -q tests agent/tests` | 245 passed, 72 subtests (baseline before Shafin's changes) |
| 2026-11-08 | `02f97e3` + Shafin's uncommitted changes | same | same, plus `compileall -q coordinator agent`, `from coordinator.main import app`, `git diff --check` | 290 passed, 80 subtests, exit 0; compile ok; import prints "LowLlama Coordinator"; diff check clean |

## Open requests to other owners

| To | Request | Why | Status |
|---|---|---|---|
| Itmam | In `tests/test_launcher.py`, patch `launcher.LAUNCH_LOG_DIRECTORY` to a temp folder in `setUp` | Each test run writes about 18 fake `api-launcher-*.log` files into the real `.runtime\logs` evidence folder | open |
| Rodel | `/start` still returns `str(e)` in "Error selecting workers: ..." and "Launcher error: ..." | Can leak internal text; a fixed message plus server-side log is safer | open |
