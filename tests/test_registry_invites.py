"""Tests for registry.py and invites.py, and that coordinator/main.py can call them. Run from the project root:
    .venv/Scripts/python.exe -m unittest tests.test_registry_invites -v
or with every coordinator test:
    .venv/Scripts/python.exe -m unittest discover -s tests -v
"""
import os, sys, time, asyncio, base64, json, threading, unittest
from unittest import mock
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # so `coordinator` imports from the project root
from coordinator import registry, invites

MASTER_ENV={"LOWLLAMA_MASTER_IP":"100.64.0.1"}
WIRING_ENV={"LOWLLAMA_MASTER_IP":"100.64.0.1","LOWLLAMA_ADMIN_TOKEN":"test-admin-token"}   # set only inside test_main_wiring


def _encode(payload):   # build a code by hand, like an attacker would
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()


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
    with mock.patch.dict(os.environ,{"LOWLLAMA_MASTER_IP":"100.64.0.9"}):
        assert invites.decode(invites.create_invite()["invite_code"])["master_ip"]=="100.64.0.9"   # no-argument form main.py uses
    old=invites.create_invite("100.64.0.1")
    invites._invites[invites.decode(old["invite_code"])["token"]]=time.time()-1   # make it expired without sleeping
    try: invites.validate(old["invite_code"]); assert False
    except invites.InviteError as e: assert "expired" in str(e)
    assert not invites.validate_and_use_invite(old["invite_code"])
    assert invites.cancel_invite(inv["invite_code"]) and not invites.validate_and_use_invite(inv["invite_code"])
    other=invites.create_invite("100.64.0.1")
    invites.clear()   # master restarted: old codes no longer valid
    try: invites.validate(other["invite_code"]); assert False
    except invites.InviteError as e: assert "not issued" in str(e)


def test_main_wiring():
    """Call main.py's endpoint functions directly with the REAL registry/invites (no mocks, no HTTP)."""
    from fastapi import HTTPException
    from coordinator import main
    registry.clear(); invites.clear()
    req=type("FakeRequest",(),{"client":type("FakeClient",(),{"host":"127.0.0.1"})()})()   # main.py reads req.client.host for the address
    with mock.patch.dict(os.environ,WIRING_ENV):   # main.py reads the admin token and master address from the environment
        reg=asyncio.run(main.register(main.RegistrationRequest(node_name="Helper 1",ram_gb=16,rpc_port=50052),req=req, _admin="test-admin-token"))
        main.RegistrationResponse(**reg)   # raises if the shape doesn't match main.py's response model
        assert registry.get_node(reg["node_id"])["address"]=="127.0.0.1"
        try: asyncio.run(main.register(main.RegistrationRequest(node_name="X",ram_gb=8,rpc_port=50052),req=req, _admin="wrong-token")); assert False
        except HTTPException as e: assert e.status_code==403
        assert asyncio.run(main.heartbeat(main.HeartbeatRequest(node_id=reg["node_id"],credentials=reg["credentials"],free_ram_gb=9.5,rpc_ready=True)))=={"status":"ok"}
        try: asyncio.run(main.heartbeat(main.HeartbeatRequest(node_id=reg["node_id"],credentials="wrong"))); assert False
        except HTTPException as e: assert e.status_code==401
        try: asyncio.run(main.heartbeat(main.HeartbeatRequest(node_id=reg["node_id"],credentials=reg["credentials"],free_ram_gb=99))); assert False
        except HTTPException as e: assert e.status_code==422   # more free RAM than the laptop has
        nodes=asyncio.run(main.get_nodes(_admin_token="test-admin-token"))
        assert [main.NodeInfo(**n).node_name for n in nodes]==["Helper 1"] and nodes[0]["free_ram_gb"]==9.5 and nodes[0]["rpc_ready"] is True
        inv=asyncio.run(main.create_invite(main.InviteCreateRequest(), _admin="test-admin-token"))
        main.InviteCreateResponse(**inv)
        join=lambda code,cid: asyncio.run(main.join(main.JoinRequest(invite_code=code,node_name="Helper 2",ram_gb=8,rpc_port=50052,client_id=cid),req=req))
        joined=join(inv["invite_code"],"client-2")
        main.RegistrationResponse(**joined)
        assert len(registry.get_all_nodes())==2
        try: join(inv["invite_code"],"client-2"); assert False
        except HTTPException as e: assert e.status_code==409   # same install can't enroll twice
        try: join("garbage!!","client-3"); assert False
        except HTTPException as e: assert e.status_code==400
        assert asyncio.run(main.reconnect(main.ReconnectRequest(node_id=joined["node_id"],credentials=joined["credentials"])))=={"status":"ok"}
        try: asyncio.run(main.reconnect(main.ReconnectRequest(node_id=joined["node_id"],credentials="wrong"))); assert False
        except HTTPException as e: assert e.status_code==401
        assert len(registry.get_all_nodes())==2   # reconnect never adds an entry
    with mock.patch.dict(os.environ,{"LOWLLAMA_ADMIN_TOKEN":"test-admin-token"},clear=True):
        try: asyncio.run(main.create_invite(main.InviteCreateRequest(), _admin="test-admin-token")); assert False
        except HTTPException as e: assert e.status_code==503   # no master address configured


