"""Backend integration tests: real HTTP requests through coordinator/main.py with the REAL registry and invites.

Run from the project root:
    .venv/Scripts/python.exe -m unittest tests.test_backend_integration -v

Transport: FastAPI's TestClient (client address forced to 127.0.0.1) when its httpx2/httpx dependency is
installed; otherwise a real uvicorn server on 127.0.0.1 with urllib. Both send real HTTP requests to the
real app. Each test clears registry/invite state and patches environment variables only for its own duration.
No real secrets are used.
"""
import json, os, shutil, socket, subprocess, sys, threading, time, unittest, urllib.error, urllib.request
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


DEMO_MODEL_ID="qwen2.5-0.5b-instruct-q4_k_m"   # the shared Phase 2 demo model (launcher.MODEL_ALLOWLIST is the one catalog)


def _launcher_models():
    import importlib
    return dict(importlib.import_module("coordinator.launcher").MODEL_ALLOWLIST)


_LAUNCHER_WAS_LOADED=None


def setUpModule():
    global _LAUNCHER_WAS_LOADED
    _LAUNCHER_WAS_LOADED="coordinator.launcher" in sys.modules


def tearDownModule():   # leave the import state as we found it, so other files' sys.modules mocks still work
    import coordinator
    if _LAUNCHER_WAS_LOADED is False:
        launcher=sys.modules.pop("coordinator.launcher",None)
        if launcher is not None: launcher.stop()   # nothing should be running, but never leave a child behind
        if hasattr(coordinator,"launcher"): delattr(coordinator,"launcher")


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
        except ImportError:   # older checkout without launcher.py: inject a whole stand-in module
            fake=types.ModuleType("coordinator.launcher")
            for name,fn in stand_ins.items(): setattr(fake,name,fn)
            mods=mock.patch.dict(sys.modules,{"coordinator.launcher":fake}); mods.start(); self.addCleanup(mods.stop)
        api_models=getattr(main,"ALLOWED_MODELS",None) or _launcher_models()   # survives Rodel switching to the launcher's catalog
        self.model=DEMO_MODEL_ID if DEMO_MODEL_ID in api_models else next(iter(api_models))   # a model /start accepts today

    def ready(self, name, address, free=8.0):
        a=registry.register_node(name,16,50052,address=address,free_ram_gb=free,rpc_ready=True)
        return a["node_id"]

    def start(self, body, headers=ADMIN_HEADER): return self.http.post("/start",body,headers)

    def test_start_requires_admin_and_allowlisted_model(self):
        self.assertIn(self.start({"model_id":self.model},headers={})[0],(401,403))
        self.assertEqual(self.start({"model_id":self.model},headers={"Authorization":"Bearer wrong"})[0],403)
        self.assertEqual(self.start({"model_id":"../../evil.gguf"})[0],400)
        self.assertEqual(self.calls,[])   # launcher never reached

    def test_selected_nodes_reach_the_launcher_and_others_are_marked_not_selected(self):
        a=self.ready("Ready16","100.64.0.2",free=9.5)
        b=registry.register_node("NotReady",8,50052,address="100.64.0.3")["node_id"]
        status,body=self.start({"model_id":self.model,"node_ids":[a]})
        self.assertEqual(status,200,body)
        self.assertEqual(body["worker_ids"],[a])
        (model_id,selection),=self.calls
        self.assertEqual(model_id,self.model)
        self.assertEqual(selection.rpc_endpoints(),"100.64.0.2:50052")
        self.assertEqual({r.node_id:r.reason for r in selection.rejected},{b:"not_selected"})
        self.assertEqual(selection.selected_rejections(),())

    def test_partial_selection_is_refused_with_the_rejected_workers_named(self):
        a=self.ready("Ready16","100.64.0.2",free=9.5)
        b=registry.register_node("NotReady",8,50052,address="100.64.0.3")["node_id"]
        status,body=self.start({"model_id":self.model,"node_ids":[a,b,"node-missing"]})
        self.assertIn(status,(400,409),body)   # asked for 3 workers, only 1 usable: refuse, don't start with fewer
        self.assertEqual(self.calls,[])
        text=json.dumps(body)
        for node_id in (b,"node-missing"): self.assertIn(node_id,text)   # say WHICH selected workers were rejected

    def test_every_launcher_model_including_the_demo_is_accepted_by_start(self):   # fixed by Rodel in e127f2d
        a=self.ready("Ready","100.64.0.2")
        self.assertIn(DEMO_MODEL_ID,_launcher_models())
        for model_id in _launcher_models():
            with self.subTest(model_id=model_id):
                status,body=self.start({"model_id":model_id,"node_ids":[a]})
                self.assertEqual(status,200,body)
        self.assertEqual([m for m,_ in self.calls],list(_launcher_models()))

    def test_start_refuses_models_the_launcher_does_not_know(self):   # fixed by Rodel in e127f2d
        from coordinator import main
        a=self.ready("Ready","100.64.0.2")
        api_only=[m for m in getattr(main,"ALLOWED_MODELS",{}) if m not in _launcher_models()]
        for model_id in api_only+["qwen2.5-7b","../../evil.gguf"]:   # the old API-only ID must stay refused
            with self.subTest(model_id=model_id):
                self.assertEqual(self.start({"model_id":model_id,"node_ids":[a]})[0],400)
        self.assertEqual(self.calls,[])

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

    def test_refusal_names_each_rejected_worker_and_its_reason(self):
        not_ready=registry.register_node("NotReady",8,50052,address="100.64.0.3")["node_id"]
        status,body=self.start({"model_id":self.model,"node_ids":[not_ready,"node-missing"]})
        self.assertEqual(status,400,body)
        text=json.dumps(body)
        self.assertNotIn("attribute",text)   # no Python error text leaking into the API
        for expected in (not_ready,"rpc_not_ready","node-missing","unknown_node"): self.assertIn(expected,text)

    def test_eligible_offline_and_unknown_together_refuse_everything(self):
        good=self.ready("Good","100.64.0.2")
        gone=self.ready("Gone","100.64.0.4")
        registry._nodes[gone]["last_seen"]-=registry.OFFLINE_AFTER+1
        status,body=self.start({"model_id":self.model,"node_ids":[good,gone,"node-missing"]})
        self.assertEqual(status,400,body)
        self.assertEqual(self.calls,[])   # all-or-nothing: the eligible one is NOT started alone
        text=json.dumps(body)
        for expected in (gone,"offline","node-missing","unknown_node"): self.assertIn(expected,text)
        for leak in ("Traceback","Error selecting","attribute"): self.assertNotIn(leak,text)

    def test_same_node_id_twice_counts_once(self):
        a=self.ready("A","100.64.0.2")
        status,body=self.start({"model_id":self.model,"node_ids":[a,a]})
        self.assertEqual(status,200,body)
        self.assertEqual(body["worker_ids"],[a])   # never one helper counted as RPC0 and RPC1
        self.assertEqual(self.calls[0][1].rpc_endpoints(),"100.64.0.2:50052")

    def test_unknown_free_ram_is_allowed_not_assumed(self):
        a=self.ready("NoRamReport","100.64.0.2",free=None)
        status,body=self.start({"model_id":self.model,"node_ids":[a]})
        self.assertEqual(status,200,body)
        self.assertIsNone(self.calls[0][1].candidates[0].free_ram_gb)   # passed on as unknown, not invented

    def test_two_nodes_on_one_endpoint_are_refused(self):
        a=self.ready("First","100.64.0.2")
        b=self.ready("StaleCopy","100.64.0.2")   # same address:port, e.g. a re-enrolled laptop
        status,body=self.start({"model_id":self.model,"node_ids":[a,b]})
        self.assertEqual(status,400,body)
        self.assertEqual(self.calls,[])
        self.assertIn("duplicate_endpoint",json.dumps(body))


