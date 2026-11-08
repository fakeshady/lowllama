# ENGINE ONLY — API CONTRACT (PROVISIONAL)

This document defines the engine-facing HTTP API for the LowLlama coordinator. This is an **internal** contract between the coordinator and its helpers/admin tools. No browser frontend exists yet.

## AUTHENTICATION

All administrative and model control endpoints require an `Authorization: Bearer <LOWLLAMA_ADMIN_TOKEN>` header.
Helper endpoints (`/join`, `/heartbeat`, `/reconnect`) use either an invite code or node-specific credentials.

## ENDPOINTS

### 1. ADMIN - Management

#### `GET /health`
- **Auth:** Public (within Tailscale)
- **Response:** `{"status": "healthy"}`

#### `POST /invite`
- **Auth:** Admin Bearer
- **Request:** `{}`
- **Response:** `{"invite_code": "base64_string", "expires_at": "ISO8601"}`
- **Notes:** Codes expire after 15 minutes by default. Reusable until expiry. In-memory only.

#### `GET /nodes`
- **Auth:** Admin Bearer
- **Response:** `List[NodeInfo]`
  - `NodeInfo`: `{"node_id", "node_name", "ram_gb", "free_ram_gb", "rpc_port", "address", "rpc_ready", "status", "last_seen_s"}`

### 2. HELPER - Enrollment & Lifecycle

#### `POST /join`
- **Request:** `{"invite_code", "node_name", "ram_gb", "rpc_port", "client_id"}`
- **Response:** `{"node_id": "string", "credentials": "secret_string"}`
- **Errors:** 400 (Invalid invite), 409 (Conflict/Already enrolled), 422 (Invalid node data)

#### `POST /heartbeat`
- **Request:** `{"node_id", "credentials", "free_ram_gb", "rpc_ready"}`
- **Response:** `{"status": "ok"}`
- **Errors:** 401 (Unauthorized)

#### `POST /reconnect`
- **Request:** `{"node_id", "credentials"}`
- **Response:** `{"status": "ok"}`
- **Errors:** 401 (Unauthorized)

### 3. ADMIN - Model Control

#### `POST /start`
- **Auth:** Admin Bearer
- **Request:** `{"model_id": "string", "node_ids": ["node-1", "node-2"]}`
- **Response:** `ModelStatusResponse`
- **Notes:** All-or-nothing. Fails if any selected node is offline or lacks RPC.

#### `GET /status`
- **Auth:** Admin Bearer
- **Response:** `{"status": "stopped|starting|ready|error", "model_id", "worker_ids", "error"}`

#### `POST /stop`
- **Auth:** Admin Bearer
- **Response:** `{"status": "stopped"}`

### 4. ADMIN/USER - Inference

#### `POST /chat`
- **Auth:** Admin Bearer
- **Request:** OpenAI-compatible Chat Completions
  - `{"messages": [{"role", "content"}, ...], "max_tokens", "temperature", "stream": false}`
- **Response:** OpenAI-compatible Chat Completions JSON.
- **Errors:** 400 (Streaming requested), 503 (Model not ready)

## LIMITATIONS & POLICIES

- **Streaming:** Not supported (HTTP 400).
- **Persistence:** None. Master restart clears all invites and nodes.
- **Concurrency:** Single-model master. Only one model can run at a time.
- **Deduplication:** Helpers identified by `client_id` (UUID generated on helper side).