class RegistryInviteIntegrationTests(unittest.TestCase):
    """Exercise Person 3's real registry, invitation, and coordinator wiring."""

    def test_registry_behaviour(self):
        test_registry()

    def test_invitation_behaviour(self):
        test_invites()

    def test_coordinator_wiring(self):
        test_main_wiring()


class ReconnectAndIdentityTests(unittest.TestCase):
    """Stable identity: credentials are the only proof; nothing else can take over a node."""

    def setUp(self): registry.clear()

    def test_restart_reconnect_keeps_one_entry(self):
        a=registry.register_node("Helper 1",16,50052,client_id="install-a")
        for _ in range(3):   # helper restarts three times
            again=registry.reconnect_node(a["node_id"],a["credentials"],free_ram_gb=6.5,rpc_ready=True)
            self.assertEqual(again,a)   # same node_id and same credentials: idempotent
        self.assertEqual(len(registry.get_all_nodes()),1)
        node=registry.get_node(a["node_id"])
        self.assertEqual((node["free_ram_gb"],node["rpc_ready"],node["status"]),(6.5,True,"online"))

    def test_reconnect_updates_only_given_fields(self):
        a=registry.register_node("Helper 1",16,50052,address="100.64.0.2")
        registry.reconnect_node(a["node_id"],a["credentials"],name="Helper One",port=50053)
        node=registry.get_node(a["node_id"])
        self.assertEqual((node["node_name"],node["rpc_port"],node["ram_gb"],node["address"]),("Helper One",50053,16.0,"100.64.0.2"))

    def test_reconnect_brings_offline_node_back(self):
        a=registry.register_node("Helper 1",16,50052)
        registry._nodes[a["node_id"]]["last_seen"]-=registry.OFFLINE_AFTER+1
        self.assertEqual(registry.get_node(a["node_id"])["status"],"offline")
        registry.reconnect_node(a["node_id"],a["credentials"])
        self.assertEqual(registry.get_node(a["node_id"])["status"],"online")

    def test_same_display_name_makes_separate_nodes(self):
        a=registry.register_node("Laptop",16,50052,client_id="install-a")
        b=registry.register_node("Laptop",8,50052,client_id="install-b")
        self.assertNotEqual(a["node_id"],b["node_id"])
        self.assertEqual(len(registry.get_all_nodes()),2)
        self.assertFalse(registry.update_heartbeat(a["node_id"],b["credentials"]))   # same name gives no shared access

    def test_known_client_id_cannot_enroll_twice(self):
        a=registry.register_node("Helper 1",16,50052,client_id="install-a")
        with self.assertRaises(registry.NodeConflictError): registry.register_node("Helper 1",16,50052,client_id="install-a")
        self.assertEqual(len(registry.get_all_nodes()),1)
        self.assertTrue(registry.update_heartbeat(a["node_id"],a["credentials"]))   # original untouched

    def test_lost_credentials_require_removal_then_new_identity(self):
        a=registry.register_node("Helper 1",16,50052,client_id="install-a")
        self.assertTrue(registry.remove(a["node_id"]))   # admin revokes the old entry
        b=registry.register_node("Helper 1",16,50052,client_id="install-a")   # re-enroll (after a new invite)
        self.assertNotEqual(a["node_id"],b["node_id"])
        self.assertNotEqual(a["credentials"],b["credentials"])
        self.assertEqual(len(registry.get_all_nodes()),1)

    def test_id_name_or_address_alone_cannot_take_over(self):
        a=registry.register_node("Helper 1",16,50052,address="100.64.0.2",client_id="install-a")
        b=registry.register_node("Helper 2",8,50052)
        for creds in ("",None,"guess",b["credentials"],"install-a","100.64.0.2","Helper 1",a["node_id"]):
            with self.assertRaises(registry.NodeAuthError): registry.reconnect_node(a["node_id"],creds,name="Hijacked")
            self.assertFalse(registry.update_heartbeat(a["node_id"],creds))
        self.assertEqual(registry.get_node(a["node_id"])["node_name"],"Helper 1")

    def test_wrong_credentials_change_nothing(self):
        a=registry.register_node("Helper 1",16,50052)
        registry._nodes[a["node_id"]]["last_seen"]-=registry.OFFLINE_AFTER+1
        self.assertFalse(registry.update_heartbeat(a["node_id"],"wrong",free_ram_gb=1.0,rpc_ready=True))
        node=registry.get_node(a["node_id"])
        self.assertEqual((node["status"],node["free_ram_gb"],node["rpc_ready"]),("offline",None,False))

    def test_revoked_node_is_rejected(self):
        a=registry.register_node("Helper 1",16,50052)
        self.assertTrue(registry.remove(a["node_id"]))
        self.assertFalse(registry.remove(a["node_id"]))
        self.assertFalse(registry.update_heartbeat(a["node_id"],a["credentials"]))
        with self.assertRaises(registry.NodeAuthError): registry.reconnect_node(a["node_id"],a["credentials"])
        self.assertIsNone(registry.get_node(a["node_id"]))

    def test_unknown_or_odd_node_ids(self):
        for node_id in ("nobody",None,123,["node-1"]):
            self.assertFalse(registry.update_heartbeat(node_id,"x"))
            self.assertIsNone(registry.get_node(node_id))
            self.assertFalse(registry.remove(node_id))


