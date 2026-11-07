"""Quick checks for registry.py and invites.py, and that coordinator/main.py can call them. Run from the project root:
    python tests/test_registry_invites.py
"""
import os, sys, time, asyncio, unittest
# Set dummy admin token for wiring tests
os.environ["LOWLLAMA_ADMIN_TOKEN"] = "test-admin-token"
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # so `coordinator` imports from the project root
from coordinator import registry, invites


def test_registry():
    registry.clear()
    a=registry.register_node("Helper 1",16,50052,address="100.64.0.2",free_ram_gb=9.5)
    b=registry.register_node("Helper 2",8,50052)
    assert set(a)=={"node_id","credentials"} and a["node_id"]!=b["node_id"]
    assert len(registry.get_all_nodes())==2
    nodes={n["node_id"]:n for n in registry.get_all_nodes()}
    n1=nodes[a["node_id"]]
    assert n1["node_name"]=="Helper 1" and n1["ram_gb"]==16.0 and n1["rpc_port"]==50052 and n1["status"]=="online"
    assert "credentials" not in n1,"/nodes must never leak credentials"
    assert registry.update_heartbeat(a["node_id"],a["credentials"],free_ram_gb=7.0,rpc_ready=True)
    assert registry.get_node(a["node_id"])["free_ram_gb"]==7.0 and registry.get_node(a["node_id"])["rpc_ready"]
    assert not registry.update_heartbeat(a["node_id"],b["credentials"])   # someone else's credentials
    assert not registry.update_heartbeat("nobody",a["credentials"])
    for bad in (None,12345,"tøken"): assert not registry.update_heartbeat(a["node_id"],bad)   # False, not a crash
    registry._nodes[b["node_id"]]["last_seen"]-=registry.OFFLINE_AFTER+1   # pretend 11 s of silence
    nodes={n["node_id"]:n for n in registry.get_all_nodes()}
    assert nodes[b["node_id"]]["status"]=="offline" and nodes[a["node_id"]]["status"]=="online"
    assert [n["node_id"] for n in registry.online_nodes()]==[a["node_id"]]
    assert registry.update_heartbeat(b["node_id"],b["credentials"])   # comes back
    assert registry.get_node(b["node_id"])["status"]=="online"
    assert registry.remove(b["node_id"]) and not registry.update_heartbeat(b["node_id"],b["credentials"])   # kicked out
    try: registry.register_node("",8,50052); assert False
    except ValueError: pass
    print("registry: OK")


def test_invites():
    invites.clear()
    inv=invites.create_invite("100.64.0.1")
    assert set(inv)=={"invite_code","expires_at"} and isinstance(inv["expires_at"],str)
    assert invites.decode(inv["invite_code"])["master_ip"]=="100.64.0.1"
    assert invites.validate(inv["invite_code"])["port"]==8000
    assert invites.validate_and_use_invite(inv["invite_code"])
    assert invites.validate_and_use_invite(inv["invite_code"])   # reusable by a second helper
    for bad in ("garbage!!","MTIz",None,""):   # MTIz = base64 of 123: valid JSON but not an object
        assert not invites.validate_and_use_invite(bad)
    os.environ["LOWLLAMA_MASTER_IP"]="100.64.0.9"
    assert invites.decode(invites.create_invite()["invite_code"])["master_ip"]=="100.64.0.9"   # no-argument form main.py uses
    del os.environ["LOWLLAMA_MASTER_IP"]
    old=invites.create_invite("100.64.0.1",ttl_min=0)   # expires immediately
    time.sleep(1.1)
    try: invites.validate(old["invite_code"]); assert False
    except invites.InviteError as e: assert "expired" in str(e)
    assert not invites.validate_and_use_invite(old["invite_code"])
    assert invites.cancel_invite(inv["invite_code"]) and not invites.validate_and_use_invite(inv["invite_code"])
    other=invites.create_invite("100.64.0.1")
    invites.clear()   # master restarted: old codes no longer valid
    try: invites.validate(other["invite_code"]); assert False
    except invites.InviteError as e: assert "not issued" in str(e)
    print("invites: OK")


def test_main_wiring():
    """Call main.py's endpoint functions directly with the REAL registry/invites (no mocks, no HTTP)."""
    from fastapi import HTTPException, Request
    from coordinator import main
    registry.clear(); invites.clear()
    
    # Required for /invite
    os.environ["LOWLLAMA_MASTER_IP"] = "100.64.0.1"
    
    # Mock request for address
    mock_req = type("MockRequest", (), {"client": type("MockClient", (), {"host": "127.0.0.1"})()})()

    # Now requires admin_token and req
    reg=asyncio.run(main.register(main.RegistrationRequest(
        node_name="Helper 1", ram_gb=16, rpc_port=50052, admin_token="test-admin-token"
    ), req=mock_req))
    main.RegistrationResponse(**reg)   # raises if the shape doesn't match main.py's response model
    assert asyncio.run(main.heartbeat(main.HeartbeatRequest(node_id=reg["node_id"],credentials=reg["credentials"])))=={"status":"ok"}
    try: asyncio.run(main.heartbeat(main.HeartbeatRequest(node_id=reg["node_id"],credentials="wrong"))); assert False
    except HTTPException as e: assert e.status_code==401
    nodes=asyncio.run(main.get_nodes())
    assert [main.NodeInfo(**n).node_name for n in nodes]==["Helper 1"]
    # Update admin token from old "secret-admin-token"
    inv=asyncio.run(main.create_invite(main.InviteCreateRequest(admin_token="test-admin-token")))
    main.InviteCreateResponse(**inv)
    # Now requires client_id and req
    joined=asyncio.run(main.join(main.JoinRequest(
        invite_code=inv["invite_code"], node_name="Helper 2", ram_gb=8, rpc_port=50052, client_id="client-2"
    ), req=mock_req))
    main.RegistrationResponse(**joined)
    assert len(registry.get_all_nodes())==2
    try: asyncio.run(main.join(main.JoinRequest(
        invite_code="garbage!!", node_name="X", ram_gb=8, rpc_port=50052, client_id="client-3"
    ), req=mock_req)); assert False
    except HTTPException as e: assert e.status_code==400
    print("main.py wiring: OK")


class RegistryInviteIntegrationTests(unittest.TestCase):
    """Exercise Person 3's real registry, invitation, and coordinator wiring."""

    def test_registry_behaviour(self):
        test_registry()

    def test_invitation_behaviour(self):
        test_invites()

    def test_coordinator_wiring(self):
        test_main_wiring()


if __name__=="__main__":
    unittest.main(verbosity=2)
