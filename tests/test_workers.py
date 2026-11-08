"""Tests for coordinator/workers.py (read-only worker candidate selection). Run from the project root:
    .venv/Scripts/python.exe -m unittest tests.test_workers -v
"""
import copy, json, os, sys, unittest
from unittest import mock
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # so `coordinator` imports from the project root
from coordinator import registry, workers


def snapshot(node_id, **over):   # a fake public snapshot, for shapes the real registry would never store
    base={"node_id":node_id,"node_name":node_id,"ram_gb":16.0,"free_ram_gb":8.0,"rpc_port":50052,
          "address":"100.64.0.10","rpc_ready":True,"status":"online","last_seen_s":1.0}
    base.update(over); return base


class WorkerSelectionTests(unittest.TestCase):
    def setUp(self):
        registry.clear(); self.addCleanup(registry.clear)
        self.clock=[1_000_000.0]
        p=mock.patch.object(registry,"_now",lambda: self.clock[0]); p.start(); self.addCleanup(p.stop)

    def add(self, name, address="100.64.0.2", ready=True, free=8.0, ram=16, port=50052):
        a=registry.register_node(name,ram,port,address=address,free_ram_gb=free,rpc_ready=ready)
        return a["node_id"]

    def ids(self, selection): return [w.node_id for w in selection.candidates]
    def reasons(self, selection): return {r.node_id:r.reason for r in selection.rejected}

    def test_ready_online_tailscale_node_is_a_candidate(self):
        nid=self.add("Helper",address="100.101.102.103",free=9.5)
        sel=workers.get_candidate_workers()
        self.assertEqual(self.ids(sel),[nid])
        w=sel.candidates[0]
        self.assertEqual((w.address,w.rpc_port,w.endpoint,w.free_ram_gb,w.status),("100.101.102.103",50052,"100.101.102.103:50052",9.5,"online"))
        self.assertEqual(sel.rpc_endpoints(),"100.101.102.103:50052")
        self.assertEqual(sel.rejected,())

    def test_offline_node_excluded_even_if_it_claimed_rpc_ready(self):
        nid=self.add("Stale",free=15.0)   # big free RAM claim, then silence
        self.clock[0]+=registry.OFFLINE_AFTER+1
        sel=workers.get_candidate_workers()
        self.assertEqual((self.ids(sel),self.reasons(sel)),([],{nid:workers.OFFLINE}))

    def test_rpc_not_ready_excluded(self):
        nid=self.add("NotReady",ready=False)
        self.assertEqual(self.reasons(workers.get_candidate_workers()),{nid:workers.RPC_NOT_READY})

    def test_missing_and_loopback_addresses(self):
        none=self.add("NoAddr",address=None)
        loop=self.add("Loop",address="127.0.0.1")
        sel=workers.get_candidate_workers()
        self.assertEqual(self.reasons(sel),{none:workers.NO_ADDRESS,loop:workers.NOT_TAILSCALE})
        self.assertEqual(self.ids(workers.get_candidate_workers(allow_loopback=True)),[loop])   # same-laptop testing only

    def test_malformed_snapshot_values_are_rejected_defensively(self):
        fake=[snapshot("a",address="not-an-ip"),snapshot("b",address="8.8.8.8"),snapshot("c",rpc_port=0),
              snapshot("d",rpc_port=70000),snapshot("e",rpc_port="50052"),snapshot("f",rpc_port=True),
              snapshot("g",rpc_ready="true"),snapshot("h",address=42)]
        with mock.patch.object(registry,"get_all_nodes",return_value=fake):
            sel=workers.get_candidate_workers()
        self.assertEqual(self.ids(sel),[])
        self.assertEqual(self.reasons(sel),{"a":workers.INVALID_ADDRESS,"b":workers.NOT_TAILSCALE,"c":workers.INVALID_PORT,
                                            "d":workers.INVALID_PORT,"e":workers.INVALID_PORT,"f":workers.INVALID_PORT,
                                            "g":workers.RPC_NOT_READY,"h":workers.INVALID_ADDRESS})

    def test_duplicate_endpoints_keep_only_the_first(self):
        a=self.add("A",address="100.64.0.2",free=8.0)
        b=self.add("B",address="100.64.0.2",free=4.0)   # same address:port, e.g. a stale second enrollment
        sel=workers.get_candidate_workers()
        self.assertEqual((self.ids(sel),self.reasons(sel)),([a],{b:workers.DUPLICATE_ENDPOINT}))

    def test_same_address_different_port_is_fine(self):
        a=self.add("A",address="100.64.0.2",port=50052)
        b=self.add("B",address="100.64.0.2",port=50053)
        self.assertEqual(sorted(self.ids(workers.get_candidate_workers())),sorted([a,b]))

    def test_deterministic_ordering_by_reported_free_ram(self):
        low=self.add("Low",address="100.64.0.3",free=2.0)
        unknown=self.add("Unknown",address="100.64.0.4",free=None)
        high=self.add("High",address="100.64.0.5",free=12.0)
        mid=self.add("Mid",address="100.64.0.6",free=6.0)
        first=self.ids(workers.get_candidate_workers())
        self.assertEqual(first,[high,mid,low,unknown])
        for _ in range(5): self.assertEqual(self.ids(workers.get_candidate_workers()),first)

    def test_selected_ids_keep_selection_order_and_report_problems(self):
        a=self.add("A",address="100.64.0.2",free=2.0)
        b=self.add("B",address="100.64.0.3",free=12.0)
        c=self.add("C",address="100.64.0.4",ready=False)
        d=self.add("D",address="100.64.0.5")
        sel=workers.get_candidate_workers(selected_node_ids=[a,"node-missing",b,c,a])
        self.assertEqual(self.ids(sel),[a,b])   # your order, not free-RAM order; repeats dropped
        self.assertEqual(self.reasons(sel),{"node-missing":workers.UNKNOWN_NODE,c:workers.RPC_NOT_READY,d:workers.NOT_SELECTED})
        missing=[r for r in sel.rejected if r.node_id=="node-missing"][0]
        self.assertIsNone(missing.node_name)

    def test_selecting_nothing_gives_nothing(self):
        a=self.add("A")
        sel=workers.get_candidate_workers(selected_node_ids=[])
        self.assertEqual((self.ids(sel),self.reasons(sel)),([],{a:workers.NOT_SELECTED}))
        self.assertEqual(sel.rpc_endpoints(),"")

    def test_min_free_ram_and_unknown_free_ram(self):
        big=self.add("Big",address="100.64.0.2",free=10.0)
        small=self.add("Small",address="100.64.0.3",free=3.0)
        unknown=self.add("Unknown",address="100.64.0.4",free=None)
        sel=workers.get_candidate_workers(min_free_ram_gb=4)
        self.assertEqual(self.ids(sel),[big])
        self.assertEqual(self.reasons(sel),{small:workers.NOT_ENOUGH_FREE_RAM,unknown:workers.FREE_RAM_UNKNOWN})
        self.assertIn(unknown,self.ids(workers.get_candidate_workers()))   # without a minimum, unknown is allowed but last

    def test_bad_arguments(self):
        for bad in ("node-1",5,[None],[""],[3]):
            with self.assertRaises(ValueError): workers.get_candidate_workers(selected_node_ids=bad)
        for bad in (-1,float("nan"),float("inf"),"4",True):
            with self.assertRaises(ValueError): workers.get_candidate_workers(min_free_ram_gb=bad)

    def test_no_secrets_and_read_only(self):
        a=registry.register_node("Helper",16,50052,address="100.64.0.2",rpc_ready=True,client_id="install-secret")
        before=copy.deepcopy(registry._nodes)
        sel=workers.get_candidate_workers(); summary=workers.resource_summary()
        self.assertEqual(registry._nodes,before)   # nothing in the registry changed
        text=json.dumps(sel.as_dict())+json.dumps(summary)
        for secret in (a["credentials"],"install-secret"): self.assertNotIn(secret,text)
        self.assertEqual(set(sel.candidates[0].as_dict()),
                         {"node_id","node_name","address","rpc_port","endpoint","ram_gb","free_ram_gb","rpc_ready","status","last_seen_s"})
        with self.assertRaises(Exception): sel.candidates[0].address="1.2.3.4"   # frozen: the launcher can't edit it

    def test_get_candidates_is_the_name_main_py_calls(self):
        a=self.add("A",address="100.64.0.2",free=2.0)
        b=self.add("B",address="100.64.0.3",free=12.0)
        self.assertEqual(self.ids(workers.get_candidates()),[b,a])   # POST /start with no node_ids
        self.assertEqual(self.ids(workers.get_candidates([a])),[a])   # POST /start with node_ids (positional, like main.py)
        self.assertEqual(workers.get_candidates(None),workers.get_candidate_workers())

    def test_ipv6_tailscale_endpoint_uses_brackets(self):
        self.add("V6",address="fd7a:115c:a1e0::5")
        self.assertEqual(workers.get_candidate_workers().rpc_endpoints(),"[fd7a:115c:a1e0::5]:50052")

    def test_resource_summary_counts_reported_values_only(self):
        self.add("Ready16",address="100.64.0.2",free=9.5)
        self.add("Ready8",address="100.64.0.3",free=None,ram=8)
        self.add("NotReady",address="100.64.0.4",ready=False,free=5.0)
        stale=self.add("Stale",address="100.64.0.5",free=7.0)
        registry._nodes[stale]["last_seen"]-=registry.OFFLINE_AFTER+1
        s=workers.resource_summary()
        self.assertEqual({k:v for k,v in s.items() if k!="note"},
                         {"registered_nodes":4,"online_agents":3,"reported_rpc_ready_online":2,"candidate_workers":2,
                          "candidate_reported_free_ram_gb":9.5,"candidates_with_unknown_free_ram":1})
        self.assertIn("reported",s["note"])

    def test_sample_data_for_the_launcher(self):
        """SAMPLE DATA: a 16 GB ready helper and an 8 GB offline helper (made-up addresses)."""
        ready=self.add("Shafin-16GB",address="100.101.102.103",free=9.5,ram=16)
        gone=self.add("Rodel-8GB",address="100.101.102.104",free=5.0,ram=8)
        registry._nodes[gone]["last_seen"]-=registry.OFFLINE_AFTER+1
        d=workers.get_candidate_workers().as_dict()
        self.assertEqual([(c["node_name"],c["endpoint"],c["ram_gb"],c["free_ram_gb"]) for c in d["candidates"]],
                         [("Shafin-16GB","100.101.102.103:50052",16.0,9.5)])
        self.assertEqual(d["rejected"],[{"node_id":gone,"node_name":"Rodel-8GB","reason":"offline"}])
        self.assertEqual(json.loads(json.dumps(d)),d)   # plain JSON, so Rodel's API can return it unchanged


if __name__=="__main__":
    unittest.main(verbosity=2)