class ValidationTests(unittest.TestCase):
    """The registry is the last trust boundary: impossible values never get stored."""

    def setUp(self): registry.clear()

    def test_bad_names(self):
        for name in (None,"","   ",42,"x"*(registry.MAX_NAME_LEN+1),"bad\nname","tab\tname"):
            with self.assertRaises(registry.InvalidNodeData): registry.register_node(name,16,50052)
        self.assertEqual(registry.get_all_nodes(),[])

    def test_name_is_trimmed(self):
        a=registry.register_node("  Helper 1  ",16,50052)
        self.assertEqual(registry.get_node(a["node_id"])["node_name"],"Helper 1")

    def test_bad_ram(self):
        for ram in (float("nan"),float("inf"),-1,0,True,"16",None,registry.MAX_RAM_GB+1):
            with self.assertRaises(registry.InvalidNodeData): registry.register_node("Helper",ram,50052)

    def test_bad_free_ram(self):
        for free in (float("nan"),float("-inf"),-0.5,17,"9"):
            with self.assertRaises(registry.InvalidNodeData): registry.register_node("Helper",16,50052,free_ram_gb=free)
        a=registry.register_node("Helper",16,50052,free_ram_gb=0)
        for free in (float("nan"),-1,16.5):
            with self.assertRaises(registry.InvalidNodeData): registry.update_heartbeat(a["node_id"],a["credentials"],free_ram_gb=free)
        self.assertEqual(registry.get_node(a["node_id"])["free_ram_gb"],0.0)   # unchanged after rejected updates

    def test_bad_ports(self):
        for port in (0,65536,-1,True,5.5,"50052",None):
            with self.assertRaises(registry.InvalidNodeData): registry.register_node("Helper",16,port)
        for port in (1,65535): registry.register_node("Helper",16,port)

    def test_rpc_ready_must_be_a_real_boolean(self):
        for flag in ("false","true",1,0,None):
            with self.assertRaises(registry.InvalidNodeData): registry.register_node("Helper",16,50052,rpc_ready=flag)
        a=registry.register_node("Helper",16,50052)
        with self.assertRaises(registry.InvalidNodeData): registry.update_heartbeat(a["node_id"],a["credentials"],rpc_ready="false")
        self.assertFalse(registry.get_node(a["node_id"])["rpc_ready"])

    def test_addresses(self):
        for good,stored in (("100.64.0.2","100.64.0.2"),(" 100.127.255.254 ","100.127.255.254"),("127.0.0.1","127.0.0.1"),
                            ("fd7a:115c:a1e0::1","fd7a:115c:a1e0::1"),("::1","::1")):
            a=registry.register_node("Helper",16,50052,address=good)
            self.assertEqual(registry.get_node(a["node_id"])["address"],stored)
        for bad in ("8.8.8.8","192.168.1.5","100.128.0.1","0.0.0.0","not-an-ip","",42,"100.64.0.2:50052"):
            with self.assertRaises(registry.InvalidNodeData): registry.register_node("Helper",16,50052,address=bad)

    def test_bad_client_ids(self):
        for cid in ("","  ",5,"x"*(registry.MAX_CLIENT_ID_LEN+1),"bad\nid"):
            with self.assertRaises(registry.InvalidNodeData): registry.register_node("Helper",16,50052,client_id=cid)

    def test_reconnect_validates_and_keeps_free_ram_consistent(self):
        a=registry.register_node("Helper",16,50052,free_ram_gb=12)
        with self.assertRaises(registry.InvalidNodeData): registry.reconnect_node(a["node_id"],a["credentials"],ram=-1)
        registry.reconnect_node(a["node_id"],a["credentials"],ram=8)   # smaller laptop now: old 12 GB free is impossible
        self.assertIsNone(registry.get_node(a["node_id"])["free_ram_gb"])
        with self.assertRaises(registry.InvalidNodeData): registry.reconnect_node(a["node_id"],a["credentials"],free_ram_gb=9)


