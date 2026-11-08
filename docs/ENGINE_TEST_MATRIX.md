# Engine Test Matrix (owner: Shafin)

Every engine check, who owns it, how it runs and what counts as a pass. Automated rows run with
`.\.venv\Scripts\python.exe -m pytest -q tests agent/tests`; they use stand-ins and synthetic logs and **never** prove
that a GPU, helper or model ran. Physical rows need the three laptops and stay `PENDING` until observed.

Result values: `PASS (executed)`, `FAIL (output)`, `NOT RUN`, `PENDING - PHYSICAL`.

## A. Automated (no hardware)

| ID | Owner | Area | Test (file::name or command) | Input | Expected | Result |
|---|---|---|---|---|---|---|
| A1 | Shafin | Selection | `test_workers.py` (29 tests) | registry snapshots, malformed values | deterministic candidates; every reject has a reason; explicit order kept; no registry change | PASS (executed) |
| A2 | Shafin | Selection | `test_workers::test_two_helper_order_is_exactly_the_selected_order` | Rodel, Shafin | `--rpc` order Rodel,Shafin even when Shafin has more free RAM | PASS (executed) |
| A3 | Shafin | Selection | `test_workers::test_nonsense_free_ram_counts_as_unknown_and_never_crashes` | NaN, inf, -1, "8", True | treated as unknown, never passed on as a number | PASS (executed) |
| A4 | Shafin | HTTP | `test_backend_integration::BackendIntegrationTests` | invite, join, heartbeat, reconnect, revoke, restart | enrollment works; secrets never listed; bad tokens 401/403 | PASS (executed) |
| A5 | Shafin | HTTP | `...::test_two_laptops_enroll_over_http_and_start_as_rpc0_rpc1` | two clients with their own Tailscale addresses | two distinct IDs; launcher command `--rpc A,B --device CUDA0,RPC0,RPC1`; one launch log in a temp folder | PASS (executed) |
| A6 | Shafin | HTTP | `...::test_one_of_two_laptops_going_silent_refuses_the_whole_start` | Shafin stops heartbeating | 400 naming Shafin offline; nothing probed or started | PASS (executed) |
| A7 | Shafin | HTTP | `...StartHandoffTests` (13 tests) | partial, unknown, duplicate, bad model | all-or-nothing refusal with reasons, no Python error text | PASS (executed) |
| A8 | Shafin | HTTP | `...::test_worker_lost_between_selection_and_probe_fails_without_starting` | TCP refused at probe time | no Popen; `/status` error; `/stop` ok | PASS (executed) |
| A9 | Shafin | HTTP | `...::test_tcp_only_listener_is_not_treated_as_a_worker` | port open, no RPC device | not started | PASS (executed) |
| A10 | Shafin | HTTP | `...::test_second_start_while_running_is_busy_and_leaves_the_first_alone` | `/start` twice | 409; first engine untouched | PASS (executed) |
| A11 | Shafin | HTTP | `...::test_unexpected_launcher_error_returns_500_and_nothing_runs` | launcher raises | 500, nothing running, `/stop` safe | PASS (executed) |
| A12 | Shafin | HTTP | `...::test_stop_is_idempotent_and_chat_needs_a_ready_engine` | `/stop` twice, `/chat` stopped, `stream:true` | 200 twice; 503; 400 | PASS (executed) |
| A13 | Shafin | Script | `...ApiFlowScriptTests` (8 tests, real `Test-Api-Flow.ps1` vs real app) | happy path, engine error, chat 500, busy 409, stale IDs, one node, wrong count, no token | PASS only on ready+answer+stopped; `/stop` always after an accepted start; busy engine never stopped; token never printed | PASS (executed) |
| A14 | Shafin | Evidence | `test_engine_proof_contract.py` master tests | synthetic b12110 logs | PASS only with weights on N distinct non-loopback hosts; KV-only, same host, unmapped, reload, stale, wrong API log all FAIL | PASS (executed) |
| A15 | Shafin | Evidence | `test_engine_proof_contract.py` helper tests | appended, zero, rewritten, shrunk, old window, re-taken before | PASS / FAIL / AMBIGUOUS exactly as documented; pair used once | PASS (executed) |
| A16 | Shafin | Evidence | `test_engine_proof_contract.py` same-prompt tests | windows vs chat time | PASS only when both helpers PASS in one session and contain the chat time | PASS (executed) |
| A17 | Itmam | Launcher | `test_launcher.py` | launcher unit cases | as defined by Itmam | PASS (executed); leaks fake `api-launcher-*.log` into `.runtime\logs` (fix requested) |
| A18 | Rodel | API | `test_coordinator_api.py`, `test_registry_invites.py`, `test_api_engine_flow.py` | API contract | as defined by Rodel / Itmam | PASS (executed) |
| A19 | Itmam | Agent | `agent/tests` | agent behaviour | as defined by Itmam | PASS (executed) |

