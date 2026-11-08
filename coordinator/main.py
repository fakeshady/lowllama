import os
import secrets
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
import uvicorn

app = FastAPI(title="LowLlama Coordinator")

# --- Schemas ---

class RegistrationRequest(BaseModel):
    node_name: str = Field(..., min_length=1, max_length=64)
    ram_gb: float = Field(..., gt=0)
    rpc_port: int = Field(..., ge=1, le=65535)

class RegistrationResponse(BaseModel):
    node_id: str
    credentials: str

class HeartbeatRequest(BaseModel):
    node_id: str
    credentials: str
    free_ram_gb: Optional[float] = Field(None, ge=0)
    rpc_ready: Optional[bool] = None

class ReconnectRequest(BaseModel):
    node_id: str
    credentials: str

class HeartbeatResponse(BaseModel):
    status: str

class NodeInfo(BaseModel):
    node_id: str
    node_name: str
    ram_gb: float
    free_ram_gb: Optional[float] = None
    rpc_port: int
    address: Optional[str] = None
    rpc_ready: bool = False
    status: str  # "online", "offline"
    last_seen_s: Optional[float] = None

class InviteCreateRequest(BaseModel):
    pass

class InviteCreateResponse(BaseModel):
    invite_code: str
    expires_at: str

class JoinRequest(BaseModel):
    invite_code: str
    node_name: str = Field(..., min_length=1, max_length=64)
    ram_gb: float = Field(..., gt=0)
    rpc_port: int = Field(..., ge=1, le=65535)
    client_id: str = Field(..., min_length=1)

class ModelStartRequest(BaseModel):
    model_id: str
    node_ids: Optional[List[str]] = None

class ModelStatusResponse(BaseModel):
    status: str
    model_id: Optional[str] = None
    worker_ids: Optional[List[str]] = None
    error: Optional[str] = None

class ModelStopResponse(BaseModel):
    status: str

class ChatRequest(BaseModel):
    messages: List[dict]
    max_tokens: Optional[int] = Field(512, gt=0, le=4096)
    temperature: Optional[float] = Field(0.7, ge=0.0, le=2.0)
    stream: Optional[bool] = False

# --- Helpers ---

security = HTTPBearer()

def get_model_allowlist():
    try:
        from coordinator import launcher
        return launcher.MODEL_ALLOWLIST
    except ImportError:
        return {}

def verify_admin(token: str):
    admin_secret = os.environ.get("LOWLLAMA_ADMIN_TOKEN")
    if not admin_secret:
        # Fail closed if not configured
        raise HTTPException(status_code=403, detail="Admin access not configured")
    if not secrets.compare_digest(token.encode(), admin_secret.encode()):
        raise HTTPException(status_code=403, detail="Unauthorized")

def get_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    verify_admin(credentials.credentials)
    return credentials.credentials

# --- API Endpoints ---

