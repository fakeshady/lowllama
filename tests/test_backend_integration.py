"""Backend integration tests: real HTTP requests through coordinator/main.py with the REAL registry and invites.

Run from the project root:
    .venv/Scripts/python.exe -m unittest tests.test_backend_integration -v

Transport: FastAPI's TestClient (client address forced to 127.0.0.1) when its httpx2/httpx dependency is
installed; otherwise a real uvicorn server on 127.0.0.1 with urllib. Both send real HTTP requests to the
real app. Each test clears registry/invite state and patches environment variables only for its own duration.
No real secrets are used.
"""
import json, os, socket, sys, threading, time, unittest, urllib.error, urllib.request
from unittest import mock
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # so `coordinator` imports from the project root
from coordinator import registry, invites
from coordinator.main import app

ADMIN="integration-admin-token"   # test-only value
ENV={"LOWLLAMA_ADMIN_TOKEN":ADMIN,"LOWLLAMA_MASTER_IP":"100.64.0.1"}

try:
    from fastapi.testclient import TestClient
    TRANSPORT="testclient"
except (ImportError,RuntimeError):   # starlette raises RuntimeError when httpx2/httpx is missing
    TestClient=None
    TRANSPORT="uvicorn"


class _Http:
    """post/get returning (status_code, parsed_json) through whichever transport is available."""
    def __init__(self):
        self._server=None
        if TestClient is not None:
            self._client=TestClient(app,client=("127.0.0.1",50000))   # loopback, not the synthetic "testclient" host
            return
        import uvicorn
        with socket.socket() as s: s.bind(("127.0.0.1",0)); port=s.getsockname()[1]
        self._base=f"http://127.0.0.1:{port}"
        self._server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=port,log_level="critical"))
        self._thread=threading.Thread(target=self._server.run,daemon=True); self._thread.start()
        deadline=time.time()+10
        while not self._server.started:
            if time.time()>deadline: raise RuntimeError("test uvicorn server did not start")
            time.sleep(0.02)

    def close(self):
        if self._server is not None: self._server.should_exit=True; self._thread.join(5)

    def request(self, method, path, body=None, headers=None):
        headers=dict(headers or {})
        if self._server is None:
            r=self._client.request(method,path,json=body,headers=headers)
            return r.status_code,(r.json() if r.content else None)
        data=None if body is None else json.dumps(body).encode()
        req=urllib.request.Request(self._base+path,data=data,method=method,headers={"Content-Type":"application/json",**headers})
        try:
            with urllib.request.urlopen(req,timeout=10) as r: return r.status,json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            with e: return e.code,json.loads(e.read() or b"null")   # `with` closes the error response

    def post(self, path, body, headers=None): return self.request("POST",path,body,headers)
    def get(self, path, headers=None): return self.request("GET",path,None,headers)

ADMIN_HEADER={"Authorization":f"Bearer {ADMIN}"}   # GET /nodes is admin-only


class BackendIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.http=_Http()

    @classmethod
    def tearDownClass(cls): cls.http.close()

    def setUp(self):
        registry.clear(); invites.clear()
        env=mock.patch.dict(os.environ,ENV); env.start(); self.addCleanup(env.stop)
        self.addCleanup(registry.clear); self.addCleanup(invites.clear)

    # helpers
    def invite(self):
        status,body=self.http.post("/invite",{},headers=ADMIN_HEADER)
        self.assertEqual(status,200,body)
        return body["invite_code"]

    def join(self, code, client_id="install-a", name="Helper 1", ram=16, port=50052):
        return self.http.post("/join",{"invite_code":code,"client_id":client_id,"node_name":name,"ram_gb":ram,"rpc_port":port})

    def nodes(self):
        status,body=self.http.get("/nodes",ADMIN_HEADER)
        self.assertEqual(status,200,body)
        return body

    def test_node_listing_requires_admin(self):
        self.assertEqual(self.join(self.invite())[0],200)
        self.assertIn(self.http.get("/nodes")[0],(401,403))   # no token
        self.assertEqual(self.http.get("/nodes",{"Authorization":"Bearer wrong"})[0],403)
        self.assertEqual(len(self.nodes()),1)

    # tests
    def test_full_enrollment_flow(self):
        code=self.invite()
        status,node=self.join(code)
        self.assertEqual(status,200,node)
        self.assertEqual(set(node),{"node_id","credentials"})
        status,body=self.http.post("/heartbeat",{**node,"free_ram_gb":9.5,"rpc_ready":True})
        self.assertEqual((status,body),(200,{"status":"ok"}))
        listing=self.nodes()
        self.assertEqual(len(listing),1)
        n=listing[0]
        self.assertEqual((n["node_id"],n["node_name"],n["ram_gb"],n["free_ram_gb"],n["rpc_port"],n["address"],n["rpc_ready"],n["status"]),
                         (node["node_id"],"Helper 1",16.0,9.5,50052,"127.0.0.1",True,"online"))
        self.assertIn("last_seen_s",n)
        status,body=self.http.post("/reconnect",node)   # helper restarted
        self.assertEqual((status,body),(200,{"status":"ok"}))
        self.assertEqual([x["node_id"] for x in self.nodes()],[node["node_id"]])   # same ID, no duplicate

    def test_offline_then_online_over_http(self):
        status,node=self.join(self.invite()); self.assertEqual(status,200)
        real_now=registry._now
        with mock.patch.object(registry,"_now",lambda: real_now()+registry.OFFLINE_AFTER+1):
            self.assertEqual(self.nodes()[0]["status"],"offline")
        self.assertEqual(self.http.post("/heartbeat",node)[0],200)
        self.assertEqual(self.nodes()[0]["status"],"online")

    def test_no_secrets_in_public_listing(self):
        code=self.invite()
        status,node=self.join(code,client_id="secret-install-id"); self.assertEqual(status,200)
        text=json.dumps(self.nodes())
        for secret in (node["credentials"],"secret-install-id",code,invites.decode(code)["token"],ADMIN):
            self.assertNotIn(secret,text)
        for hidden in ("credentials","client_id","last_seen","joined_at"): self.assertNotIn(f'"{hidden}"',text)

    def test_admin_token_rules(self):
        self.assertEqual(self.http.post("/invite",{},headers={"Authorization":"Bearer wrong"})[0],403)
        with mock.patch.dict(os.environ,{"LOWLLAMA_ADMIN_TOKEN":""}):
            self.assertEqual(self.http.post("/invite",{},headers={"Authorization":"Bearer any"})[0],403)   # fails closed when not configured
        self.assertEqual(self.http.post("/register",{"node_name":"X","ram_gb":8,"rpc_port":50052})[0],401)   # no token at all
        self.assertEqual(self.http.post("/register",{"node_name":"X","ram_gb":8,"rpc_port":50052},headers={"Authorization":"Bearer wrong"})[0],403)
        status,body=self.http.post("/register",{"node_name":"X","ram_gb":8,"rpc_port":50052},headers=ADMIN_HEADER)
        self.assertEqual(status,200,body)

    def test_invite_requires_master_address(self):
        with mock.patch.dict(os.environ,{"LOWLLAMA_MASTER_IP":""}):
            self.assertEqual(self.http.post("/invite",{},headers=ADMIN_HEADER)[0],503)
        with mock.patch.dict(os.environ,{"LOWLLAMA_MASTER_IP":"not-an-ip"}):
            self.assertEqual(self.http.post("/invite",{},headers=ADMIN_HEADER)[0],503)

    def test_join_rejections(self):
        code=self.invite()
        self.assertEqual(self.join(code)[0],200)
        self.assertEqual(self.join(code)[0],409)   # same client_id again
        self.assertEqual(self.join(code,client_id="install-b")[0],200)   # invites stay reusable until expiry
        self.assertEqual(self.join("garbage!!",client_id="install-c")[0],400)
        payload=invites.decode(code); payload["token"]="forged"
        import base64
        forged=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        self.assertEqual(self.join(forged,client_id="install-d")[0],400)
        invites._invites[invites.decode(code)["token"]]=invites._now()-1   # expire it
        self.assertEqual(self.join(code,client_id="install-e")[0],400)
        self.assertEqual(self.join(self.invite(),client_id="install-f",ram=-1)[0],422)   # Pydantic rejects before the registry
        self.assertEqual(len(self.nodes()),2)

    def test_heartbeat_and_reconnect_rejections(self):
        status,node=self.join(self.invite()); self.assertEqual(status,200)
        self.assertEqual(self.http.post("/heartbeat",{"node_id":node["node_id"],"credentials":"wrong"})[0],401)
        self.assertEqual(self.http.post("/heartbeat",{"node_id":"node-forged","credentials":node["credentials"]})[0],401)
        self.assertEqual(self.http.post("/heartbeat",{**node,"free_ram_gb":99})[0],422)   # more free than total
        self.assertEqual(self.http.post("/heartbeat",{**node,"rpc_ready":"maybe"})[0],422)
        self.assertEqual(self.http.post("/reconnect",{"node_id":node["node_id"],"credentials":"wrong"})[0],401)
        self.assertEqual(self.nodes()[0]["free_ram_gb"],None)   # rejected updates changed nothing

    def test_revoked_node_is_locked_out(self):
        status,node=self.join(self.invite()); self.assertEqual(status,200)
        self.assertTrue(registry.remove(node["node_id"]))   # admin revokes (no HTTP route for this yet)
        self.assertEqual(self.http.post("/heartbeat",node)[0],401)
        self.assertEqual(self.http.post("/reconnect",node)[0],401)

    def test_coordinator_restart_needs_new_invite(self):
        old_code=self.invite()
        status,node=self.join(old_code); self.assertEqual(status,200)
        registry.clear(); invites.clear()   # coordinator restarted: in-memory state is gone
        self.assertEqual(self.http.post("/reconnect",node)[0],401)
        self.assertEqual(self.http.post("/heartbeat",node)[0],401)
        self.assertEqual(self.join(old_code)[0],400)   # old invite died with the restart
        status,new_node=self.join(self.invite())   # deliberate re-enrollment with a NEW invite
        self.assertEqual(status,200)
        self.assertNotEqual(new_node["node_id"],node["node_id"])
        self.assertEqual(len(self.nodes()),1)


