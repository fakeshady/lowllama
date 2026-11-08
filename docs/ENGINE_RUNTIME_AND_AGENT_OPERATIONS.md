# LowLlama engine runtime and helper-agent operations

This runbook is for the engine-only three-laptop deployment. Itmam's NVIDIA laptop is the master. Rodel and Shafin are CPU helpers. All commands use Windows PowerShell 5.1 from the project root and the same reviewed `main` commit.

Never paste admin tokens, invite codes, helper credentials, Tailscale authentication material, prompts, or raw private logs into Git. `.runtime/` and model files are local and ignored.

## Fixed topology

| Machine | Processes | Network listeners |
|---|---|---|
| Itmam | FastAPI coordinator and its owned `llama-server` | coordinator on current Tailscale IP port 8000; model server only on `127.0.0.1:8080` |
| Rodel | CPU `ggml-rpc-server` and Python helper agent | current Tailscale IP port 50052 |
| Shafin | CPU `ggml-rpc-server` and Python helper agent | current Tailscale IP port 50052 |

RPC order is chosen by `/start.node_ids`: the first selected helper becomes RPC0 and the second RPC1. It is not permanently associated with a person's name.

## Install and verify pinned assets

On every laptop, use the existing `.venv` and install Python dependencies:

```powershell
py -3.12 -m venv .venv   # only if .venv does not exist
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Itmam master:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Runtime.ps1 -Role Master
nvidia-smi
& .\.runtime\llama-b12110-cuda\llama-server.exe --version
& .\.runtime\llama-b12110-cuda\llama-server.exe --list-devices
Get-FileHash -Algorithm SHA256 .\.runtime\models\qwen2.5-0.5b-instruct-q4_k_m.gguf
```

Expected build: `12110`, commit `cd959bae99ff4bed22bef2604f45241c09bbb8c8`. Expected master device: `CUDA0`. Expected model SHA-256: `74A4DA8C9FDBCD15BD1F6D01D621410D31C6FC00986F5EB687824E7B93D7A9DB`.

Each helper:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Runtime.ps1 -Role Helper
& .\.runtime\llama-b12110-cpu\llama-server.exe --version
Test-Path .\.runtime\llama-b12110-cpu\ggml-rpc-server.exe
```

Helper installation does not download the GGUF or require NVIDIA. Downloads are reused only after their pinned SHA-256 matches. If a direct GitHub release request fails, the installer makes a bounded attempt through the official GitHub release-asset API and still requires the same hash.

## Tailnet and firewall preparation

Keep all machines in Rodel's existing tailnet. Do not create public port forwards or change tailnet ownership.

```powershell
& 'C:\Program Files\Tailscale\tailscale.exe' status
& 'C:\Program Files\Tailscale\tailscale.exe' ip -4
```

Record current addresses locally for the session. Historical IPs are not evidence. Windows Firewall should allow master TCP 8000 only from the two trusted helper/admin tailnet addresses, and each helper TCP 50052 only from Itmam's current tailnet address. Configure those rules manually with the team administrator; the start scripts deliberately do not modify firewall or Tailscale settings.

Before enrollment, both helpers must verify the master:

```powershell
Test-NetConnection <ITMAM_CURRENT_TAILSCALE_IP> -Port 8000
Invoke-RestMethod http://<ITMAM_CURRENT_TAILSCALE_IP>:8000/health
```

## Start the master

In a private Itmam master terminal:

```powershell
$env:LOWLLAMA_ADMIN_TOKEN = Read-Host 'Set private coordinator admin token'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-Master.ps1
```

The script discovers Itmam's current Tailscale IPv4, sets `LOWLLAMA_MASTER_IP` only for the process, verifies driver/runtime/model/CUDA0, verifies port 8000 is free, records the exact owned PID under `.runtime/pids/`, and starts Uvicorn without reload. It never prints or saves the admin token.

In another Itmam terminal, enter the same private token and create a fresh invitation using the existing authenticated `/invite` endpoint. Invites and the node registry are in memory. Every coordinator restart requires fresh invitations, new enrollment, and freshly fetched node IDs.

## Start each helper

Use two separate terminals on each CPU helper.

RPC terminal:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-Helper.ps1 -Component Rpc -RpcPort 50052 -Threads 4
```

Agent terminal, first enrollment or enrollment after a coordinator restart:

```powershell
$env:LOWLLAMA_COORDINATOR_URL = 'http://<ITMAM_CURRENT_TAILSCALE_IP>:8000'
$env:LOWLLAMA_NODE_NAME = '<RODEL_OR_SHAFIN_HELPER_NAME>'
$env:LOWLLAMA_INVITE_CODE = Read-Host 'Fresh private LowLlama invite code'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-Helper.ps1 -Component Agent -RpcPort 50052
```

For a normal helper-agent restart against the same uninterrupted coordinator, omit `LOWLLAMA_INVITE_CODE`; DPAPI-protected stored credentials reconnect the existing identity. If the coordinator restarted or rejects old credentials, obtain a new invite. Do not delete the entire `%LOCALAPPDATA%\LowLlama\Agent` directory: `client_id` is stable local identity and `registration.dpapi` is protected registration state.