@unittest.skipUnless(_has_route("/start"),"this checkout's main.py has no POST /start yet")
class StartWithRealLauncherTests(unittest.TestCase):
    """POST /start through the REAL workers AND the REAL launcher. Only the outside world is faked: the binary and
    model paths, the TCP connection to the helper, and Popen. No llama-server runs; this is NOT a live RPC test.
    """
    @classmethod
    def setUpClass(cls): cls.http=_Http()

    @classmethod
    def tearDownClass(cls): cls.http.close()

    def setUp(self):
        import importlib, tempfile
        from pathlib import Path
        from coordinator import main
        self.launcher=importlib.import_module("coordinator.launcher")
        registry.clear(); invites.clear()
        env=mock.patch.dict(os.environ,ENV); env.start(); self.addCleanup(env.stop)
        self.addCleanup(registry.clear)
        self.launcher.stop(); self.addCleanup(self.launcher.stop)
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        binary=Path(tmp.name)/"llama-server.exe"; model=Path(tmp.name)/"model.gguf"
        binary.write_bytes(b"x"); model.write_bytes(b"x")
        self.log_dir=Path(tmp.name)/"logs"   # launch logs go here, never into the real .runtime/logs evidence folder
        patches=[mock.patch.object(self.launcher,"_resolve_runtime",lambda model_id: (binary,model)),   # fake files, real everything else
                 mock.patch.object(self.launcher,"_ensure_server_port_available")]
        if hasattr(self.launcher,"LAUNCH_LOG_DIRECTORY"): patches.append(mock.patch.object(self.launcher,"LAUNCH_LOG_DIRECTORY",self.log_dir))
        for p in patches: p.start(); self.addCleanup(p.stop)
        api_models=getattr(main,"ALLOWED_MODELS",None) or _launcher_models()
        self.model=DEMO_MODEL_ID if DEMO_MODEL_ID in api_models else next(iter(api_models))

    def fake_engine(self):
        """Mocks for the outside world only: TCP to helpers, the --list-devices probe, the llama-server child, /health."""
        import contextlib, io, subprocess
        child=mock.MagicMock(); child.poll.return_value=None; child.stdout=io.StringIO("")
        def stop_child(): child.poll.return_value=-15
        child.terminate.side_effect=stop_child
        def probe(command, **kwargs):   # each endpoint is probed alone, so it always lists as RPC0
            return subprocess.CompletedProcess(args=command,returncode=0,stdout=f"Available devices:\n  RPC0: {command[2]} (16000 MiB, 8000 MiB free)",stderr="")
        stack=contextlib.ExitStack()
        stack.enter_context(mock.patch.object(self.launcher.socket,"create_connection"))
        run=stack.enter_context(mock.patch.object(self.launcher.subprocess,"run",side_effect=probe))
        popen=stack.enter_context(mock.patch.object(self.launcher.subprocess,"Popen",return_value=child))
        stack.enter_context(mock.patch.object(self.launcher,"_health_is_ready",return_value=True))
        return stack,child,run,popen

    def wait_for(self, wanted, seconds=2):
        deadline=time.time()+seconds
        while self.http.get("/status",ADMIN_HEADER)[1]["status"]!=wanted and time.time()<deadline: time.sleep(0.01)
        return self.http.get("/status",ADMIN_HEADER)[1]

    def laptop(self, address):
        """An HTTP client that the coordinator sees as coming from this Tailscale address (one per helper laptop)."""
        client=TestClient(app,client=(address,50000)); self.addCleanup(client.close)
        return client

    def enroll(self, laptop, code, name, client_id):
        r=laptop.post("/join",json={"invite_code":code,"client_id":client_id,"node_name":name,"ram_gb":16,"rpc_port":50052})
        self.assertEqual(r.status_code,200,r.text)
        node=r.json()
        r=laptop.post("/heartbeat",json={**node,"free_ram_gb":6.0,"rpc_ready":True})
        self.assertEqual(r.status_code,200,r.text)
        return node

    @unittest.skipIf(TestClient is None,"needs TestClient to give each laptop its own client address")
    def test_two_laptops_enroll_over_http_and_start_as_rpc0_rpc1(self):
        code=self.http.post("/invite",{},ADMIN_HEADER)[1]["invite_code"]
        rodel=self.enroll(self.laptop("100.64.0.2"),code,"Rodel-Laptop","install-rodel")
        shafin=self.enroll(self.laptop("100.64.0.3"),code,"Shafin-Laptop","install-shafin")
        listing=self.http.get("/nodes",ADMIN_HEADER)[1]
        self.assertEqual(sorted((n["node_name"],n["address"],n["status"],n["rpc_ready"]) for n in listing),
                         [("Rodel-Laptop","100.64.0.2","online",True),("Shafin-Laptop","100.64.0.3","online",True)])
        self.assertNotEqual(rodel["node_id"],shafin["node_id"])
        real_log_dir=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),".runtime","logs")
        real_logs=set(os.listdir(real_log_dir)) if os.path.isdir(real_log_dir) else set()
        stack,child,run,popen=self.fake_engine()
        with stack:
            status,body=self.http.post("/start",{"model_id":self.model,"node_ids":[rodel["node_id"],shafin["node_id"]]},ADMIN_HEADER)
            self.assertEqual(status,200,body)
            state=self.wait_for("ready")
        self.assertEqual((state["status"],state["worker_ids"]),("ready",[rodel["node_id"],shafin["node_id"]]))
        command=popen.call_args.args[0]
        self.assertEqual(command[command.index("--rpc")+1],"100.64.0.2:50052,100.64.0.3:50052")   # Rodel = RPC0, Shafin = RPC1
        self.assertEqual(command[command.index("--device")+1],"CUDA0,RPC0,RPC1")
        self.assertEqual(self.http.post("/stop",{},ADMIN_HEADER)[0],200)
        self.assertEqual(self.http.get("/status",ADMIN_HEADER)[1]["status"],"stopped")
        if hasattr(self.launcher,"LAUNCH_LOG_DIRECTORY"):
            self.assertEqual(len([f for f in os.listdir(self.log_dir) if f.startswith("api-launcher-")]),1)   # one unique log per start
            if os.path.isdir(real_log_dir): self.assertEqual(set(os.listdir(real_log_dir))-real_logs,set())   # nothing leaked into real evidence logs

    @unittest.skipIf(TestClient is None,"needs TestClient to give each laptop its own client address")
    def test_one_of_two_laptops_going_silent_refuses_the_whole_start(self):
        code=self.http.post("/invite",{},ADMIN_HEADER)[1]["invite_code"]
        rodel_laptop=self.laptop("100.64.0.2")
        rodel=self.enroll(rodel_laptop,code,"Rodel-Laptop","install-rodel")
        shafin=self.enroll(self.laptop("100.64.0.3"),code,"Shafin-Laptop","install-shafin")
        real_now=registry._now
        stack,child,run,popen=self.fake_engine()
        with stack, mock.patch.object(registry,"_now",lambda: real_now()+registry.OFFLINE_AFTER+1):
            self.assertEqual(rodel_laptop.post("/heartbeat",json=rodel).status_code,200)   # only Rodel keeps beating
            status,body=self.http.post("/start",{"model_id":self.model,"node_ids":[rodel["node_id"],shafin["node_id"]]},ADMIN_HEADER)
        self.assertEqual(status,400,body)
        self.assertIn(shafin["node_id"],json.dumps(body)); self.assertIn("offline",json.dumps(body))
        run.assert_not_called(); popen.assert_not_called()   # never probed, never started with Rodel alone

    def test_second_start_while_running_is_busy_and_leaves_the_first_alone(self):
        a=registry.register_node("A",16,50052,address="100.64.0.2",free_ram_gb=8.0,rpc_ready=True)["node_id"]
        stack,child,run,popen=self.fake_engine()
        with stack:
            self.assertEqual(self.http.post("/start",{"model_id":self.model,"node_ids":[a]},ADMIN_HEADER)[0],200)
            self.assertEqual(self.wait_for("ready")["status"],"ready")
            status,body=self.http.post("/start",{"model_id":self.model,"node_ids":[a]},ADMIN_HEADER)
            self.assertEqual(status,409,body)
            self.assertEqual(popen.call_count,1)
            self.assertEqual(self.http.get("/status",ADMIN_HEADER)[1]["status"],"ready")   # the running engine is untouched
        child.terminate.assert_not_called()
        self.assertEqual(self.http.post("/stop",{},ADMIN_HEADER)[0],200)
        child.terminate.assert_called_once()

    def test_unexpected_launcher_error_returns_500_and_nothing_runs(self):
        a=registry.register_node("A",16,50052,address="100.64.0.2",free_ram_gb=8.0,rpc_ready=True)["node_id"]
        stack,child,run,popen=self.fake_engine()
        with stack, mock.patch.object(self.launcher,"start",side_effect=RuntimeError("boom")):
            status,body=self.http.post("/start",{"model_id":self.model,"node_ids":[a]},ADMIN_HEADER)
        self.assertEqual(status,500,body)
        self.assertNotIn("Traceback",json.dumps(body))
        popen.assert_not_called()
        self.assertNotIn(self.http.get("/status",ADMIN_HEADER)[1]["status"],("starting","ready"))
        self.assertEqual(self.http.post("/stop",{},ADMIN_HEADER)[0],200)   # safe even though nothing started

    def test_stop_is_idempotent_and_chat_needs_a_ready_engine(self):
        for _ in range(2):
            self.assertEqual(self.http.post("/stop",{},ADMIN_HEADER),(200,{"status":"stopped"}))
        msgs={"messages":[{"role":"user","content":"hi"}]}
        self.assertEqual(self.http.post("/chat",msgs,ADMIN_HEADER)[0],503)   # stopped: refused before contacting llama-server
        self.assertEqual(self.http.post("/chat",{**msgs,"stream":True},ADMIN_HEADER)[0],400)
        self.assertIn(self.http.post("/chat",msgs)[0],(401,403))

    def test_worker_lost_between_selection_and_probe_fails_without_starting(self):
        nid=registry.register_node("Helper",16,50052,address="100.64.0.2",free_ram_gb=8.0,rpc_ready=True)["node_id"]
        with (mock.patch.object(self.launcher.socket,"create_connection",side_effect=ConnectionRefusedError()) as connect,   # helper just died
              mock.patch.object(self.launcher.subprocess,"Popen") as popen):
            status,body=self.http.post("/start",{"model_id":self.model,"node_ids":[nid]},ADMIN_HEADER)
        self.assertNotEqual(status,200,body)   # registry still said online + rpc_ready; the launcher's own probe caught it
        connect.assert_called_once()
        popen.assert_not_called()
        status,state=self.http.get("/status",ADMIN_HEADER)
        self.assertEqual((status,state["status"]),(200,"error"))
        self.assertIn("unreachable",state["error"])
        self.assertEqual(self.http.post("/stop",{},ADMIN_HEADER)[0],200)
        self.assertEqual(self.http.get("/status",ADMIN_HEADER)[1]["status"],"stopped")

    def test_tcp_only_listener_is_not_treated_as_a_worker(self):
        nid=registry.register_node("Helper",16,50052,address="100.64.0.2",free_ram_gb=8.0,rpc_ready=True)["node_id"]
        import subprocess
        not_llama=subprocess.CompletedProcess(args=[],returncode=0,stdout="Available devices:\n  CUDA0: fake GPU",stderr="")
        with (mock.patch.object(self.launcher.socket,"create_connection"),   # port 50052 accepts TCP...
              mock.patch.object(self.launcher.subprocess,"run",return_value=not_llama),   # ...but it is not llama.cpp RPC
              mock.patch.object(self.launcher.subprocess,"Popen") as popen):
            status,body=self.http.post("/start",{"model_id":self.model,"node_ids":[nid]},ADMIN_HEADER)
        self.assertNotEqual(status,200,body)
        popen.assert_not_called()
        self.assertIn("not llama.cpp compatible",self.http.get("/status",ADMIN_HEADER)[1]["error"])

    def test_two_distinct_helpers_become_rpc0_rpc1_in_selected_order(self):
        import io, subprocess
        a=registry.register_node("HelperA",16,50052,address="100.64.0.2",free_ram_gb=8.0,rpc_ready=True)["node_id"]
        b=registry.register_node("HelperB",16,50052,address="100.64.0.3",free_ram_gb=2.0,rpc_ready=True)["node_id"]
        child=mock.MagicMock(); child.poll.return_value=None; child.stdout=io.StringIO("")
        def stop_child(): child.poll.return_value=-15
        child.terminate.side_effect=stop_child
        def probe(command, **kwargs):   # each endpoint is probed alone, so it always lists as RPC0
            return subprocess.CompletedProcess(args=command,returncode=0,stdout=f"Available devices:\n  RPC0: {command[2]} (16000 MiB, 8000 MiB free)",stderr="")
        with (mock.patch.object(self.launcher.socket,"create_connection"),
              mock.patch.object(self.launcher.subprocess,"run",side_effect=probe) as run,
              mock.patch.object(self.launcher.subprocess,"Popen",return_value=child) as popen,
              mock.patch.object(self.launcher,"_health_is_ready",return_value=True)):
            status,body=self.http.post("/start",{"model_id":self.model,"node_ids":[b,a]},ADMIN_HEADER)   # B first on purpose
            self.assertEqual(status,200,body)
            deadline=time.time()+2
            while self.http.get("/status",ADMIN_HEADER)[1]["status"]!="ready" and time.time()<deadline: time.sleep(0.01)
            state=self.http.get("/status",ADMIN_HEADER)[1]
        self.assertEqual((state["status"],state["worker_ids"],state["model_id"]),("ready",[b,a],self.model))
        self.assertEqual([c.args[0][2] for c in run.call_args_list],["100.64.0.3:50052","100.64.0.2:50052"])   # both probed
        command=popen.call_args.args[0]
        self.assertEqual(command[command.index("--rpc")+1],"100.64.0.3:50052,100.64.0.2:50052")   # B = RPC0, A = RPC1
        self.assertEqual(command[command.index("--device")+1],"CUDA0,RPC0,RPC1")
        self.assertEqual(command[command.index("--alias")+1],self.model)
        self.assertEqual(self.http.post("/stop",{},ADMIN_HEADER)[0],200)
        self.assertEqual(self.http.get("/status",ADMIN_HEADER)[1]["status"],"stopped")
        child.terminate.assert_called_once()   # only its own child was stopped


