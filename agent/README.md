# LowLlama Helper Agent

The helper agent runs on a teammate's Windows laptop. It detects basic hardware,
registers the laptop with the LowLlama coordinator, and sends regular heartbeats
so the coordinator can track whether the helper is online.

The Python agent does not run model inference, expose HTTP endpoints, manage the
coordinator registry, or execute commands received from the coordinator. The
llama.cpp RPC server is a separate process and must be started independently.

## Supported system and prerequisites

The supported demo environment is Windows 10 or Windows 11 on an x64 laptop.
The Python code is mostly platform-independent, but other operating systems and
non-x64 llama.cpp runtimes have not been qualified for the project.

The helper laptop needs:

- Python 3.12 x64. Development and tests use Python 3.12.3.
- The repository checked out locally.
- Tailscale installed and signed in with the teammate's own account on the same
  tailnet as the master.
- Network access to the coordinator's TCP port, normally `8000`.
- The pinned llama.cpp CPU runtime for the separate RPC process.
- PowerShell and `curl.exe`, which are included with current Windows versions.

The master must run the coordinator application that defines `/register` and
`/heartbeat`. Those routes currently live in `coordinator.main:app`.

## Install Python dependencies

Run commands from the repository root. Reuse the existing `.venv` when present:

```powershell
uv pip install --python .\.venv\Scripts\python.exe -r .\requirements.txt
```

For a fresh checkout, create the environment first:

```powershell
uv venv .venv --python 3.12
uv pip install --python .\.venv\Scripts\python.exe -r .\requirements.txt
```

The helper agent's only third-party runtime dependency is `psutil`. HTTP,
configuration, logging, retry, and shutdown support use the Python standard
library. The shared `requirements.txt` already includes `psutil`; no additional
agent dependency file is required.

Prepare the pinned llama.cpp helper runtime separately:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
    -File .\scripts\Install-Runtime.ps1 -Role Helper
```

This downloads the CPU binaries only. A helper does not need the GGUF model.

## Environment variables

Set variables in the PowerShell window that will start the agent.

| Variable | Default | Purpose and validation |
| --- | --- | --- |
| `LOWLLAMA_COORDINATOR_URL` | `http://127.0.0.1:8000` | Coordinator base URL. Remote URLs must use HTTPS or a literal Tailscale IP address. |
| `LOWLLAMA_NODE_ID` | Generated UUID | Optional stable local identifier. If omitted, it is generated once and reused. The current coordinator issues a separate server node ID during registration. |
| `LOWLLAMA_NODE_NAME` | Computer hostname | Display name sent to the coordinator; 1-100 characters without control characters. |
| `LOWLLAMA_HEARTBEAT_INTERVAL_SECONDS` | `5` | Delay after a successful heartbeat; allowed range 1-3600 seconds. The default stays below the coordinator's 10-second offline cutoff. |
| `LOWLLAMA_REQUEST_TIMEOUT_SECONDS` | `10` | Timeout for each coordinator request; allowed range 0.1-300 seconds. |
| `LOWLLAMA_RECONNECTION_INTERVAL_SECONDS` | `5` | Initial retry delay; allowed range 0.1-3600 seconds. Retry backoff is capped at 60 seconds. |
| `LOWLLAMA_AUTH_TOKEN` | Unset | Reserved opaque credential configuration. The current API instead returns credentials during registration, so this value is not sent by the agent. Never commit a real value. |
| `LOWLLAMA_STATE_DIR` | Per-user state directory | Optional location for the non-secret local node ID. On Windows the default is `%LOCALAPPDATA%\LowLlama\Agent`. |

Example helper configuration:

```powershell
$env:LOWLLAMA_COORDINATOR_URL = 'http://100.100.20.30:8000'
$env:LOWLLAMA_NODE_NAME = 'UBHack Helper 1'
$env:LOWLLAMA_HEARTBEAT_INTERVAL_SECONDS = '5'
$env:LOWLLAMA_REQUEST_TIMEOUT_SECONDS = '10'
$env:LOWLLAMA_RECONNECTION_INTERVAL_SECONDS = '5'
```

Replace `100.100.20.30` with the master's real Tailscale IPv4 address. Plain
HTTP is accepted for loopback and literal Tailscale IPv4 or IPv6 addresses
because that traffic is protected by the Tailscale tunnel. Ordinary remote or
private-LAN HTTP addresses are rejected. Use HTTPS for other coordinator hosts.

Embedded URL usernames, passwords, query strings, and fragments are rejected.

## Start the helper

First confirm Tailscale and the coordinator are reachable:

```powershell
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
& $tailscale status
& $tailscale ip -4

$masterIp = '100.100.20.30'
Test-NetConnection $masterIp -Port 8000
Invoke-RestMethod "http://${masterIp}:8000/health"
```

The expected health response is `{"status":"healthy"}`.

Start llama.cpp RPC in its own PowerShell window. Bind it to this helper's
Tailscale address, not a public network interface:

```powershell
$tailscale = 'C:\Program Files\Tailscale\tailscale.exe'
$helperIp = (& $tailscale ip -4 | Select-Object -First 1).Trim()
$rpc = (Resolve-Path '.runtime\llama-b12110-cpu\ggml-rpc-server.exe').Path
$env:GGML_RPC_DEBUG = '1'
& $rpc --device CPU --host $helperIp --port 50052 --threads 4
```

