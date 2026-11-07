# LowLlama

UBhacking project: share model inference across three Windows laptops using
llama.cpp RPC. The master stores the GGUF and sends model tensors to helpers;
helpers contribute CPU compute without downloading the full GGUF.
Deadline: **November 8, 2026, 11:00 AM Eastern (EST)**.

## Current status (verified November 7, 2026)

- `coordinator/__init__.py` contains the existing FastAPI app: `/` returns a
  status message and `/nodes` returns the in-memory node list, initially empty.
  `coordinator/main.py` is empty. Node registration, inference integration,
  scheduling, and a Streamlit dashboard remain to be built.
- The existing `.venv` runs Python 3.12.3, FastAPI 0.162.2, Uvicorn 0.54.0,
  and psutil 7.2.2. Preserve this environment. It uses `uv` and has no `pip`
  module; that does not prevent running the app.
- Local CUDA inference passed with Qwen2.5-0.5B-Instruct Q4_K_M. The answer to
  "What is 2 + 2?" was "2 + 2 equals 4." Health and chat endpoints passed.
- A **same-laptop RPC diagnostic** also passed: master layers 0-12 on CUDA0,
  layers 13-24 on RPC0; model buffers 129.39 MiB on CUDA0 and 244.33 MiB on
  RPC0. The RPC CPU process logged allocation and graph computation, and chat
  returned the same correct answer. Layer 24 is the output layer.
- **Two-physical-laptop inference is not verified.** The master has Tailscale
  installed but its inspected state was `NeedsLogin`. The team has not set up
  the tailnet or supplied a helper address. Complete the teammate steps below.
- Both diagnostic servers were stopped after testing. Local evidence is in
  `.runtime/logs/local-master.log`, `local-response.json`,
  `loopback-master.log`, `loopback-helper.log`, and `loopback-response.json`.
  These ignored files exist only on the tested master.

The inference server below is separate from the FastAPI coordinator. The first
milestone is an answer using a real helper, with runtime evidence on both
computers. Build the dashboard after that milestone.

## Tool inventory on the master

| Tool | Checked result |
| --- | --- |
| Git | 2.55.0.windows.3 at `C:\Program Files\Git\cmd\git.exe` |
| Python | Existing `.venv\Scripts\python.exe`, Python 3.12.3 |
| uv | `C:\Users\shahi\.local\bin\uv.exe` |
| Tailscale | 1.104.5 at `C:\Program Files\Tailscale\tailscale.exe`; service running, login required |
| NVIDIA | RTX 4060 Laptop, 8188 MiB dedicated memory, driver 610.60; CUDA inference tested |
| CMake / Visual Studio C++ tools | Not found on PATH or in checked standard install locations/registry |
| CUDA toolkit (`nvcc`) | Not found on PATH or in the standard toolkit location |
| Existing llama.cpp / GGUF | None found in checked Downloads, Documents, OneDrive, app/cache, and project locations; pinned runtime installed below |

The NVIDIA driver's reported CUDA support is not an installed CUDA toolkit.
No compiler or toolkit installation is needed for these tested prebuilt
binaries: the master includes the CUDA 12.4 runtime DLLs. The master has about
16 GB RAM; helpers have 16 GB and 8 GB. Start with one CPU helper and a 2048-token
context. Close memory-heavy apps if a helper is short on available RAM.

## Pinned runtime and model

All laptops must use **llama.cpp b12110**, commit
`cd959bae99ff4bed22bef2604f45241c09bbb8c8`. The binary reports
`0.9.0-dev (build 12110, commit cd959bae9)`; the RPC protocol reports `8.0.1`.
Those version strings describe different things.