_POWERSHELL=shutil.which("powershell.exe") or shutil.which("pwsh")


@unittest.skipUnless(_has_route("/start") and _POWERSHELL and os.name=="nt","needs POST /start and Windows PowerShell")
class ApiFlowScriptTests(unittest.TestCase):
    """scripts/Test-Api-Flow.ps1 against the REAL coordinator app on a local uvicorn port.

    Stand-ins: the launcher's start/status/stop (no llama-server runs) and the coordinator's HTTP call to
    llama-server on 127.0.0.1:8080. Checks the script's cleanup and error paths, NOT inference.
    """
    @classmethod
    def setUpClass(cls):
        import uvicorn
        with socket.socket() as s: s.bind(("127.0.0.1",0)); cls.port=s.getsockname()[1]
        cls.server=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=cls.port,log_level="critical"))
        cls.thread=threading.Thread(target=cls.server.run,daemon=True); cls.thread.start()
        deadline=time.time()+10
        while not cls.server.started:
            if time.time()>deadline: raise RuntimeError("test uvicorn server did not start")
            time.sleep(0.02)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit=True; cls.thread.join(5)

    def setUp(self):
        import importlib, httpx
        registry.clear(); invites.clear(); self.addCleanup(registry.clear)
        env=mock.patch.dict(os.environ,ENV); env.start(); self.addCleanup(env.stop)
        launcher=importlib.import_module("coordinator.launcher")
        self.calls={"start":[],"stop":0,"chat":0}
        self.states=["starting","ready"]   # what /status reports, one entry per poll; the last one repeats
        self.start_error=None
        self.chat_status=200
        def start(model_id, selection):
            if self.start_error: raise self.start_error
            self.calls["start"].append((model_id,[w.node_id for w in selection.candidates]))
            self.running=True
            return {"status":"starting","model_id":model_id,"worker_ids":[w.node_id for w in selection.candidates]}
        def status():
            if not getattr(self,"running",False): return {"status":"stopped"}
            state=self.states.pop(0) if len(self.states)>1 else self.states[0]
            return {"status":state,"model_id":DEMO_MODEL_ID,"worker_ids":[],"error":"engine crashed" if state=="error" else None}
        def stop():
            self.calls["stop"]+=1; self.running=False
        for name,fn in {"start":start,"status":status,"stop":stop}.items():
            p=mock.patch.object(launcher,name,fn); p.start(); self.addCleanup(p.stop)
        async def fake_llama_post(client, url, json=None, timeout=None, **kw):
            self.calls["chat"]+=1
            body={"choices":[{"index":0,"message":{"role":"assistant","content":"Paris."}}]} if self.chat_status==200 else {"error":"x"}
            return httpx.Response(self.chat_status,json=body)
        p=mock.patch.object(httpx.AsyncClient,"post",fake_llama_post); p.start(); self.addCleanup(p.stop)

    def helper(self, name, address):
        return registry.register_node(name,16,50052,address=address,free_ram_gb=6.0,rpc_ready=True)["node_id"]

    def run_flow(self, *args, token=ADMIN):
        script=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),"scripts","Test-Api-Flow.ps1")
        env={**os.environ,"LOWLLAMA_ADMIN_TOKEN":token}
        p=subprocess.run([_POWERSHELL,"-NoProfile","-ExecutionPolicy","Bypass","-File",script,"-CoordinatorUrl",f"http://127.0.0.1:{self.port}",
                          "-ReadyTimeoutSeconds","20",*args],capture_output=True,text=True,timeout=120,env=env)
        out=p.stdout+p.stderr
        self.assertNotIn(ADMIN,out)   # the token is never printed
        return p.returncode,out

    def test_happy_path_two_helpers_in_order_then_stopped(self):
        rodel=self.helper("Rodel","100.64.0.2"); shafin=self.helper("Shafin","100.64.0.3")
        code,out=self.run_flow("-NodeIds",f"{rodel},{shafin}","-ExpectHelpers","2")
        self.assertEqual(code,0,out)
        self.assertIn("API FLOW PASS",out); self.assertIn("CHAT SENT AT",out); self.assertIn("Paris.",out)
        self.assertEqual(self.calls["start"],[(DEMO_MODEL_ID,[rodel,shafin])])   # RPC0=Rodel, RPC1=Shafin
        self.assertEqual(self.calls["stop"],1)

    def test_engine_error_after_start_still_stops_it(self):   # the old script skipped /stop here and leaked the engine
        a=self.helper("A","100.64.0.2")
        self.states=["starting","error"]
        code,out=self.run_flow("-NodeIds",a)
        self.assertEqual(code,1,out); self.assertIn("engine error",out)
        self.assertEqual((self.calls["stop"],self.calls["chat"]),(1,0))

    def test_failed_chat_still_stops_and_fails(self):
        a=self.helper("A","100.64.0.2")
        self.chat_status=500
        code,out=self.run_flow("-NodeIds",a)
        self.assertEqual(code,1,out); self.assertIn("/chat returned",out)
        self.assertEqual(self.calls["stop"],1)

    def test_busy_engine_is_left_alone(self):
        from coordinator import launcher
        a=self.helper("A","100.64.0.2")
        self.start_error=launcher.LauncherBusyError("already running")
        code,out=self.run_flow("-NodeIds",a)
        self.assertEqual(code,1,out); self.assertIn("409",out)
        self.assertEqual(self.calls["stop"],0)   # never stop an engine this script did not start

    def test_stale_or_unready_ids_never_reach_start(self):
        a=self.helper("A","100.64.0.2")
        b=registry.register_node("NotReady",16,50052,address="100.64.0.3")["node_id"]
        for ids,reason in (([a,"node-from-yesterday"],"not in the current /nodes"),([a,b],"not rpc_ready"),([a,a],"selected twice")):
            with self.subTest(reason=reason):
                code,out=self.run_flow("-NodeIds",",".join(ids))
                self.assertEqual(code,1,out); self.assertIn(reason,out)
        self.assertEqual((self.calls["start"],self.calls["stop"]),([],0))

    def test_single_auto_selected_node_is_sent_as_a_list(self):
        a=self.helper("Only","100.64.0.2")
        code,out=self.run_flow()
        self.assertEqual(code,0,out)
        self.assertEqual(self.calls["start"],[(DEMO_MODEL_ID,[a])])   # a lone ID must still be a JSON list for /start

    def test_expect_helpers_refuses_a_one_helper_run(self):
        a=self.helper("A","100.64.0.2")
        code,out=self.run_flow("-NodeIds",a,"-ExpectHelpers","2")
        self.assertEqual(code,1,out); self.assertIn("expected 2",out)
        self.assertEqual(self.calls["start"],[])

    def test_missing_token_is_not_run(self):
        code,out=self.run_flow(token="")
        self.assertEqual(code,1,out); self.assertIn("NOT RUN",out)



if __name__=="__main__":
    unittest.main(verbosity=2)