class PublicSnapshotTests(unittest.TestCase):
    def setUp(self): registry.clear()

    def test_secrets_and_internals_never_listed(self):
        a=registry.register_node("Helper",16,50052,client_id="install-a")
        listing=registry.get_all_nodes()+[registry.get_node(a["node_id"])]+registry.online_nodes()
        for node in listing:
            for hidden in ("credentials","client_id","last_seen","joined_at"): self.assertNotIn(hidden,node)
            self.assertNotIn(a["credentials"],json.dumps(node))
        self.assertEqual(set(listing[0]),{"node_id","node_name","ram_gb","free_ram_gb","rpc_port","address","rpc_ready","status","last_seen_s"})

    def test_snapshots_are_copies(self):
        a=registry.register_node("Helper",16,50052)
        snap=registry.get_node(a["node_id"])
        snap["node_name"]="changed"; snap["status"]="offline"
        registry.get_all_nodes()[0]["ram_gb"]=999
        node=registry.get_node(a["node_id"])
        self.assertEqual((node["node_name"],node["ram_gb"]),("Helper",16.0))

    def test_online_does_not_imply_rpc_ready(self):
        registry.register_node("Helper",16,50052)
        node=registry.online_nodes()[0]
        self.assertEqual((node["status"],node["rpc_ready"]),("online",False))


class ConcurrencyTests(unittest.TestCase):
    def setUp(self): registry.clear()

    def _run(self, target, count):
        errors=[]
        def wrapped(i):
            try: target(i)
            except Exception as e: errors.append(e)
        threads=[threading.Thread(target=wrapped,args=(i,)) for i in range(count)]
        for t in threads: t.start()
        for t in threads: t.join()
        return errors

    def test_parallel_enrollments_get_unique_ids(self):
        results=[]
        self.assertEqual(self._run(lambda i: results.append(registry.register_node(f"Helper {i}",8,50052)),50),[])
        self.assertEqual(len({r["node_id"] for r in results}),50)
        self.assertEqual(len(registry.get_all_nodes()),50)

    def test_parallel_reconnects_and_heartbeats_stay_one_entry(self):
        a=registry.register_node("Helper",16,50052)
        def hit(i):
            if i%2: assert registry.update_heartbeat(a["node_id"],a["credentials"],free_ram_gb=float(i%16))
            else: assert registry.reconnect_node(a["node_id"],a["credentials"])==a
        self.assertEqual(self._run(hit,60),[])
        self.assertEqual(len(registry.get_all_nodes()),1)

    def test_competing_enrollments_of_one_client(self):
        def enroll(i): registry.register_node("Helper",16,50052,client_id="install-a")
        errors=self._run(enroll,20)
        self.assertEqual(len(errors),19)   # exactly one wins
        self.assertTrue(all(isinstance(e,registry.NodeConflictError) for e in errors))
        self.assertEqual(len(registry.get_all_nodes()),1)


