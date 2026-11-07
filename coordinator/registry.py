"""Node registry: stores every laptop that joined and works out online/offline.

coordinator/main.py calls these functions from the endpoints:
    POST /register, POST /join -> registry.register_node(name, ram, port)        -> {"node_id", "credentials"}
    POST /heartbeat            -> registry.update_heartbeat(node_id, credentials) -> True/False
    GET  /nodes                -> registry.get_all_nodes()
Proposed (needs Rodel to expose it, Itmam to call it):
    reconnect                  -> registry.reconnect_node(node_id, credentials, ...) -> same {"node_id", "credentials"}

Identity rules:
    - The server issues node_id + credentials. Only the credentials prove who a node is.
    - client_id (the helper's local install ID), name, address or node_id alone never authorize anything.
    - A helper that restarts calls reconnect_node with its saved credentials: same entry, no duplicate.
    - A helper that lost its credentials cannot take over its old entry: an admin calls remove() and the
      helper enrolls again with a new invite, getting a new node_id (identity rotation).
Everything lives in memory: restarting the coordinator forgets every node, and helpers must enroll again.

Errors (all messages are safe to show; none contain secrets):
    InvalidNodeData (a ValueError) -> bad name/RAM/port/address/flag            -> HTTP 400 or 422
    NodeAuthError                  -> unknown, revoked or wrong-credential node -> HTTP 401
    NodeConflictError              -> client_id already enrolled                -> HTTP 409
"""
import time, threading, secrets, math, ipaddress

OFFLINE_AFTER=10   # seconds without a heartbeat before a node counts as offline
DEFAULT_RPC_PORT=50052
MAX_NAME_LEN=64
MAX_RAM_GB=4096   # sanity bound, far above any laptop
MAX_CLIENT_ID_LEN=128
_TAILSCALE_NETS=(ipaddress.ip_network("100.64.0.0/10"),ipaddress.ip_network("fd7a:115c:a1e0::/48"))   # Tailscale's address ranges
_nodes={}          # node_id -> dict of that laptop's info (includes its secret credentials)
_by_client={}      # client_id -> node_id, to spot a second enrollment of the same install
_lock=threading.Lock()   # one lock for all nodes; every read and write goes through it
_HIDDEN=("credentials","last_seen","joined_at","client_id")   # never sent back from /nodes


class InvalidNodeData(ValueError): pass
class NodeAuthError(Exception): pass
class NodeConflictError(Exception): pass


def register_node(name, ram, port=DEFAULT_RPC_PORT, address=None, free_ram_gb=None, rpc_ready=False, client_id=None):
    """Enroll a laptop. Returns its new node_id and the secret credentials it must keep and send back.

    Callers must check an invite (or admin rights) before calling this. Raises InvalidNodeData or NodeConflictError.
    """
    fields={"node_name":_clean_name(name),"ram_gb":_clean_ram(ram,"ram_gb"),"rpc_port":_clean_port(port),
            "address":_clean_address(address),"rpc_ready":_clean_flag(rpc_ready,"rpc_ready")}
    fields["free_ram_gb"]=_clean_free_ram(free_ram_gb,fields["ram_gb"])
    client_id=_clean_client_id(client_id)
    credentials=secrets.token_urlsafe(24)
    now=time.time()
    with _lock:
        if client_id is not None and client_id in _by_client:
            raise NodeConflictError("this helper is already enrolled; reconnect with its saved credentials, or ask the admin to remove the old entry")
        node_id=_new_node_id()
        _nodes[node_id]={"node_id":node_id,**fields,"credentials":credentials,"client_id":client_id,"joined_at":now,"last_seen":now}
        if client_id is not None: _by_client[client_id]=node_id
    return {"node_id":node_id,"credentials":credentials}


def reconnect_node(node_id, credentials, name=None, ram=None, port=None, free_ram_gb=None, rpc_ready=None, address=None):
    """Let an enrolled laptop come back after a restart, proven by its credentials. Idempotent: no new entry, same credentials.

    Only the fields you pass are updated. Raises NodeAuthError or InvalidNodeData.
    """
    updates={}
    if name is not None: updates["node_name"]=_clean_name(name)
    if ram is not None: updates["ram_gb"]=_clean_ram(ram,"ram_gb")
    if port is not None: updates["rpc_port"]=_clean_port(port)
    if address is not None: updates["address"]=_clean_address(address)
    if rpc_ready is not None: updates["rpc_ready"]=_clean_flag(rpc_ready,"rpc_ready")
    with _lock:
        node=_authenticated(node_id,credentials)
        if node is None: raise NodeAuthError("unknown node or wrong credentials")
        total=updates.get("ram_gb",node["ram_gb"])
        if free_ram_gb is not None: updates["free_ram_gb"]=_clean_free_ram(free_ram_gb,total)
        elif node["free_ram_gb"] is not None and node["free_ram_gb"]>total: updates["free_ram_gb"]=None   # old value no longer possible
        node.update(updates)
        node["last_seen"]=time.time()   # coordinator's own clock
        return {"node_id":node["node_id"],"credentials":node["credentials"]}


