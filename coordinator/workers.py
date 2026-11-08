"""Worker candidates: a read-only, advisory view of which helpers the launcher MAY try to use.

What this module does:
    - Reads registry public snapshots (registry.get_all_nodes()). It never changes registry state,
      never opens network connections, and never sees credentials.
    - Returns CANDIDATES plus a list of REJECTED nodes with a reason, so the launcher can show why a
      laptop was left out instead of silently ignoring it.

What it does NOT do (Itmam's launcher must):
    - Prove the RPC port is reachable or that llama.cpp accepts it.
    - Prove a model fits. free_ram_gb is a REPORTED planning hint, not a guarantee.
    - Prove a node computed anything. "online", "rpc_ready" (helper's TCP self-check) and
      "verified compute participation" are three different things.

Eligibility rules (a node is a candidate only if ALL hold):
    1. status == "online"                 (heartbeat within registry.OFFLINE_AFTER seconds)
    2. rpc_ready is True                  (helper reported its RPC listener is up)
    3. address is a Tailscale IP          (loopback only if allow_loopback=True, for same-laptop tests)
    4. rpc_port is a whole number 1..65535
    5. its address:port is not already taken by an earlier candidate
    6. if min_free_ram_gb is given: free_ram_gb is known AND >= min_free_ram_gb
       (unknown free RAM is never assumed to be enough)

Ordering (deterministic):
    - With selected_node_ids: the order you selected (that order becomes llama.cpp's RPC0, RPC1, ...).
    - Without: most reported free RAM first, unknown free RAM last, ties broken by node_id.

SAMPLE DATA (made up, for the launcher's input format only; not from real laptops):
    >>> sel = get_candidate_workers()
    >>> sel.as_dict()
    {"candidates": [
        {"node_id": "node-1a2b3c4d", "node_name": "Shafin-16GB", "address": "100.101.102.103",
         "rpc_port": 50052, "endpoint": "100.101.102.103:50052", "ram_gb": 16.0, "free_ram_gb": 9.5,
         "rpc_ready": true, "status": "online", "last_seen_s": 1.2}],
     "rejected": [
        {"node_id": "node-5e6f7a8b", "node_name": "Rodel-8GB", "reason": "offline"}]}
    >>> sel.rpc_endpoints()      # ready for llama-server --rpc
    "100.101.102.103:50052"
"""
import ipaddress
from dataclasses import dataclass, asdict
from coordinator import registry

# reasons a node is not a candidate
OFFLINE="offline"
RPC_NOT_READY="rpc_not_ready"
NO_ADDRESS="no_address"
INVALID_ADDRESS="invalid_address"
NOT_TAILSCALE="not_tailscale_address"
INVALID_PORT="invalid_port"
DUPLICATE_ENDPOINT="duplicate_endpoint"
FREE_RAM_UNKNOWN="free_ram_unknown"
NOT_ENOUGH_FREE_RAM="not_enough_free_ram"
UNKNOWN_NODE="unknown_node"
NOT_SELECTED="not_selected"


@dataclass(frozen=True)
class WorkerEndpoint:
    """One candidate helper. No credentials, client IDs or invite data, by construction."""
    node_id: str
    node_name: str
    address: str
    rpc_port: int
    ram_gb: float
    free_ram_gb: float | None   # reported by the helper; None = unknown
    rpc_ready: bool            # reported by the helper; not proof the launcher can use it
    status: str
    last_seen_s: float

    @property
    def endpoint(self):
        """host:port for llama-server --rpc (IPv6 in brackets)."""
        host=f"[{self.address}]" if ":" in self.address else self.address
        return f"{host}:{self.rpc_port}"

    def as_dict(self):
        return {**asdict(self),"endpoint":self.endpoint}


@dataclass(frozen=True)
class RejectedWorker:
    node_id: str
    node_name: str | None   # None when the selected node_id doesn't exist
    reason: str

    def as_dict(self): return asdict(self)


@dataclass(frozen=True)
class CandidateSelection:
    candidates: tuple   # WorkerEndpoint, in launcher order
    rejected: tuple     # RejectedWorker, in the order nodes were considered

    def rpc_endpoints(self):
        """Comma-separated endpoints in candidate order, e.g. for llama-server --rpc. Empty string if none."""
        return ",".join(w.endpoint for w in self.candidates)

    def selected_rejections(self):
        """Rejections of nodes the caller ASKED for (everything except not_selected).

        Non-empty means an explicit selection cannot be honoured in full, so /start should refuse with these
        reasons instead of silently starting with fewer workers than were selected.
        """
        return tuple(r for r in self.rejected if r.reason!=NOT_SELECTED)

    def as_dict(self):
        return {"candidates":[w.as_dict() for w in self.candidates],"rejected":[r.as_dict() for r in self.rejected]}


