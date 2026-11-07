from typing import List, Optional
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel, Field
import uvicorn

app = FastAPI(title="LowLlama Coordinator")


class RegistrationRequest(BaseModel):
    node_name: str = Field(..., example="Rodel's Laptop")
    ram_gb: float = Field(..., example=8.0)
    rpc_port: int = Field(..., example=50051)

class RegistrationResponse(BaseModel):
    node_id: str
    credentials: str

class HeartbeatRequest(BaseModel):
    node_id: str
    credentials: str

class HeartbeatResponse(BaseModel):
    status: str

class NodeInfo(BaseModel):
    node_id: str
    node_name: str
    ram_gb: float
    rpc_port: int
    status: str  # e.g., "online", "offline"

class InviteCreateRequest(BaseModel):
    admin_token: str

class InviteCreateResponse(BaseModel):
    invite_code: str
    expires_at: str

class JoinRequest(BaseModel):
    invite_code: str
    node_name: str
    ram_gb: float
    rpc_port: int

# --- API Endpoints ---

@app.get("/")
async def root():
    return {"message": "LowLlama coordinator running"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.post("/register", response_model=RegistrationResponse)
async def register(request: RegistrationRequest):
    # shafin will implement registry.register_node
    try:
        from coordinator import registry
        result = registry.register_node(
            name=request.node_name,
            ram=request.ram_gb,
            port=request.rpc_port
        )
        return result
    except ImportError:
        raise HTTPException(status_code=501, detail="Registry module not implemented")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(request: HeartbeatRequest):
    # shafin will implement registry.update_heartbeat
    try:
        from coordinator import registry
        success = registry.update_heartbeat(
            node_id=request.node_id,
            credentials=request.credentials
        )
        if not success:
            raise HTTPException(status_code=401, detail="Invalid node ID or credentials")
        return {"status": "ok"}
    except ImportError:
        raise HTTPException(status_code=501, detail="Registry module not implemented")

@app.get("/nodes", response_model=List[NodeInfo])
async def get_nodes():
    # shafin will implement registry.get_all_nodes
    try:
        from coordinator import registry
        nodes = registry.get_all_nodes()
        return nodes
    except ImportError:
        raise HTTPException(status_code=501, detail="Registry module not implemented")

@app.post("/invite", response_model=InviteCreateResponse)
async def create_invite(request: InviteCreateRequest):
    # Restricted to authorized administrators (simplified for now)
    if request.admin_token != "secret-admin-token":
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    # shafin will implement invites.create_invite
    try:
        from coordinator import invites
        invite = invites.create_invite()
        return invite
    except ImportError:
        raise HTTPException(status_code=501, detail="Invites module not implemented")

@app.post("/join", response_model=RegistrationResponse)
async def join(request: JoinRequest):
    # shafin will implement invites.validate_and_use_invite
    try:
        from coordinator import invites
        from coordinator import registry
        
        if not invites.validate_and_use_invite(request.invite_code):
            raise HTTPException(status_code=400, detail="Invalid or expired invite code")
            
        result = registry.register_node(
            name=request.node_name,
            ram=request.ram_gb,
            port=request.rpc_port
        )
        return result
    except ImportError:
        raise HTTPException(status_code=501, detail="Registry or Invites module not implemented")

if __name__ == "__main__":
    uvicorn.run("coordinator.main:app", host="0.0.0.0", port=8000, reload=True)