def update_heartbeat(node_id, credentials, free_ram_gb=None, rpc_ready=None):
    """Mark a laptop as still alive. False if the node_id is unknown, removed, or the credentials are wrong.

    rpc_ready is only what the helper claims; the launcher must still probe the RPC port itself.
    Raises InvalidNodeData for impossible values (negative/NaN RAM, a non-boolean rpc_ready).
    """
    if rpc_ready is not None: rpc_ready=_clean_flag(rpc_ready,"rpc_ready")
    with _lock:
        node=_authenticated(node_id,credentials)
        if node is None: return False
        if free_ram_gb is not None: node["free_ram_gb"]=_clean_free_ram(free_ram_gb,node["ram_gb"])
        if rpc_ready is not None: node["rpc_ready"]=rpc_ready
        node["last_seen"]=time.time()   # coordinator's own clock, not the helper's
        return True


def get_all_nodes():
    """Every laptop with its current status, ready to return from GET /nodes. Copies; credentials never included."""
    with _lock: return [_public(n) for n in _nodes.values()]


def online_nodes():
    """Only laptops whose agent is online right now. Online does NOT mean its RPC server works."""
    return [n for n in get_all_nodes() if n["status"]=="online"]


def get_node(node_id):
    """One laptop's public snapshot, or None."""
    with _lock:
        node=_nodes.get(node_id) if isinstance(node_id,str) else None
        return _public(node) if node else None


def remove(node_id):
    """Kick a laptop out (revoke it). Its credentials stop working at once. False if it wasn't there."""
    with _lock:
        node=_nodes.pop(node_id,None) if isinstance(node_id,str) else None
        if node is None: return False
        if node["client_id"] is not None: _by_client.pop(node["client_id"],None)
        return True


def clear():
    with _lock: _nodes.clear(); _by_client.clear()


def _authenticated(node_id, credentials):
    """The stored record if node_id exists and the credentials match, else None. Call with _lock held."""
    node=_nodes.get(node_id) if isinstance(node_id,str) else None
    if node is None or not _same_secret(node["credentials"],credentials): return None
    return node


def _same_secret(expected, given):
    if not isinstance(given,str): return False   # missing, number, null, etc.
    return secrets.compare_digest(expected.encode(),given.encode())   # constant time; bytes avoid TypeError on non-ASCII


def _new_node_id():
    while True:   # call with _lock held
        node_id="node-"+secrets.token_hex(4)   # e.g. node-9f3a1c2b
        if node_id not in _nodes: return node_id


def _clean_name(name):
    if not isinstance(name,str): raise InvalidNodeData("node name must be text")
    name=name.strip()
    if not name: raise InvalidNodeData("node name is required")
    if len(name)>MAX_NAME_LEN: raise InvalidNodeData(f"node name is longer than {MAX_NAME_LEN} characters")
    if not name.isprintable(): raise InvalidNodeData("node name contains control characters")
    return name


def _number(value, label):
    if isinstance(value,bool) or not isinstance(value,(int,float)): raise InvalidNodeData(f"{label} must be a number")
    value=float(value)
    if not math.isfinite(value): raise InvalidNodeData(f"{label} must be a finite number")   # rejects NaN and Infinity
    return value


def _clean_ram(value, label):
    value=_number(value,label)
    if not 0<value<=MAX_RAM_GB: raise InvalidNodeData(f"{label} must be above 0 and at most {MAX_RAM_GB}")
    return value


def _clean_free_ram(value, total):
    if value is None: return None
    value=_number(value,"free_ram_gb")
    if value<0: raise InvalidNodeData("free_ram_gb cannot be negative")
    if value>total: raise InvalidNodeData("free_ram_gb cannot be more than ram_gb")
    return value


def _clean_port(port):
    if isinstance(port,bool) or not isinstance(port,int): raise InvalidNodeData("rpc_port must be a whole number")
    if not 1<=port<=65535: raise InvalidNodeData("rpc_port must be between 1 and 65535")
    return port


def _clean_flag(value, label):
    if not isinstance(value,bool): raise InvalidNodeData(f"{label} must be true or false")   # bool("false") would be True
    return value


def _clean_address(address):
    """Tailscale IP or loopback only. main.py should pass the address the request actually came from, not a client claim."""
    if address is None: return None
    if not isinstance(address,str): raise InvalidNodeData("address must be an IP address")
    try: ip=ipaddress.ip_address(address.strip())
    except ValueError: raise InvalidNodeData("address must be an IP address") from None
    if not (ip.is_loopback or any(ip in net for net in _TAILSCALE_NETS)):
        raise InvalidNodeData("address must be a Tailscale IP (100.64.0.0/10 or fd7a:115c:a1e0::/48) or loopback")
    return str(ip)


def _clean_client_id(client_id):
    if client_id is None: return None
    if not isinstance(client_id,str) or not client_id.strip(): raise InvalidNodeData("client_id must be non-empty text")
    client_id=client_id.strip()
    if len(client_id)>MAX_CLIENT_ID_LEN or not client_id.isprintable(): raise InvalidNodeData("client_id is too long or has control characters")
    return client_id


def _public(node):
    age=time.time()-node["last_seen"]
    out={k:v for k,v in node.items() if k not in _HIDDEN}   # new dict of plain values, safe to hand out
    out["status"]="online" if age<=OFFLINE_AFTER else "offline"
    out["last_seen_s"]=round(age,1)   # seconds since last heartbeat, nice for the dashboard
    return out
