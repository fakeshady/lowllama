"""Node registry: stores every laptop that joined and works out online/offline.

coordinator/main.py calls these functions from the endpoints:
    POST /register, POST /join -> registry.register_node(name, ram, port)  -> {"node_id", "credentials"}
    POST /heartbeat            -> registry.update_heartbeat(node_id, credentials) -> True/false
    GET  /nodes                -> registry.get_all_nodes()
"""
import time, threading, secrets

OFFLINE_AFTER=10   # seconds without a heartbeat before a node counts as offline
DEFAULT_RPC_PORT=50052
_nodes={}          # node_id -> dict of that laptop's info (includes its secret credentials)
_lock=threading.Lock()   # endpoints can run on several threads at once
_HIDDEN=("credentials","last_seen","joined_at")   # never sent back from /nodes


def register_node(name, ram, port=DEFAULT_RPC_PORT, address=None, free_ram_gb=None, rpc_ready=False):
    """Add a laptop. Returns its new node_id and the secret credentials it must send with every heartbeat."""
    name=str(name or "").strip()
    if not name: raise ValueError("node name is required")
    node_id="node-"+secrets.token_hex(4)   # e.g. node-9f3a1c2b
    credentials=secrets.token_urlsafe(24)
    now=time.time()
    with _lock:
        _nodes[node_id]={
            "node_id":node_id,
            "node_name":name,
            "ram_gb":float(ram),
            "rpc_port":int(port),
            "address":address,   # Tailscale IP, e.g. 100.x.x.x (main.py doesn't send it yet)
            "free_ram_gb":free_ram_gb,
            "rpc_ready":bool(rpc_ready),
            "credentials":credentials,
            "joined_at":now,
            "last_seen":now,
        }
    return {"node_id":node_id,"credentials":credentials}


def update_heartbeat(node_id, credentials, free_ram_gb=None, rpc_ready=None):
    """Mark a laptop as still alive. False if the node_id is unknown or the credentials are wrong."""
    with _lock:
        node=_nodes.get(node_id)
        if node is None or not _same_secret(node["credentials"],credentials): return False
        node["last_seen"]=time.time()   # coordinator's own clock, not the helper's
        if free_ram_gb is not None: node["free_ram_gb"]=free_ram_gb
        if rpc_ready is not None: node["rpc_ready"]=bool(rpc_ready)
        return True


def get_all_nodes():
    """Every laptop with its current status, ready to return from GET /nodes."""
    with _lock: return [_public(n) for n in _nodes.values()]


def online_nodes():
    """Only laptops that are online right now (for the launcher later)."""
    return [n for n in get_all_nodes() if n["status"]=="online"]


def get_node(node_id):
    with _lock:
        node=_nodes.get(node_id)
        return _public(node) if node else None


def remove(node_id):
    """Kick a laptop out of the pool. Returns False if it wasn't there."""
    with _lock: return _nodes.pop(node_id,None) is not None


def clear():
    with _lock: _nodes.clear()


def _same_secret(expected, given):
    if not isinstance(given,str): return False   # missing, number, null, etc.
    return secrets.compare_digest(expected.encode(),given.encode())   # constant time; bytes avoid TypeError on non-ASCII


def _public(node):
    age=time.time()-node["last_seen"]
    out={k:v for k,v in node.items() if k not in _HIDDEN}
    out["status"]="online" if age<=OFFLINE_AFTER else "offline"
    out["last_seen_s"]=round(age,1)   # seconds since last heartbeat, nice for the dashboard
    return out