def get_candidate_workers(selected_node_ids=None, min_free_ram_gb=None, allow_loopback=False):
    """Advisory list of helpers the launcher may try. Read-only; see the module docstring for the rules.

    selected_node_ids: optional list of node_ids to consider (in this order); others are reported as not_selected.
    min_free_ram_gb:  optional minimum REPORTED free RAM; nodes with unknown free RAM are then rejected.
    allow_loopback:   accept 127.0.0.1 / ::1 addresses (same-laptop testing only; never a real distributed setup).
    Raises ValueError for bad arguments.
    """
    selected=_clean_selection(selected_node_ids)
    min_free=_clean_min_free(min_free_ram_gb)
    snapshots={n["node_id"]:n for n in registry.get_all_nodes()}   # copies; registry is not touched again
    candidates,rejected,seen=[],[],set()

    if selected is None:
        order=sorted(snapshots.values(),key=_free_ram_order)
    else:
        order=[]
        for node_id in selected:
            if node_id in snapshots: order.append(snapshots[node_id])
            else: rejected.append(RejectedWorker(node_id,None,UNKNOWN_NODE))

    for node in order:
        reason=_ineligibility(node,min_free,allow_loopback)
        endpoint=None if reason else _endpoint_key(node)
        if reason is None and endpoint in seen: reason=DUPLICATE_ENDPOINT
        if reason: rejected.append(RejectedWorker(node["node_id"],node["node_name"],reason)); continue
        seen.add(endpoint)
        candidates.append(WorkerEndpoint(node["node_id"],node["node_name"],str(ipaddress.ip_address(node["address"])),node["rpc_port"],
                                         node["ram_gb"],node["free_ram_gb"],node["rpc_ready"],node["status"],node["last_seen_s"]))

    if selected is not None:
        chosen=set(selected)
        for node in sorted(snapshots.values(),key=lambda n: n["node_id"]):
            if node["node_id"] not in chosen: rejected.append(RejectedWorker(node["node_id"],node["node_name"],NOT_SELECTED))
    return CandidateSelection(tuple(candidates),tuple(rejected))


def get_candidates(node_ids=None):
    """The name coordinator/main.py's POST /start calls: same as get_candidate_workers(selected_node_ids=node_ids).

    Returns a CandidateSelection (.candidates, .rejected, .rpc_endpoints(), .as_dict()). /start requires a
    non-empty node_ids list and refuses (400) when .candidates is empty; .rejected says why each node was left out.
    Partial-selection policy: if .selected_rejections() is non-empty, one of the chosen nodes is unusable and
    /start must refuse with those reasons rather than start with fewer workers.
    Labels: a candidate is only "worth probing". "RPC-compatible" needs the launcher's --list-devices probe, and
    "proved computing" needs remote model-weight allocation plus fresh graph_compute lines on that helper.
    """
    return get_candidate_workers(selected_node_ids=node_ids)


def resource_summary():
    """Counts and REPORTED totals for a dashboard. Not a capacity guarantee or a contribution measure."""
    nodes=registry.get_all_nodes()
    selection=get_candidate_workers()
    known=[w.free_ram_gb for w in selection.candidates if w.free_ram_gb is not None]
    return {
        "registered_nodes":len(nodes),
        "online_agents":sum(1 for n in nodes if n["status"]=="online"),
        "reported_rpc_ready_online":sum(1 for n in nodes if n["status"]=="online" and n["rpc_ready"]),
        "candidate_workers":len(selection.candidates),
        "candidate_reported_free_ram_gb":round(sum(known),2),
        "candidates_with_unknown_free_ram":len(selection.candidates)-len(known),
        "note":"reported by helpers; the launcher must still verify RPC and actual computation",
    }


def _ineligibility(node, min_free, allow_loopback):
    if node.get("status")!="online": return OFFLINE
    if node.get("rpc_ready") is not True: return RPC_NOT_READY
    address=node.get("address")
    if address is None or address=="": return NO_ADDRESS
    if not isinstance(address,str): return INVALID_ADDRESS   # ip_address(42) would quietly mean 0.0.0.42
    try: ip=ipaddress.ip_address(address)
    except (ValueError,TypeError): return INVALID_ADDRESS
    tailscale=any(ip.version==net.version and ip in net for net in registry._TAILSCALE_NETS)
    if not (tailscale or (allow_loopback and ip.is_loopback)): return NOT_TAILSCALE
    port=node.get("rpc_port")
    if isinstance(port,bool) or not isinstance(port,int) or not 1<=port<=65535: return INVALID_PORT
    if min_free is not None:
        free=node.get("free_ram_gb")
        if free is None: return FREE_RAM_UNKNOWN
        if free<min_free: return NOT_ENOUGH_FREE_RAM
    return None


def _endpoint_key(node):
    return (str(ipaddress.ip_address(node["address"])),node["rpc_port"])


def _free_ram_order(node):
    free=node.get("free_ram_gb")
    return (free is None,-(free or 0.0),node["node_id"])   # most free RAM first, unknown last, then by id


def _clean_selection(selected_node_ids):
    if selected_node_ids is None: return None
    if isinstance(selected_node_ids,str) or not hasattr(selected_node_ids,"__iter__"):
        raise ValueError("selected_node_ids must be a list of node IDs")
    out=[]
    for node_id in selected_node_ids:
        if not isinstance(node_id,str) or not node_id.strip(): raise ValueError("each selected node ID must be non-empty text")
        if node_id not in out: out.append(node_id)   # drop repeats, keep first position
    return out


def _clean_min_free(value):
    if value is None: return None
    if isinstance(value,bool) or not isinstance(value,(int,float)) or value!=value or value<0 or value==float("inf"):
        raise ValueError("min_free_ram_gb must be a finite number >= 0")
    return float(value)