In a second project-root PowerShell window, set the agent environment variables
and start the Python agent:

```powershell
$env:LOWLLAMA_COORDINATOR_URL = 'http://100.100.20.30:8000'
$env:LOWLLAMA_NODE_NAME = 'UBHack Helper 1'
.\.venv\Scripts\python.exe -m agent.agent
```

Direct execution is also supported:

```powershell
.\.venv\Scripts\python.exe .\agent\agent.py
```

The agent registers with the configured display name, detected total RAM, and
RPC port `50052`. It begins heartbeats only after registration succeeds.
Temporary failures use exponential backoff and do not discard a valid
registration. Rejected credentials cause a controlled re-registration.

## Verify registration

A successful agent console shows these state events:

```text
helper_agent_started
coordinator_request_succeeded
helper_registered
```

Further `coordinator_request_succeeded` messages confirm successful heartbeats.
If the coordinator registry is available, check its node list from another
PowerShell window:

```powershell
$coordinatorUrl = 'http://100.100.20.30:8000'
Invoke-RestMethod "$coordinatorUrl/nodes" | ConvertTo-Json -Depth 5
```

Find the configured node display name and confirm its status is `online`. On the
master, also verify RPC model allocation and graph-compute logs before claiming
that the helper contributed inference; registration alone proves only that the
agent reached the coordinator.

## Troubleshooting

### The agent keeps reporting `registration_failed`

Check Tailscale, the URL, the coordinator process, and port `8000`:

```powershell
$masterIp = '100.100.20.30'
& 'C:\Program Files\Tailscale\tailscale.exe' ping $masterIp
Test-NetConnection $masterIp -Port 8000
```

The coordinator must listen on an address reachable through Tailscale. A server
bound only to its own `127.0.0.1` cannot accept a helper connection.

The agent retries at the configured reconnection interval, then doubles the
delay up to 60 seconds. Stop the agent rather than opening several copies; each
copy can create another registration.

### The coordinator responds with 404

The wrong FastAPI application may be running. Registration and heartbeat routes
are defined in `coordinator.main:app`, not the older application in
`coordinator/__init__.py`.

### The coordinator responds with 501

The coordinator route exists, but the registry implementation is unavailable.
This is a coordinator/registry integration issue. The helper will retry with
controlled backoff.

### The URL is rejected as insecure

Use HTTPS, `http://127.0.0.1:8000` for same-machine development, or the master's
literal Tailscale address. HTTP MagicDNS names and ordinary LAN addresses are
rejected because the agent cannot prove that their transport is protected.

### Heartbeats fail after registration

The agent retains the server-issued node ID and credentials during timeouts,
connection failures, invalid responses, and server errors. It logs
`heartbeat_failed` once, retries with backoff, and logs `heartbeat_restored`
after recovery. A `401` or `403` means the credentials were rejected; the agent
waits and registers again.

### RPC is unreachable from the master

RPC uses port `50052`, while the coordinator normally uses port `8000`. Confirm
the RPC process is still running and bound to the helper's Tailscale address.
If Windows Firewall blocks it, use the narrowly scoped firewall rule described
in the root project README. Do not create a public router port forward.

### Startup reports that RAM could not be detected

The coordinator requires a numeric `ram_gb` registration field. Confirm psutil
is installed in the selected interpreter:

```powershell
.\.venv\Scripts\python.exe -c "import psutil; print(psutil.virtual_memory())"
```

## Stop the helper

Press Ctrl+C in the agent window. The signal interrupts heartbeat or retry
waiting and exits without creating background worker tasks. An HTTP request
already in progress can take up to `LOWLLAMA_REQUEST_TIMEOUT_SECONDS` to finish.

Stop `ggml-rpc-server.exe` separately with Ctrl+C in its PowerShell window.
The current coordinator contract has no deregistration endpoint, so the registry
must mark a silent helper offline after its heartbeat timeout.

## Run automated tests

Run the complete Person 2 suite from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s agent\tests -v
```

Each file can also run directly:

```powershell
.\.venv\Scripts\python.exe .\agent\tests\test_hardware.py -v
.\.venv\Scripts\python.exe .\agent\tests\test_config.py -v
.\.venv\Scripts\python.exe .\agent\tests\test_communication.py -v
.\.venv\Scripts\python.exe .\agent\tests\test_agent.py -v
```

The delivery baseline is 43 passing agent tests.

## Security considerations

- Use HTTPS for remote coordinators unless the URL contains a literal Tailscale
  address. Do not put credentials on ordinary HTTP networks.
- Do not expose coordinator port `8000` or RPC port `50052` to the public
  internet. Keep both inside the team's tailnet and restrict firewall rules.
- Coordinator-issued credentials remain in process memory. They are excluded
  from object representations and logs and are not persisted to disk.
- `LOWLLAMA_AUTH_TOKEN`, if ever required by a later contract, must come from
  the process environment or a proper secret manager. Never commit it.
- The persistent local `node_id` file is an identifier, not a credential.
- The agent collects only the requested general system fields. Registration
  currently sends the display name (hostname by default), total RAM, and RPC
  port. It does not read user files, passwords, browser data, or documents.
- The client accepts only documented response fields and never executes commands
  received from the coordinator.
- Logs intentionally omit request bodies, response bodies, and credentials.