class InviteSafetyTests(unittest.TestCase):
    def setUp(self): invites.clear()

    def test_refuses_missing_or_bad_master_address(self):
        with mock.patch.dict(os.environ,{},clear=True):
            with self.assertRaises(invites.InviteError) as ctx: invites.create_invite()
            self.assertIn("LOWLLAMA_MASTER_IP",str(ctx.exception))
        for ip in ("","   ","not-an-ip","0.0.0.0","::","224.0.0.1","100.64.0.1:8000",42):
            with self.assertRaises(invites.InviteError): invites.create_invite(ip)
        self.assertEqual(invites._invites,{})   # nothing half-made was stored

    def test_env_address_is_used_and_normalized(self):
        with mock.patch.dict(os.environ,{"LOWLLAMA_MASTER_IP":" 100.64.0.1 "}):
            self.assertEqual(invites.decode(invites.create_invite()["invite_code"])["master_ip"],"100.64.0.1")

    def test_bad_port_and_lifetime(self):
        for port in (0,65536,True,"8000",8000.0):
            with self.assertRaises(invites.InviteError): invites.create_invite("100.64.0.1",port=port)
        for ttl in (-1,float("nan"),float("inf"),invites.MAX_TTL_MIN+1,"15",True):
            with self.assertRaises(invites.InviteError): invites.create_invite("100.64.0.1",ttl_min=ttl)

    def test_malformed_codes(self):
        bad=[None,42,b"bytes","","   ","garbage!!","%%%%","MTIz","W10",_encode([1,2]),_encode({"master_ip":"100.64.0.1"}),
             _encode({"token":123}),"A"*(invites.MAX_CODE_LEN+1),base64.urlsafe_b64encode(b"\xff\xfe").decode()]
        for code in bad:
            with self.assertRaises(invites.InviteError): invites.decode(code)
            self.assertFalse(invites.validate_and_use_invite(code))
            self.assertFalse(invites.cancel_invite(code))

    def test_code_without_padding_and_with_spaces_still_works(self):
        code=invites.create_invite("100.64.0.1")["invite_code"]
        self.assertTrue(invites.validate_and_use_invite("  "+code.rstrip("=")+"\n"))

    def test_forged_and_tampered_codes(self):
        self.assertFalse(invites.validate_and_use_invite(_encode({"master_ip":"100.64.0.1","port":8000,"token":"made-up","expires":2**40})))
        code=invites.create_invite("100.64.0.1")["invite_code"]
        payload=invites.decode(code)
        invites._invites[payload["token"]]=time.time()-1   # this real code has expired
        payload["expires"]=2**40   # attacker rewrites the expiry inside the code
        with self.assertRaises(invites.InviteError) as ctx: invites.validate(_encode(payload))
        self.assertIn("expired",str(ctx.exception))

    def test_reusable_until_expiry_then_cancel(self):
        code=invites.create_invite("100.64.0.1")["invite_code"]
        for _ in range(5): self.assertTrue(invites.validate_and_use_invite(code))
        self.assertTrue(invites.cancel_invite(code))
        self.assertFalse(invites.cancel_invite(code))
        self.assertFalse(invites.validate_and_use_invite(code))

    def test_expired_codes_are_pruned(self):
        old=invites.create_invite("100.64.0.1")["invite_code"]
        token=invites.decode(old)["token"]
        invites._invites[token]=time.time()-1
        invites.create_invite("100.64.0.1")
        self.assertNotIn(token,invites._invites)

    def test_errors_never_echo_the_code(self):
        code=invites.create_invite("100.64.0.1")["invite_code"]
        token=invites.decode(code)["token"]
        invites._invites[token]=time.time()-1
        try: invites.validate(code)
        except invites.InviteError as e: self.assertNotIn(token,str(e)); self.assertNotIn(code,str(e))


class FakeClock:
    """Stands in for registry._now / invites._now so timeouts can be checked to the exact second."""
    def __init__(self, start=1_000_000.0): self.t=start
    def __call__(self): return self.t