@app.get("/")
async def root():
    return {"message": "LowLlama coordinator running"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.post("/register", response_model=RegistrationResponse)
async def register(request: RegistrationRequest, req: Request, _admin: str = Depends(get_admin)):
    verify_admin(_admin)
    from coordinator import registry
    try:
        result = registry.register_node(
            name=request.node_name,
            ram=request.ram_gb,
            port=request.rpc_port,
            address=req.client.host if req.client else None
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error during registration")

@app.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(request: HeartbeatRequest):
    from coordinator import registry
    try:
        success = registry.update_heartbeat(
            node_id=request.node_id,
            credentials=request.credentials,
            free_ram_gb=request.free_ram_gb,
            rpc_ready=request.rpc_ready
        )
        if not success:
            raise HTTPException(status_code=401, detail="Invalid node ID or credentials")
        return {"status": "ok"}
    except AttributeError:
        # Fallback if registry doesn't have update_heartbeat (should not happen if imported)
        raise HTTPException(status_code=503, detail="Registry function unavailable")
    except HTTPException:
        raise
    except Exception as e:
        if isinstance(e, registry.InvalidNodeData):
            raise HTTPException(status_code=422, detail=str(e))
        raise HTTPException(status_code=500, detail="Internal server error during heartbeat")

@app.get("/nodes", response_model=List[NodeInfo])
async def get_nodes(_admin_token: str = Depends(get_admin)):
    verify_admin(_admin_token)
    from coordinator import registry
    nodes = registry.get_all_nodes()
    return nodes

@app.post("/invite", response_model=InviteCreateResponse)
async def create_invite(request: InviteCreateRequest, _admin: str = Depends(get_admin)):
    verify_admin(_admin)
    if not os.environ.get("LOWLLAMA_MASTER_IP"):
        raise HTTPException(status_code=503, detail="set LOWLLAMA_MASTER_IP")
    
    from coordinator import invites
    try:
        invite = invites.create_invite()
        return invite
    except invites.InviteError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error during invite creation")

@app.post("/join", response_model=RegistrationResponse)
async def join(request: JoinRequest, req: Request):
    from coordinator import invites
    from coordinator import registry
    
    try:
        invites.validate(request.invite_code)
    except invites.InviteError as e:
        raise HTTPException(status_code=400, detail=str(e))
        
    try:
        result = registry.register_node(
            name=request.node_name,
            ram=request.ram_gb,
            port=request.rpc_port,
            address=req.client.host if req.client else None,
            client_id=request.client_id
        )
        return result
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        if isinstance(e, registry.NodeConflictError):
            raise HTTPException(status_code=409, detail=str(e))
        raise HTTPException(status_code=500, detail="Internal server error during join")

@app.post("/reconnect", response_model=HeartbeatResponse)
async def reconnect(request: ReconnectRequest):
    from coordinator import registry
    try:
        # Proposed reconnect_node should check node_id and credentials
        success = registry.reconnect_node(
            node_id=request.node_id,
            credentials=request.credentials
        )
        if not success:
             raise HTTPException(status_code=401, detail="Invalid node ID or credentials")
        return {"status": "ok"}
    except AttributeError:
        raise HTTPException(status_code=501, detail="Reconnection not supported by registry")
    except HTTPException:
        raise
    except Exception as e:
        if isinstance(e, registry.NodeAuthError):
            raise HTTPException(status_code=401, detail=str(e))
        raise HTTPException(status_code=500, detail="Internal server error during reconnect")

@app.post("/start", response_model=ModelStatusResponse)
async def start_model(request: ModelStartRequest, _admin: str = Depends(get_admin)):
    verify_admin(_admin)
    
    allowlist = get_model_allowlist()
    if request.model_id not in allowlist:
        raise HTTPException(status_code=400, detail=f"Model {request.model_id} not in allowlist")

    if not request.node_ids:
        raise HTTPException(status_code=400, detail="Selection must not be empty")

    try:
        from coordinator import workers
        from coordinator import launcher
    except ImportError:
        raise HTTPException(status_code=501, detail="Model control modules not yet implemented")

    # 1. Get candidate nodes for distributed inference
    try:
        selection = workers.get_candidates(request.node_ids)
        if not selection.candidates:
            # Check if any were rejected
            if getattr(selection, 'rejected', None):
                reasons = ", ".join([f"{nid}: {err}" for nid, err in selection.rejected.items()])
                raise HTTPException(status_code=400, detail=f"No usable workers. Rejected: {reasons}")
            raise HTTPException(status_code=400, detail="No usable workers found in selection")
    except HTTPException:
        raise
    except Exception as e:
        # Check if it looks like a registry or selection error that should be 400
        # If it's a generic Exception from workers.get_candidates it might be bad data
        raise HTTPException(status_code=400, detail=f"Error selecting workers: {str(e)}")

    # 2. Call launcher to start the model (offloaded to threadpool)
    try:
        result = await run_in_threadpool(launcher.start, request.model_id, selection)
        return result
    except launcher.LauncherBusyError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except (launcher.LauncherConfigurationError, launcher.LauncherSelectionError, launcher.RpcEndpointError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Launcher error: {str(e)}")

@app.get("/status", response_model=ModelStatusResponse)
async def get_model_status(_admin: str = Depends(get_admin)):
    verify_admin(_admin)
    try:
        from coordinator import launcher
        return launcher.status()
    except ImportError:
        return {"status": "stopped", "error": "Launcher module missing"}
    except Exception as e:
        return {"status": "error", "error": f"Status check failed: {str(e)}"}

@app.post("/stop", response_model=ModelStopResponse)
async def stop_model(_admin: str = Depends(get_admin)):
    verify_admin(_admin)
    try:
        from coordinator import launcher
        await run_in_threadpool(launcher.stop)
        return {"status": "stopped"}
    except ImportError:
        raise HTTPException(status_code=501, detail="Launcher module missing")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Launcher error: {str(e)}")

@app.post("/chat")
async def chat(request: ChatRequest, _admin: str = Depends(get_admin)):
    verify_admin(_admin)
    
    if request.stream:
        raise HTTPException(status_code=400, detail="Streaming not supported in MVP")

    try:
        from coordinator import launcher
        state = launcher.status()
        if state.get("status") != "ready":
            status_msg = state.get("status", "unknown")
            raise HTTPException(status_code=503, detail=f"Model not ready (status: {status_msg})")
    except ImportError:
        raise HTTPException(status_code=501, detail="Launcher module missing")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="Model status unavailable")

    import httpx
    try:
        async with httpx.AsyncClient() as client:
            # Pinned to 127.0.0.1:8080 as per launcher.SERVER_PORT
            payload = request.model_dump()
            # Explicitly set model to current active model or a known alias if needed
            # For now, llama-server usually accepts what it's given or ignores it
            
            response = await client.post(
                "http://127.0.0.1:8080/v1/chat/completions",
                json=payload,
                timeout=120.0
            )
            if response.status_code != 200:
                # Sanitize error to avoid leaking internal details but keep status
                raise HTTPException(status_code=response.status_code, detail="Inference server error")
            return response.json()
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Inference server unreachable")

if __name__ == "__main__":
    uvicorn.run("coordinator.main:app", host="127.0.0.1", port=8000, reload=True)