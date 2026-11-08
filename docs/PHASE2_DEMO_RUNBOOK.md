# LowLlama Phase 2 Demo Runbook

This document describes the API-controlled demonstration of the LowLlama distributed inference system.

## Prerequisites

1. **Environment Variables**:
   - `LOWLLAMA_ADMIN_TOKEN`: Set to a secure secret.
   - `LOWLLAMA_MASTER_IP`: Set to the Tailscale IP of the coordinator machine.

2. **Helpers**:
   - At least two physical helpers must be online and joined to the coordinator.
   - Use `GET /nodes` to verify `status: online` and `rpc_ready: true`.
   - **Important**: If the coordinator restarts, you MUST re-enroll helpers using a fresh invite code to get valid Node IDs.

3. **Inference Backend**:
   - Ensure the pinned model weights exist at `.runtime/models/qwen2.5-0.5b-instruct-q4_k_m.gguf`.

## Demo Steps (API Flow)

### 1. Preparation
Fetch the Tailscale IP of the master and the admin token.
Fetch current Node IDs from the coordinator:
```powershell
$base = "http://<MASTER_TAILSCALE_IP>:8000"
$headers = @{ Authorization = "Bearer <ADMIN_TOKEN>" }
Invoke-RestMethod -Uri "$base/nodes" -Headers $headers | Format-Table node_id, node_name, status, rpc_ready
```

### 2. Execution
Run `scripts/Test-Api-Flow.ps1` with explicit Tailscale URL and Node IDs:
```powershell
.\scripts\Test-Api-Flow.ps1 -CoordinatorUrl "http://<MASTER_TAILSCALE_IP>:8000" `
    -AdminToken "<ADMIN_TOKEN>" `
    -NodeIds "node-id-1", "node-id-2"
```

The script will:
- Validate nodes.
- Start the model on selected nodes.
- Poll until `ready`.
- Send a chat request.
- Stop the model (releasing resources).

## Observed Evidence Checklist (Shafin's responsibility)

During a real inference request, the following evidence should be collected from the master and helper logs:

- [ ] **Master Logs**: Successful binding to `127.0.0.1:8080` and RPC client initialization.
- [ ] **Helper Logs**: `graph_compute` or `graph_recompute` counts increasing during the request.
- [ ] **Resource Allocation**: Non-zero layer/weight allocation to RPC devices as shown in the initial `llama-server` output.

## Troubleshooting

- **503 Model Not Ready**: The model process is still starting or failed. Check `/status` for error messages.
- **400 Selected workers unavailable**: One or more requested nodes are offline, missing, or not `rpc_ready`.
- **401/403**: Invalid admin token or environment configuration.
- **Address not a Tailscale IP**: Security policy requires helpers to connect via Tailscale (100.64.x.x range).

## Cleanup
Always ensure `POST /stop` is called to terminate the inference backend and release RPC resources on helpers. The `Test-Api-Flow.ps1` script attempts this automatically in a `finally` block.
