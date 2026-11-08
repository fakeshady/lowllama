# LowLlama Engine Demo Runbook (owner: Shafin)

How to run the physical engine session from a clean start: one NVIDIA master, two CPU helpers, all on the
existing Tailscale network. Every step says **who** runs it and in **which terminal**. Use placeholders in anything
you share; real addresses stay on the laptops.

## Roles, ports and rules

| Laptop | Role | Terminals | Listens on |
|---|---|---|---|
| Itmam | **Master** (only NVIDIA GPU): coordinator + llama-server | A: coordinator, B: tests and evidence | `<ITMAM_IP>:8000` (coordinator), `127.0.0.1:8080` (llama-server, local only) |
| Rodel | **Helper** (CPU), tailnet owner | A: RPC server, B: agent, C: evidence | `<RODEL_IP>:50052` |
| Shafin | **Helper** (CPU) | A: RPC server, B: agent, C: evidence | `<SHAFIN_IP>:50052` |

- All three stay on **Rodel's existing tailnet**. Owning the tailnet does not make Rodel the master.
- The order of `node_ids` in `/start` (or of addresses in `--rpc`) **is** RPC0, RPC1. This runbook uses Rodel = RPC0, Shafin = RPC1.
- Only one llama-server may own `127.0.0.1:8080` at a time: a manual run **or** the API-launched one, never both.
- Never paste admin tokens, invite codes, raw logs, prompts or node credentials into chat, issues or commits. Share only
  the printed `... PASS/FAIL` summary lines. `.runtime\` (logs, evidence, models, binaries) is git-ignored.
- The coordinator keeps everything in memory. A coordinator restart means: new invite, both helpers re-enroll, new node IDs.

## 0. Same code everywhere (all three)

```powershell
git status --short
git fetch origin
git status -sb                                   # must not be behind; pull --ff-only only when clean
git log -1 --oneline                             # all three must print the same commit
.\.venv\Scripts\python.exe -m pytest -q tests agent/tests
```

## 1. Current Tailscale addresses (all three)

```powershell
& 'C:\Program Files\Tailscale\tailscale.exe' status
& 'C:\Program Files\Tailscale\tailscale.exe' ip -4
```

Everyone must appear in the same tailnet and be online. Note the three IPv4 addresses locally; recheck them at the
start of every session (they are stable but not guaranteed).

## 2. Firewall: scoped, LowLlama-only rules (Administrator PowerShell)

Open **Start**, type **PowerShell**, right-click it, **Run as administrator**.

**Itmam (master)**, so helpers can reach the coordinator:

```powershell
New-NetFirewallRule -DisplayName 'LowLlama Coordinator 8000' -Direction Inbound -Protocol TCP `
    -LocalAddress <ITMAM_IP> -LocalPort 8000 -RemoteAddress <RODEL_IP>,<SHAFIN_IP> -Action Allow -Profile Any
```