class TimeoutBoundaryTests(unittest.TestCase):
    def setUp(self):
        registry.clear(); invites.clear()
        self.clock=FakeClock()
        for module in (registry,invites):
            p=mock.patch.object(module,"_now",self.clock); p.start(); self.addCleanup(p.stop)

    def test_online_until_exactly_offline_after_then_offline(self):
        a=registry.register_node("Helper",16,50052)
        self.clock.t+=registry.OFFLINE_AFTER-0.01
        self.assertEqual(registry.get_node(a["node_id"])["status"],"online")   # just before
        self.clock.t+=0.01
        self.assertEqual(registry.get_node(a["node_id"])["status"],"online")   # exactly 10 s still counts
        self.clock.t+=0.01
        self.assertEqual(registry.get_node(a["node_id"])["status"],"offline")   # just after
        self.assertEqual(registry.online_nodes(),[])

    def test_heartbeat_resets_the_timer_and_brings_node_back(self):
        a=registry.register_node("Helper",16,50052)
        self.clock.t+=60
        self.assertEqual(registry.get_node(a["node_id"])["status"],"offline")
        self.assertTrue(registry.update_heartbeat(a["node_id"],a["credentials"]))
        node=registry.get_node(a["node_id"])
        self.assertEqual((node["status"],node["last_seen_s"]),("online",0.0))
        self.clock.t+=registry.OFFLINE_AFTER+0.5
        self.assertEqual(registry.get_node(a["node_id"])["status"],"offline")

    def test_offline_node_keeps_its_old_rpc_ready_claim_but_is_not_online(self):
        a=registry.register_node("Helper",16,50052,rpc_ready=True)
        self.clock.t+=registry.OFFLINE_AFTER+1
        node=registry.get_node(a["node_id"])
        self.assertEqual((node["status"],node["rpc_ready"]),("offline",True))   # the claim is stale; status is what counts
        self.assertEqual(registry.online_nodes(),[])

    def test_invite_valid_until_exact_expiry(self):
        code=invites.create_invite("100.64.0.1",ttl_min=1)["invite_code"]
        self.clock.t+=59.5
        self.assertTrue(invites.validate_and_use_invite(code))
        self.clock.t+=0.5
        self.assertTrue(invites.validate_and_use_invite(code))   # at the stored expiry second it still works
        self.clock.t+=0.01
        self.assertFalse(invites.validate_and_use_invite(code))


class CoordinatorRestartPolicyTests(unittest.TestCase):
    """registry.clear() + invites.clear() simulate the coordinator restarting (all state is in memory)."""

    def setUp(self): registry.clear(); invites.clear()

    def test_helper_restart_keeps_identity(self):
        a=registry.register_node("Helper",16,50052,client_id="install-a")
        self.assertEqual(registry.reconnect_node(a["node_id"],a["credentials"]),a)
        self.assertEqual([n["node_id"] for n in registry.get_all_nodes()],[a["node_id"]])

    def test_coordinator_restart_invalidates_everything_until_reenrollment(self):
        old_code=invites.create_invite("100.64.0.1")["invite_code"]
        a=registry.register_node("Helper",16,50052,client_id="install-a")
        registry.clear(); invites.clear()   # coordinator restarted
        self.assertFalse(registry.update_heartbeat(a["node_id"],a["credentials"]))
        with self.assertRaises(registry.NodeAuthError): registry.reconnect_node(a["node_id"],a["credentials"])
        self.assertFalse(invites.validate_and_use_invite(old_code))   # old invites die too
        new_code=invites.create_invite("100.64.0.1")["invite_code"]   # admin issues a NEW invite
        self.assertTrue(invites.validate_and_use_invite(new_code))
        b=registry.register_node("Helper",16,50052,client_id="install-a")   # deliberate re-enrollment, same install
        self.assertNotEqual(b["node_id"],a["node_id"])   # new identity: nothing is persisted
        self.assertEqual(len(registry.get_all_nodes()),1)

    def test_client_id_alone_cannot_recover_an_existing_node(self):
        registry.register_node("Helper",16,50052,client_id="install-a")
        with self.assertRaises(registry.NodeConflictError): registry.register_node("Helper",16,50052,client_id="install-a")


class AddressTrustTests(unittest.TestCase):
    def setUp(self): registry.clear()

    def test_hostnames_and_proxy_style_values_are_rejected(self):
        for bad in ("testclient","localhost","100.64.0.2, 10.0.0.1","::ffff:8.8.8.8","169.254.1.1"):
            with self.assertRaises(registry.InvalidNodeData): registry.register_node("Helper",16,50052,address=bad)


if __name__=="__main__":
    unittest.main(verbosity=2)