def _has_route(path):
    return any(getattr(r,"path",None)==path for r in app.routes)


@unittest.skipUnless(_has_route("/start"),"this checkout's main.py has no POST /start yet")
class StartHandoffTests(unittest.TestCase):
    """POST /start -> workers.get_candidates -> launcher.start(model_id, selection).

    The launcher's start/status/stop are replaced by stand-ins for each test only (no llama-server is started).
    This checks the hand-off shape, NOT that any model runs.
    """
    @classmethod
    def setUpClass(cls): cls.http=_Http()

    @classmethod
    def tearDownClass(cls): cls.http.close()

    def setUp(self):
        import types
        from coordinator import main
        registry.clear(); invites.clear()
        env=mock.patch.dict(os.environ,ENV); env.start(); self.addCleanup(env.stop)
        self.addCleanup(registry.clear)
        self.calls=[]
        def start(model_id, selection):
            self.calls.append((model_id,selection))
            return {"status":"starting","model_id":model_id,"worker_ids":[w.node_id for w in selection.candidates]}
        stand_ins={"start":start,"status":lambda: {"status":"stopped"},"stop":lambda: None}
        import coordinator
        if "coordinator.launcher" not in sys.modules:   # leave the import state exactly as we found it
            def forget_launcher():
                sys.modules.pop("coordinator.launcher",None)
                if hasattr(coordinator,"launcher"): delattr(coordinator,"launcher")
            self.addCleanup(forget_launcher)
        try:
            from coordinator import launcher as real_launcher   # Itmam's real module: replace its functions only,
            for name,fn in stand_ins.items():                   # so this works no matter which test imported it first
                p=mock.patch.object(real_launcher,name,fn); p.start(); self.addCleanup(p.stop)
            # Rodel: sync with real allowlist
            self.model = next(iter(real_launcher.MODEL_ALLOWLIST))
            p_models = mock.patch("coordinator.launcher.MODEL_ALLOWLIST", real_launcher.MODEL_ALLOWLIST)
            p_models.start(); self.addCleanup(p_models.stop)
        except ImportError:   # older checkout without launcher.py: inject a whole stand-in module
            fake=types.ModuleType("coordinator.launcher")
            fake.MODEL_ALLOWLIST = {"llama-3-8b": "path"}
            for name,fn in stand_ins.items(): setattr(fake,name,fn)
            mods=mock.patch.dict(sys.modules,{"coordinator.launcher":fake}); mods.start(); self.addCleanup(mods.stop)
            self.model = "llama-3-8b"

    def ready(self, name, address, free=8.0):
        a=registry.register_node(name,16,50052,address=address,free_ram_gb=free,rpc_ready=True)
        return a["node_id"]

    def start(self, body, headers=ADMIN_HEADER): return self.http.post("/start",body,headers)

    def test_start_requires_admin_and_allowlisted_model(self):
        self.assertIn(self.start({"model_id":self.model},headers={})[0],(401,403))
        self.assertEqual(self.start({"model_id":self.model},headers={"Authorization":"Bearer wrong"})[0],403)
        self.assertEqual(self.start({"model_id":"../../evil.gguf"})[0],400)
        self.assertEqual(self.calls,[])   # launcher never reached

    def test_selected_nodes_reach_the_launcher_with_reasons_for_the_rest(self):
        a=self.ready("Ready16","100.64.0.2",free=9.5)
        b=registry.register_node("NotReady",8,50052,address="100.64.0.3")["node_id"]
        status,body=self.start({"model_id":self.model,"node_ids":[a,b,"node-missing"]})
        self.assertEqual(status,200,body)
        self.assertEqual(body["worker_ids"],[a])
        (model_id,selection),=self.calls
        self.assertEqual(model_id,self.model)
        self.assertEqual(selection.rpc_endpoints(),"100.64.0.2:50052")
        self.assertEqual({r.node_id:r.reason for r in selection.rejected},{b:"rpc_not_ready","node-missing":"unknown_node"})

    def test_selection_order_is_kept_even_against_free_ram(self):
        low=self.ready("Low","100.64.0.2",free=2.0)
        high=self.ready("High","100.64.0.3",free=12.0)
        status,body=self.start({"model_id":self.model,"node_ids":[low,high]})   # low first on purpose
        self.assertEqual(status,200,body)
        self.assertEqual(body["worker_ids"],[low,high])   # becomes RPC0, RPC1 in this order
        self.assertEqual(self.calls[0][1].rpc_endpoints(),"100.64.0.2:50052,100.64.0.3:50052")

    def test_start_requires_a_non_empty_selection(self):
        self.ready("Ready","100.64.0.2")
        for body in ({"model_id":self.model},{"model_id":self.model,"node_ids":[]}):
            self.assertEqual(self.start(body)[0],400)
        self.assertEqual(self.calls,[])

    def test_start_refuses_when_nobody_is_usable(self):
        not_ready=registry.register_node("NotReady",8,50052,address="100.64.0.3")["node_id"]   # real ID, so the not-ready rule is what's tested
        stale=self.ready("Stale","100.64.0.4")
        registry._nodes[stale]["last_seen"]-=registry.OFFLINE_AFTER+1   # offline despite its old rpc_ready claim
        for ids in ([not_ready],[stale],["node-does-not-exist"],[not_ready,stale]):
            status,_=self.start({"model_id":self.model,"node_ids":ids})
            self.assertIn(status,(400,409,503),ids)
        self.assertEqual(self.calls,[])


if __name__=="__main__":
    unittest.main(verbosity=2)