Also check for a hidden **block** rule (created when someone pressed Cancel on the "Windows Defender Firewall has
blocked some features of Python" popup). Block rules beat allow rules:

```powershell
Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True | Where-Object { $_.DisplayName -match 'python' } |
    Format-Table DisplayName, Profile
```

If anything is listed: **Windows Security > Firewall & network protection > Allow an app through firewall**, find
Python, tick **Private** and **Public**.

**Each helper**, so only the master can reach its RPC server (an earlier rule may still point at the old master,
check `Get-NetFirewallRule -DisplayName 'LowLlama*'` first and fix the remote address):

```powershell
$rpc = (Resolve-Path '.runtime\llama-b12110-cpu\ggml-rpc-server.exe').Path
New-NetFirewallRule -DisplayName 'LowLlama RPC 50052' -Direction Inbound -Protocol TCP `
    -LocalAddress <THIS_HELPER_IP> -LocalPort 50052 -RemoteAddress <ITMAM_IP> -Program $rpc -Action Allow -Profile Any
```

Never forward 8000 or 50052 on a router. RPC has no authentication of its own.

## 3. Master: start the coordinator (Itmam, Terminal A)

```powershell
$ts = 'C:\Program Files\Tailscale\tailscale.exe'
$masterIp = (& $ts ip -4 | Select-Object -First 1).Trim()
$env:LOWLLAMA_MASTER_IP = $masterIp
$env:LOWLLAMA_ADMIN_TOKEN = Read-Host 'New admin token'
.\.venv\Scripts\python.exe -m uvicorn coordinator.main:app --host $masterIp --port 8000
```

`--host 127.0.0.1` (as in older notes) only accepts local connections: helpers will time out. Leave Terminal A open.

**Itmam, Terminal B**: confirm the binding and the health check on the Tailscale address:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | Format-Table LocalAddress, LocalPort, OwningProcess
Invoke-RestMethod "http://<ITMAM_IP>:8000/health"
```

**Each helper**: both must succeed before enrolling (ping alone is not enough):

```powershell
Test-NetConnection <ITMAM_IP> -Port 8000
Invoke-RestMethod "http://<ITMAM_IP>:8000/health" -TimeoutSec 10
```

A **timeout** (not "refused") almost always means the master firewall is dropping the connection; see section 2.

## 4. Helpers: RPC server and agent (Rodel and Shafin)

**Terminal A** (leave running; this is the log the evidence script reads):

```powershell
$ts = 'C:\Program Files\Tailscale\tailscale.exe'
$helperIp = (& $ts ip -4 | Select-Object -First 1).Trim()
$rpc = (Resolve-Path '.runtime\llama-b12110-cpu\ggml-rpc-server.exe').Path
New-Item -ItemType Directory -Force .runtime\logs | Out-Null
$env:GGML_RPC_DEBUG = '1'
& $rpc --device CPU --host $helperIp --port 50052 --threads 4 2>&1 | Tee-Object -FilePath .runtime\logs\helper-rpc.log
```

**Itmam** creates an invite (Terminal B) and sends the code privately to each helper:

```powershell
$h = @{ Authorization = "Bearer $env:LOWLLAMA_ADMIN_TOKEN" }
Invoke-RestMethod "http://<ITMAM_IP>:8000/invite" -Method Post -Headers $h -ContentType 'application/json' -Body '{}'
```

**Helper, Terminal B**:

```powershell
$ts = 'C:\Program Files\Tailscale\tailscale.exe'
$env:LOWLLAMA_COORDINATOR_URL = 'http://<ITMAM_IP>:8000'
$env:LOWLLAMA_RPC_HOST = (& $ts ip -4 | Select-Object -First 1).Trim()
$env:LOWLLAMA_RPC_PORT = '50052'
$env:LOWLLAMA_NODE_NAME = 'Shafin-Laptop'                 # Rodel: 'Rodel-Laptop'
$env:LOWLLAMA_INVITE_CODE = Read-Host 'Invite code'       # not echoed into history
.\.venv\Scripts\python.exe -m agent.agent
```

**Itmam** checks both helpers (two distinct IDs and addresses, `online`, `rpc_ready: True`):

```powershell
Invoke-RestMethod "http://<ITMAM_IP>:8000/nodes" -Headers $h | Format-Table node_id, node_name, address, status, rpc_ready
```

Online and rpc_ready are readiness signals only, not proof of compute.

## 5. Preflight (Itmam, Terminal B)

```powershell
.\scripts\Check-RPC-Workers.ps1 -HelperIp <RODEL_IP>,<SHAFIN_IP> -ExpectHelpers 2 -Redact
```

Must end with `PREFLIGHT PASS` (pinned build 12110, CUDA0, RPC0 = Rodel, RPC1 = Shafin). Still not proof of inference.

## 6. Manual one-helper run, gate G5 (Itmam + Rodel)

Itmam, Terminal B (the coordinator in Terminal A stays up; it does not use port 8080 until `/start`):

```powershell
$server = (Resolve-Path '.runtime\llama-b12110-cuda\llama-server.exe').Path
$model = (Resolve-Path '.runtime\models\qwen2.5-0.5b-instruct-q4_k_m.gguf').Path
$env:GGML_RPC_DEBUG = '1'
& $server -m $model --alias lowllama --host 127.0.0.1 --port 8080 --rpc <RODEL_IP>:50052 `
    --device CUDA0,RPC0 --split-mode layer --tensor-split 1,1 -ngl all --fit off -c 2048 -np 1 -fa off -lv 5 `
    2>&1 | Tee-Object -FilePath .runtime\logs\master-remote.log
```

When `/health` on 8080 is ok (warmup done), in a third Itmam terminal:

```powershell
.\scripts\Collect-Inference-Evidence.ps1 -Mode Master -ExpectRpc 1 -Redact     # MASTER ALLOCATION PASS
```

Rodel, Terminal C: `.\scripts\Collect-Inference-Evidence.ps1 -Mode HelperSnapshot -Label before -Session g5 -HelperName Rodel`

Itmam: note the time and send ONE chat:

```powershell
(Get-Date).ToUniversalTime().ToString('o')      # this is the ChatSentAt value
.\scripts\Test-Chat.ps1
```

Rodel: `-Mode HelperSnapshot -Label after -Session g5 -HelperName Rodel`, then `-Mode HelperCompare -Session g5 -HelperName Rodel`
and send the `RODEL HELPER COMPUTE ...` line. Stop the manual llama-server with Ctrl+C.

## 7. Manual two-helper run, gate G6 (all three)

Same as section 6 with `--rpc <RODEL_IP>:50052,<SHAFIN_IP>:50052 --device CUDA0,RPC0,RPC1 --tensor-split 1,1,1`,
`-ExpectRpc 2`, and `-Session g6` on **both** helpers. Both take `before` after warmup, nobody chats except the ONE
request, both take `after`, both run `HelperCompare`. Then on Itmam:

```powershell
.\scripts\Collect-Inference-Evidence.ps1 -Mode SamePrompt -ChatSentAt <time noted before the chat> `
    -HelperResult '<RODEL line>', '<SHAFIN line>'
```

G6 needs `MASTER ALLOCATION PASS` (both RPC devices hold weights on two hosts), both `HELPER COMPUTE PASS`, and
`SAME PROMPT PROOF PASS`. `AMBIGUOUS` means redo, never "close enough". Stop the manual server before section 8.

## 8. API-controlled run, gate G7 (all three)

Itmam, Terminal B (Terminal A coordinator still running, no manual llama-server):

```powershell
.\scripts\Test-Api-Flow.ps1 -CoordinatorUrl http://<ITMAM_IP>:8000 -NodeIds <rodel-node-id>,<shafin-node-id> `
    -ExpectHelpers 2 -PauseBeforeChat
```

At the `PAUSED` prompt:

1. Itmam, another terminal: `.\scripts\Collect-Inference-Evidence.ps1 -Mode Master -ApiLog -ExpectRpc 2 -Redact`
   (it prints which `api-launcher-*.log` it used; the launcher also prints the path in Terminal A).
2. Both helpers: `-Mode HelperSnapshot -Label before -Session g7a -HelperName <name>`.
3. Itmam presses Enter. The script prints `CHAT SENT AT <time>`, the answer, then stops the engine.
4. Both helpers: `-Label after`, then `-Mode HelperCompare`; Itmam runs `-Mode SamePrompt` with that time.

`API FLOW PASS` requires ready, an answer, and `stopped` afterwards. Repeat once more with a new session name (`g7b`).
Negative checks for G7 (each must fail cleanly and leave `/status` stopped): a stale node ID, a helper with its RPC
server stopped, an expired invite on re-enrollment, and a second `/start` while one is running (409, left alone).

## 9. Shutdown and cleanup

1. Itmam: `POST /stop` if anything is still running (`Test-Api-Flow.ps1` already does this), then Ctrl+C in Terminal A.
2. Helpers: Ctrl+C the agent (Terminal B), then the RPC server (Terminal A).
3. Keep `.runtime\logs` and `.runtime\logs\evidence` locally; share only summary lines.

## Troubleshooting

| Symptom | Most likely cause | Check |
|---|---|---|
| Ping works, TCP 8000 times out | Master firewall drops it, or a python block rule | Section 2 on Itmam |
| TCP 8000 refused immediately | Coordinator not listening on the Tailscale address | Section 3 binding check |
| Agent says the URL must be HTTPS | URL is not a literal Tailscale IP | Use `http://<ITMAM_IP>:8000` |
| `/start` 400 "Selected workers unavailable" | A selected helper is offline, not rpc_ready or a duplicate | `/nodes`; restart that helper's agent/RPC |
| `/start` 400 unknown_node | Node IDs from before a coordinator restart | New invite, re-enroll, new IDs |
| `/start` 409 | An engine is already running | `POST /stop` only if you started it |
| Preflight: RPCn missing | Wrong llama.cpp build on a helper, or not ggml-rpc-server on 50052 | Pinned b12110 on both sides |
| Preflight: no CUDA0 | Not the NVIDIA laptop, or the CPU runtime path was used | Run on Itmam with the CUDA build |
| `MASTER ALLOCATION FAIL` with KV only | Tiny model placed no weights on that helper | Recheck `--tensor-split`; record FAIL, never fake |
| `HELPER COMPUTE AMBIGUOUS` | RPC restarted, log rewritten or window too long | Redo both snapshots around one chat |
| `MASTER ALLOCATION FAIL : STALE` | Old log | Use this run's log |
