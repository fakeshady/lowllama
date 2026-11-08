# LowLlama Phase 2 Demo Runbook

This document describes the API-controlled demonstration of the LowLlama distributed inference system.

## Prerequisites

1. **Environment Variables**:
   - `LOWLLAMA_ADMIN_TOKEN`: Set to a secure secret.
   - `LOWLLAMA_MASTER_IP`: Set to the Tailscale IP of the coordinator machine.

2. **Helpers**:
   - At least one physical helper (e.g., `Itmam-Laptop` or `Shafin-Laptop`) must be online and joined to the coordinator.
   - Use `GET /nodes` to verify `status: online` and `rpc_ready: true`.

3. **Inference Backend**:
   - Ensure the pinned model weights exist at `.runtime/models/qwen2.5-0.5b-instruct-q4_k_m.gguf`.

## Demo Steps (API Flow)

### 1. Verification
Run `scripts/Test-Api-Flow.ps1` with the appropriate parameters:
```powershell
.\scripts\Test-Api-Flow.ps1 -AdminToken "your-secret" -ModelId "qwen2.5-0.5b-instruct-q4_k_m"
```

### 2. Manual Verification (Optional)
If you prefer manual steps:

- **Start**: `POST /start` with `{"model_id": "...", "node_ids": ["..."]}`.
- **Poll**: `GET /status` until status is `ready`.
- **Chat**: `POST /chat` with `{"messages": [{"role": "user", "content": "..."}]}`.
- **Stop**: `POST /stop`.

## Observed Evidence Checklist (Shafin's responsibility)

During a real inference request, the following evidence should be collected from the master and helper logs:

- [ ] **Master Logs**: Successful binding to `127.0.0.1:8080` and RPC client initialization.
- [ ] **Helper Logs**: `graph_compute` or `graph_recompute` counts increasing during the request.
- [ ] **Resource Allocation**: Non-zero layer/weight allocation to RPC devices as shown in the initial `llama-server` output.

## Troubleshooting

- **503 Model Not Ready**: The model process is still starting or failed. Check `/status` for error messages.
- **400 Selection Rejected**: Requested nodes are offline or missing required RPC capabilities.
- **401/403**: Invalid admin token or environment configuration.

## Cleanup
Always ensure `POST /stop` is called to terminate the inference backend and release RPC resources on helpers.