The helper script derives its current local Tailscale IP and sets `LOWLLAMA_RPC_HOST`; it does not accept a historic peer address. The agent's `rpc_ready` heartbeat means only that TCP 50052 accepted a local connection.

## Verify selection and inference

On Itmam, fetch `/nodes` with the admin Bearer token. Require exactly two intended, distinct current helpers with `online` and `rpc_ready: true`. Use fresh `node_id` values after every coordinator restart.

Then run Shafin's preflight:

```powershell
.\scripts\Check-RPC-Workers.ps1 -HelperIp <RODEL_CURRENT_IP>,<SHAFIN_CURRENT_IP>
```

Preflight must show CUDA0, RPC0, and RPC1 using pinned build 12110. It proves protocol compatibility, not model allocation or computation.

For manual one-helper and two-helper tests, never run a manual model server while the API launcher owns port 8080. Use the command flags recorded in `docs/PHASE2_DEMO_RUNBOOK.md`, inspect master startup logs for nonzero model-weight buffers/layers on every claimed RPC device, and use Shafin's evidence collector around one fresh request.

For API-controlled proof, select fresh node IDs and run `/start`, poll `/status` until `ready`, take helper before snapshots, send one authenticated nonstreaming `/chat`, take after snapshots, then `/stop`. The launcher prints the unique ignored `.runtime/logs/api-launcher-*.log` path locally. Repeat two clean cycles and a failed-start case.

`scripts/Test-Chat.ps1` discovers the active alias from the local llama.cpp `/v1/models` response, so it works with both the manual `lowllama` alias and the API launcher's canonical model ID. `-ModelId` remains available for an explicit override.

Evidence levels must remain separate:

1. `online`: agent heartbeat is current.
2. `rpc_ready`: helper TCP listener answered its local probe.
3. RPC compatible: master `--list-devices` found RPC0/RPC1.
4. Allocated: master log shows nonzero remote model weights/layers.
5. Computed: the corresponding helper's fresh `graph_compute` or `graph_recompute` counter increased for that request.

Only level 5 on both distinct helpers for the same request satisfies two-helper computation. Do not claim speedup, pooled/additive RAM, or remote computation from generated text alone.

## Stop safely

Ctrl+C in each owning start-script terminal is preferred. To stop only processes whose exact PID and executable were recorded by these scripts:

```powershell
.\scripts\Stop-LowLlama.ps1 -Role Helper
.\scripts\Stop-LowLlama.ps1 -Role Master
```

Use `-Force` only when an owned recorded process will not terminate. The script refuses a PID whose executable no longer matches. It never kills all Python or llama processes. A separately started manual `llama-server` is not owned by this script and must be stopped deliberately in its own terminal before API launch.

## Automated tests

Because an unrelated parent pytest configuration may exist on a development machine, ensure pytest's root is this repository. The normal team command is:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests agent/tests
.\.venv\Scripts\python.exe -m compileall -q coordinator agent
.\.venv\Scripts\python.exe -c "from coordinator.main import app; print(app.title)"
git diff --check
```

If pytest incorrectly selects a parent project, use an ignored local config under `.runtime/` and pass it with `-c`; do not modify another owner's repository configuration without coordination.

## Failure playbook

- Download HTTP 504: rerun the installer. It retries direct download and then the official asset API. A hash mismatch always fails closed; never bypass it.
- Missing/corrupt runtime file: move only the named local runtime/archive aside and rerun installation. Do not delete the repository or entire `.runtime` tree.
- Missing CUDA0: update/fix the NVIDIA driver, confirm `nvidia-smi`, and rerun `--list-devices`. There is no silent CPU-master fallback.
- Master TCP 8000 blocked: confirm Start-Master is running on Itmam's current Tailscale IP, test the port locally, then fix the scoped firewall/tailnet policy before issuing invites.
- Helper TCP 50052 blocked: confirm the helper RPC terminal is still running and bound to that helper's current Tailscale IP, then fix the scoped firewall rule.
- `starting` timeout: check the latest local `api-launcher-*.log`, model hash, free RAM, CUDA0, RPC compatibility, and port 8080. Stop only an identified manual server; never sweep all llama processes.
- Wrong runtime build or asset mismatch: reinstall from pinned b12110 archives. Do not mix CPU/CUDA binaries from different releases.
- Agent offline after coordinator restart: create a fresh invite and re-enroll; old in-memory registry credentials cannot recover the old node ID.
- Corrupt `registration.dpapi`: preserve `client_id`, remove only the corrupt registration file deliberately, obtain a fresh invite, and re-enroll.
- Remote helper receives zero weights: record the allocation as FAIL, preserve sanitized logs, and coordinate supported split changes. Do not fabricate compute evidence or change the approved model unilaterally.

## Session evidence record

Keep raw evidence only under ignored `.runtime/logs/`. For every physical run record the reviewed commit, current machine names/IPs locally, runtime build, model hash, preflight outcome, per-device allocation, per-helper compute delta, API lifecycle result, and cleanup result. Unavailable physical checks are `PENDING — MANUAL HARDWARE`, never PASS.