The [official release](https://github.com/ggml-org/llama.cpp/releases/tag/b12110)
contains CPU and CUDA Windows x64 binaries. Its
[build workflow](https://github.com/ggml-org/llama.cpp/blob/b12110/.github/workflows/release.yml)
enables `GGML_RPC=ON`. Both RPC loading and CUDA execution were verified here;
no source build was necessary. Executable names and the commands below were
checked against the downloaded binaries' `--help` and the
[pinned RPC documentation](https://github.com/ggml-org/llama.cpp/blob/b12110/tools/rpc/README.md).
Keep this release on every laptop until the demo is working.

Model: [official Qwen2.5-0.5B-Instruct GGUF](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF),
`qwen2.5-0.5b-instruct-q4_k_m.gguf`, 491,400,032 bytes, revision
`9217f5db79a29953eb74d5343926648285ec7e67`.
SHA256: `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`.

`scripts/Install-Runtime.ps1` pins the download URLs and all SHA256 checksums.
It reuses verified files and stops on mismatches. It puts binaries, DLLs,
downloads, and weights under ignored `.runtime/`, outside application source.
The Master role downloads about 1.2 GB total; the Helper role downloads only
the approximately 25 MB CPU archive. Helpers need neither Python nor CUDA for
this milestone. They do not run the Master installation command.

## Master: run the model locally

Use PowerShell in the project root. Installation is already complete on the
current master; rerunning this command safely reuses its downloads:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Runtime.ps1 -Role Master
```

Start the model in one PowerShell window:

```powershell
New-Item -ItemType Directory -Force .runtime\logs | Out-Null
$server = (Resolve-Path '.runtime\llama-b12110-cuda\llama-server.exe').Path
$model = (Resolve-Path '.runtime\models\qwen2.5-0.5b-instruct-q4_k_m.gguf').Path
& $server --version
& $server --list-devices
& $server -m $model --alias lowllama --host 127.0.0.1 --port 8080 `
    --device CUDA0 -ngl all --fit off -c 2048 -np 1 -fa off -lv 5 `
    2>&1 | Tee-Object -FilePath .runtime\logs\master-local.log
```

Wait for `listening on http://127.0.0.1:8080`, then in a second project-root
PowerShell window:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Test-Chat.ps1
```

This checks `/health`, posts to `/v1/chat/completions`, prints the answer,
and saves a timestamped JSON response in `.runtime/logs/`. The built-in chat
UI is also available at http://127.0.0.1:8080. Stop the server with Ctrl+C
before starting the distributed configuration on the same port.

## Team: set up one Tailscale network

1. On the master, open the installed Tailscale app and sign in with your own
   account. Use its tray icon or Windows Start menu. The installer is already
   in Downloads, but reinstalling is unnecessary.
2. The tailnet owner opens the [Tailscale admin Users page](https://login.tailscale.com/admin/users),
   chooses **Invite external users**, and invites each teammate. Each teammate
   accepts with their own account. Do not share a login. Follow the
   [official invitation instructions](https://tailscale.com/docs/features/sharing/how-to/invite-any-user).
3. Each helper installs [Tailscale for Windows](https://tailscale.com/download/windows)
   if needed, signs in with their own account, and selects the shared tailnet.
   These browser sign-ins and invitation acceptance are manual steps.
4. Run the following on each laptop and share its Tailscale IPv4 address with
   the team. Addresses must come from the actual laptops; none is assumed here.

```powershell
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
& $tailscale status
& $tailscale ip -4
```

Keep RPC inside this trusted tailnet: it does not provide its own authentication.
The helper binds to its Tailscale address, and the master chat server stays on
localhost. The tailnet policy must allow the master to reach helper TCP 50052.
Do not expose RPC through a router/public port forward.

## First helper: teammate runs these commands on their laptop

Open PowerShell in their copy of this project containing the new `scripts/`
directory. No GGUF or `.venv` needs to be copied from the master.

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\Install-Runtime.ps1 -Role Helper
$rpc = (Resolve-Path '.runtime\llama-b12110-cpu\ggml-rpc-server.exe').Path
& .\.runtime\llama-b12110-cpu\llama-server.exe --version
& $rpc --help
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
$helperIp = (& $tailscale ip -4 | Select-Object -First 1)
if (-not $helperIp) { throw 'Sign in to the shared Tailscale network first.' }
$helperIp = $helperIp.Trim()
Write-Output "Send this helper address to the master: $helperIp"
```

If Windows Firewall blocks the connection, a teammate with administrator
access can add this narrowly scoped rule in **Administrator PowerShell**,
from their project root. Enter the master's actual Tailscale IPv4 address:

```powershell
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
$helperIp = (& $tailscale ip -4 | Select-Object -First 1).Trim()
$masterIp = Read-Host 'Master Tailscale IPv4 address'
$rpc = (Resolve-Path '.runtime\llama-b12110-cpu\ggml-rpc-server.exe').Path
New-NetFirewallRule -DisplayName 'LowLlama RPC from master' -Direction Inbound `
    -Action Allow -Protocol TCP -LocalPort 50052 -LocalAddress $helperIp `
    -RemoteAddress $masterIp -Program $rpc -Profile Any
```

Back in the helper's original PowerShell window, start CPU RPC and leave it
running. There is no model path and no disk-cache flag in this command:

```powershell
New-Item -ItemType Directory -Force .runtime\logs | Out-Null
$env:GGML_RPC_DEBUG = '1'
& $rpc --device CPU --host $helperIp --port 50052 --threads 4 `
    2>&1 | Tee-Object -FilePath .runtime\logs\helper-rpc.log
```

Expected startup includes the helper's Tailscale endpoint, `Devices: CPU`,
and `local cache : n/a`. Keep the laptop awake and plugged in.

## Master: connect to the real helper and generate

Stop any local model server first. In project-root PowerShell, enter the
address supplied by the teammate, then check connectivity and device names:

```powershell
$helperIp = (Read-Host 'First helper Tailscale IPv4 address').Trim()
$endpoint = "${helperIp}:50052"
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
& $tailscale ping $helperIp
if (-not (Test-NetConnection $helperIp -Port 50052 -InformationLevel Quiet)) {
    throw 'Helper RPC is unreachable. Check its listener, firewall, and tailnet policy.'
}
$server = (Resolve-Path '.runtime\llama-b12110-cuda\llama-server.exe').Path
$model = (Resolve-Path '.runtime\models\qwen2.5-0.5b-instruct-q4_k_m.gguf').Path
New-Item -ItemType Directory -Force .runtime\logs | Out-Null
& $server --rpc $endpoint --list-devices 2>&1 | Tee-Object .runtime\logs\remote-devices.log
```

The list must contain `CUDA0` for the master's RTX 4060 and `RPC0` for the
teammate's endpoint. Stop if RPC0 is missing. Then launch:

```powershell
$env:GGML_RPC_DEBUG = '1'
& $server -m $model --alias lowllama --host 127.0.0.1 --port 8080 `
    --rpc $endpoint --device CUDA0,RPC0 --split-mode layer --tensor-split 1,1 `
    -ngl all --fit off -c 2048 -np 1 -fa off -lv 5 `
    2>&1 | Tee-Object -FilePath .runtime\logs\master-remote.log
```

`1,1` requests a layer split across the two selected devices. Weight sizes
vary, so it does not guarantee equal bytes. Here `-ngl` controls device
offload, including the remote CPU exposed through RPC. Flash attention and
automatic fitting are disabled for this first reproducible test.

Once the model is ready, run the same `Test-Chat.ps1` command from a second
master terminal. This exact configuration passed with a localhost RPC
endpoint; the Tailscale/physical-helper run is still pending.

## Prove that two physical computers participate

A successful answer or TCP connection alone is insufficient. Keep evidence
from both laptops for the same running session:

1. Save each laptop's `hostname` and Tailscale IPv4 address locally. Confirm
   the helper endpoint is the teammate's distinct machine, not the master or
   `127.0.0.1`.
2. On the master, inspect the model loading log:

   ```powershell
   Select-String .runtime\logs\master-remote.log -Pattern 'using device|layer.*assigned|model buffer size'
   ```

   Require layers assigned to **both CUDA0 and RPC0**, nonzero model buffers
   on both, and the actual helper address in `RPC0[address:50052]`. An RPC
   compute/KV buffer alone does not prove model weights were allocated there.
3. On a second helper terminal, after model warmup and before asking a new
   question, count graph calls, then repeat after `Test-Chat.ps1` runs:

   ```powershell
   (Select-String .runtime\logs\helper-rpc.log -Pattern '\[graph_compute\]|\[graph_recompute\]').Count
   Select-String .runtime\logs\helper-rpc.log -Pattern 'alloc_buffer|graph_compute|graph_recompute' | Select-Object -Last 12
   ```

   The count must increase during the request. Startup warmup calls alone do
   not establish participation in that request. Save the helper log along
   with the master's allocation log and chat JSON.
4. Only then record the two-computer milestone as passed. The observed
   same-laptop values above are diagnostic evidence, not completion of this step.

After the first physical helper works, give the second helper the same CPU
runtime and repeat its setup. Enumerate both endpoints before choosing
`CUDA0,RPC0,RPC1` and a three-device split. That configuration is not tested yet.
Pooling resources increases available capacity; network/CPU overhead may make
this tiny model slower than running it entirely on the master's GPU.

## Existing FastAPI coordinator

Keep using the current environment and project layout. If dependencies need
syncing, run `uv pip install --python .venv/Scripts/python.exe -r requirements.txt`.
Only on a fresh checkout without `.venv`, first run `uv venv .venv --python 3.12`.

```powershell
.\.venv\Scripts\python.exe -m uvicorn coordinator:app --host 127.0.0.1 --port 8000 --reload
```

Open http://127.0.0.1:8000/docs. Running `coordinator/__init__.py` directly
does not start a server. Select `.venv\Scripts\python.exe` as the IDE
interpreter, or run module `uvicorn` with the parameters above and the project
root as the working directory.

`.gitignore` excludes environments, `.runtime/`, GGUF files, build products,
logs, `.env` files, private keys, and Streamlit secrets. Do not commit downloaded
archives, weights, credentials, Tailscale authentication links, or local logs.
