import os
import secrets
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Depends, Request
from pydantic import BaseModel, Field
import uvicorn

app = FastAPI(title="LowLlama Coordinator")

# --- Schemas ---

class RegistrationRequest(BaseModel):
    node_name: str = Field(..., min_length=1, max_length=64, example="MyLaptop")
    ram_gb: float = Field(..., gt=0, example=16.0)
    rpc_port: int = Field(..., ge=1, le=65535, example=50051)
    admin_token: str

class RegistrationResponse(BaseModel):
    node_id: str
    credentials: str

class HeartbeatRequest(BaseModel):
    node_id: str
    credentials: str
    free_ram_gb: Optional[float] = Field(None, ge=0)
    rpc_ready: Optional[bool] = None

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
    admin_token: str

class InviteCreateResponse(BaseModel):
    invite_code: str
    expires_at: str

class JoinRequest(BaseModel):
    invite_code: str
    node_name: str = Field(..., min_length=1, max_length=64)
    ram_gb: float = Field(..., gt=0)
    rpc_port: int = Field(..., ge=1, le=65535)
    client_id: str = Field(..., min_length=1)

# --- Helpers ---

def verify_admin(token: str):
    admin_secret = os.environ.get("LOWLLAMA_ADMIN_TOKEN")
    if not admin_secret:
        # Fail closed if not configured
        raise HTTPException(status_code=403, detail="Admin access not configured")
    if not secrets.compare_digest(token.encode(), admin_secret.encode()):
        raise HTTPException(status_code=403, detail="Unauthorized")

# --- API Endpoints ---

@app.get("/")
async def root():
    return {"message": "LowLlama coordinator running"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.post("/register", response_model=RegistrationResponse)
async def register(request: RegistrationRequest, req: Request):
    verify_admin(request.admin_token)
    try:
        from coordinator import registry
        result = registry.register_node(
            name=request.node_name,
            ram=request.ram_gb,
            port=request.rpc_port,
            address=req.client.host if req.client else None
        )
        return result
    except ImportError:
        raise HTTPException(status_code=503, detail="Registry module unavailable")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error during registration")

@app.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(request: HeartbeatRequest):
    try:
        from coordinator import registry
        success = registry.update_heartbeat(
            node_id=request.node_id,
            credentials=request.credentials,
            free_ram_gb=request.free_ram_gb,
            rpc_ready=request.rpc_ready
        )
        if not success:
            raise HTTPException(status_code=401, detail="Invalid node ID or credentials")
        return {"status": "ok"}
    except ImportError:
        raise HTTPException(status_code=503, detail="Registry module unavailable")

@app.get("/nodes", response_model=List[NodeInfo])
async def get_nodes():
    try:
        from coordinator import registry
        nodes = registry.get_all_nodes()
        return nodes
    except ImportError:
        raise HTTPException(status_code=503, detail="Registry module unavailable")

@app.post("/invite", response_model=InviteCreateResponse)
async def create_invite(request: InviteCreateRequest):
    verify_admin(request.admin_token)
    
    try:
        from coordinator import invites
        invite = invites.create_invite()
        return invite
    except ImportError:
        raise HTTPException(status_code=503, detail="Invites module unavailable")

@app.post("/join", response_model=RegistrationResponse)
async def join(request: JoinRequest, req: Request):
    try:
        from coordinator import invites
        from coordinator import registry
        
        # We use validate() to get specific errors if it fails
        try:
            invites.validate(request.invite_code)
        except Exception as e:
            # Re-wrap invite errors into 400
            raise HTTPException(status_code=400, detail=str(e))
            
        result = registry.register_node(
            name=request.node_name,
            ram=request.ram_gb,
            port=request.rpc_port,
            address=req.client.host if req.client else None
            # Note: client_id is received but not yet supported by registry.register_node
        )
        return result
    except ImportError:
        raise HTTPException(status_code=503, detail="Coordinator dependencies unavailable")
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        raise HTTPException(status_code=500, detail="Internal server error during join")

if __name__ == "__main__":
    uvicorn.run("coordinator.main:app", host="0.0.0.0", port=8000, reload=True)