## B. Script checks on one laptop (no helpers)

| ID | Owner | Command | Expected | Result |
|---|---|---|---|---|
| B1 | Shafin | `Check-RPC-Workers.ps1 -HelperIp <ITMAM_IP> -ExpectHelpers 2 -SkipTailscalePing` on a non-CUDA laptop | binary FAIL, helper-count FAIL, ping SKIPPED, `PREFLIGHT FAIL`, exit 1 | PASS (executed on Shafin's laptop) |

## C. Physical (three laptops; runbook sections in brackets)

| ID | Gate | Owner | Check | Expected | Result |
|---|---|---|---|---|---|
| C1 | G2 | Itmam | `nvidia-smi -L`; `llama-server --version`; `--list-devices` | NVIDIA GPU; build 12110; `CUDA0` | PENDING - PHYSICAL |
| C2 | G2 | Itmam | model SHA256 | `74a4da8c...d7a9db` | PENDING - PHYSICAL |
| C3 | G2 | Rodel, Shafin | `ggml-rpc-server.exe` present, b12110 CPU build | starts and listens | PENDING - PHYSICAL |
| C4 | G3 | Rodel, Shafin | `Test-NetConnection <ITMAM_IP> -Port 8000` and `/health` [3] | both succeed (historical timeout fixed) | PENDING - PHYSICAL |
| C5 | G3 | Itmam | `/nodes` after fresh invites [4] | two distinct IDs and addresses, online, rpc_ready | PENDING - PHYSICAL |
| C6 | G4 | Itmam | `Check-RPC-Workers.ps1 ... -ExpectHelpers 2` [5] | `PREFLIGHT PASS`, RPC0 = Rodel, RPC1 = Shafin, CUDA0 | PENDING - PHYSICAL |
| C7 | G5 | all | manual CUDA0,RPC0 [6] | answer; `MASTER ALLOCATION PASS` (1); `RODEL HELPER COMPUTE PASS` | PENDING - PHYSICAL |
| C8 | G6 | all | manual CUDA0,RPC0,RPC1 [7] | answer; allocation PASS (2); both helpers PASS; `SAME PROMPT PROOF PASS` | PENDING - PHYSICAL |
| C9 | G7 | all | `Test-Api-Flow.ps1 ... -PauseBeforeChat` twice [8] | `API FLOW PASS`; `-ApiLog` allocation PASS (2); same-prompt PASS | PENDING - PHYSICAL |

## D. Regression and failure cases (physical where marked)

| ID | Case | How to cause it | Expected | Automated cover | Result |
|---|---|---|---|---|---|
| D1 | Port 8000 closed | coordinator stopped or bound to 127.0.0.1 | helper `/health` fails; agent retries; runbook section 3 | - | PENDING - PHYSICAL |
| D2 | RPC 50052 closed | stop one helper's RPC server | preflight TCP FAIL; `/start` refused (rpc_not_ready) or launcher probe error; nothing started | A8 | PENDING - PHYSICAL |
| D3 | Missing CUDA0 | run preflight on a helper laptop | `Master CUDA0` FAIL | B1 | PASS (executed) |
| D4 | Corrupt runtime archive | damaged zip | `Install-Runtime.ps1` SHA256 mismatch stops (Itmam) | - | NOT RUN |
| D5 | Expired invite | join after 15 min | 400; new invite works | A4 | PENDING - PHYSICAL |
| D6 | Master restart | restart coordinator | old IDs rejected; re-enroll gives new IDs | A4 | PENDING - PHYSICAL |
| D7 | Node offline | stop one agent | `/start` with it refused (all-or-nothing) | A6 | PENDING - PHYSICAL |
| D8 | Wrong selected order | swap node_ids | RPC0/RPC1 swap in command and logs | A2, A5 | PENDING - PHYSICAL |
| D9 | Zero weights on a helper | tiny model or split | `MASTER ALLOCATION FAIL`, recorded as FAIL | A14 | PENDING - PHYSICAL |
| D10 | Zero delta | snapshots without a chat | `HELPER COMPUTE FAIL` | A15 | PENDING - PHYSICAL |
| D11 | llama-server crash | kill it during ready | `/status` error; `Test-Api-Flow` stops and exits 1 | A13 | PENDING - PHYSICAL |
| D12 | Stop under failure | Ctrl+C at `-PauseBeforeChat` | `finally` sends `/stop`; status stopped | A13 (error paths) | PENDING - PHYSICAL |

## Retry and rollback

- A failed physical row is retried only after the cause is named (wrong IP, firewall, stale ID, etc.) and recorded.
- AMBIGUOUS evidence is redone with a new `-Session`; never merged with an older window.
- Code rollback, if ever needed, is a new commit that reverts the change (`git revert <sha>`), pushed in the owner's slot.
  No `reset --hard`, no force push.
