"""Proof-contract tests for scripts/Collect-Inference-Evidence.ps1 and the evidence labels.

These run the REAL PowerShell script against SYNTHETIC log fixtures shaped like the pinned llama.cpp b12110 output
(format strings taken from llama.dll / ggml-rpc.dll). They prove the parser's PASS/FAIL/AMBIGUOUS rules; they do NOT
prove that any GPU, helper laptop or model ran. Physical proof needs real logs from a real session.

Each test copies the script into a temporary project folder, so nothing is read from or written to the real
.runtime/logs. Run from the project root:
    .venv/Scripts/python.exe -m pytest -q tests/test_engine_proof_contract.py
"""
import json, os, re, shutil, subprocess, sys, tempfile, time, unittest
from datetime import datetime, timedelta, timezone
sys.path.insert(0,os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # so `coordinator` imports from the project root
from coordinator import registry, workers

ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT=os.path.join(ROOT,"scripts","Collect-Inference-Evidence.ps1")
POWERSHELL=shutil.which("powershell.exe") or shutil.which("pwsh")


def master_log(devices, layers_per_device=8, kv_only=(), buffer_style="indexed", loads=1):
    """A synthetic b12110-style master log. devices: [("CUDA0","GPU name"),("RPC0","100.64.0.2:50052"),...]."""
    out=[]
    for _ in range(loads):
        out.append("llama_model_loader: loaded meta data with 34 key-value pairs and 291 tensors from model.gguf (version GGUF V3 (latest))")
        for name,detail in devices:
            out.append(f"llama_model_load_from_file_impl: using device {name} ({detail}) (unknown id) - 7000 MiB free")
        layer=0
        for name,_ in devices:
            for _ in range(layers_per_device):
                out.append(f"load_tensors: layer {layer:3d} assigned to device {name}, is_swa = 0"); layer+=1
        for name,detail in devices:
            if name in kv_only: out.append(f"llama_kv_cache: {name:>12s} KV buffer size =    12.00 MiB"); continue
            token=name if not name.startswith("RPC") or buffer_style=="plain" else (f"{name}[{detail}]" if buffer_style=="indexed" else f"RPC[{detail}]")
            out.append(f"load_tensors: {token:>12s} model buffer size =   100.00 MiB")
    return "\n".join(out)+"\n"


TWO_HELPERS=[("CUDA0","NVIDIA GeForce RTX 4060 Laptop GPU"),("RPC0","100.64.0.2:50052"),("RPC1","100.64.0.3:50052")]


@unittest.skipUnless(POWERSHELL and os.name=="nt","needs Windows PowerShell")
class EvidenceScriptContractTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root=tmp.name
        os.makedirs(os.path.join(self.root,"scripts"))
        shutil.copy(SCRIPT,os.path.join(self.root,"scripts"))
        self.logs=os.path.join(self.root,".runtime","logs"); os.makedirs(self.logs)

    def run_script(self, *args):
        cmd=[POWERSHELL,"-NoProfile","-ExecutionPolicy","Bypass","-File",os.path.join(self.root,"scripts","Collect-Inference-Evidence.ps1"),*args]
        p=subprocess.run(cmd,capture_output=True,text=True,timeout=120,cwd=self.root)
        return p.returncode,p.stdout+p.stderr

    def write(self, name, text, age_minutes=0):
        path=os.path.join(self.logs,name)
        with open(path,"w",encoding="utf-8") as f: f.write(text)
        if age_minutes:
            t=time.time()-age_minutes*60; os.utime(path,(t,t))
        return path

    def master(self, text, *extra, name="master-remote.log"):   # master allocation tests start here
        self.write(name,text)
        return self.run_script("-Mode","Master","-MasterLog",os.path.join(self.logs,name),*extra)

    def test_two_distinct_helpers_with_weights_pass(self):
        code,out=self.master(master_log(TWO_HELPERS),"-ExpectRpc","2")
        self.assertEqual(code,0,out)
        self.assertIn("MASTER ALLOCATION PASS : 2 RPC device(s) hold model weights on 2 distinct host(s)",out)

    def test_kv_cache_only_on_a_helper_is_not_weights(self):
        code,out=self.master(master_log(TWO_HELPERS,kv_only=("RPC1",)),"-ExpectRpc","2")
        self.assertEqual(code,1,out); self.assertIn("MASTER ALLOCATION FAIL",out)

    def test_two_rpc_devices_on_one_machine_are_one_host(self):
        same=[("CUDA0","GPU"),("RPC0","100.64.0.2:50052"),("RPC1","100.64.0.2:50053")]
        code,out=self.master(master_log(same),"-ExpectRpc","2")
        self.assertEqual(code,1,out); self.assertIn("on 1 distinct host(s)",out)

    def test_loopback_helpers_never_pass(self):
        loop=[("CUDA0","GPU"),("RPC0","127.0.0.1:50052")]
        code,out=self.master(master_log(loop),"-ExpectRpc","1")
        self.assertEqual(code,1,out); self.assertIn("LOOPBACK",out)

    def test_unindexed_rpc_buffer_maps_through_using_device_line(self):
        code,out=self.master(master_log(TWO_HELPERS,buffer_style="unindexed"),"-ExpectRpc","2")
        self.assertEqual(code,0,out)

    def test_unmappable_rpc_buffer_is_fail_not_guess(self):
        text=master_log(TWO_HELPERS).replace("RPC1[100.64.0.3:50052] model buffer","RPC[100.64.0.9:50052] model buffer")
        code,out=self.master(text,"-ExpectRpc","2")
        self.assertEqual(code,1,out); self.assertIn("RPC?[100.64.0.9:50052]",out)

    def test_only_the_last_model_load_counts(self):
        first=master_log(TWO_HELPERS)
        second=master_log([("CUDA0","GPU"),("RPC0","100.64.0.2:50052")])   # a later reload without RPC1
        code,out=self.master(first+second,"-ExpectRpc","2")
        self.assertEqual(code,1,out); self.assertIn("2 model loads",out)

    def test_stale_master_log_is_refused_unless_explicitly_allowed(self):
        self.write("master-remote.log",master_log(TWO_HELPERS),age_minutes=180)
        code,out=self.run_script("-Mode","Master","-ExpectRpc","2")
        self.assertEqual(code,1,out); self.assertIn("STALE",out)
        code,out=self.run_script("-Mode","Master","-ExpectRpc","2","-AllowOldLog")
        self.assertEqual(code,0,out)

    def test_api_log_uses_the_newest_launch_log_not_an_older_good_one(self):
        self.write("api-launcher-20261108-090000-aaaaaaaa.log",master_log(TWO_HELPERS),age_minutes=5)
        self.write("api-launcher-20261108-091000-bbbbbbbb.log","line 0\nline 1\n")   # newest: a run that never loaded
        code,out=self.run_script("-Mode","Master","-ApiLog","-ExpectRpc","2")
        self.assertEqual(code,1,out)
        self.assertIn("api-launcher-20261108-091000-bbbbbbbb.log",out); self.assertIn("2 API launch logs",out)

    def test_api_log_missing_is_not_run(self):
        code,out=self.run_script("-Mode","Master","-ApiLog")
        self.assertEqual(code,1,out); self.assertIn("MASTER ALLOCATION NOT RUN",out)

    def test_utf16_log_from_tee_object_parses(self):
        path=os.path.join(self.logs,"master-remote.log")
        with open(path,"w",encoding="utf-16") as f: f.write(master_log(TWO_HELPERS))   # PowerShell 5.1 Tee-Object writes UTF-16 LE + BOM
        code,out=self.run_script("-Mode","Master","-ExpectRpc","2")
        self.assertEqual(code,0,out)

    def graph(self, n): return "".join(f"[graph_compute] device: CPU, n_nodes: 42\n" for _ in range(n))   # helper compute tests start here

    def snap(self, label, *extra):
        return self.run_script("-Mode","HelperSnapshot","-Label",label,"-Session","run1","-HelperName","Shafin",*extra)

    def compare(self, *extra):
        return self.run_script("-Mode","HelperCompare","-Session","run1","-HelperName","Shafin",*extra)

    def test_fresh_positive_delta_passes_once_and_snapshots_are_used_up(self):
        log=self.write("helper-rpc.log","Starting RPC server\n"+self.graph(3))
        self.assertEqual(self.snap("before")[0],0)
        with open(log,"a",encoding="utf-8") as f: f.write(self.graph(5))
        self.assertEqual(self.snap("after")[0],0)
        code,out=self.compare()
        self.assertEqual(code,0,out)
        self.assertRegex(out,r"SHAFIN HELPER COMPUTE PASS : session=run1 delta=5 window=\S+/\S+")
        code,out=self.compare()
        self.assertEqual(code,1,out); self.assertIn("NOT RUN",out)   # the same pair can never be counted twice

    def test_zero_delta_fails(self):
        self.write("helper-rpc.log","Starting RPC server\n"+self.graph(3))
        self.snap("before"); self.snap("after")
        code,out=self.compare()
        self.assertEqual(code,1,out); self.assertIn("SHAFIN HELPER COMPUTE FAIL",out)

    def test_rewritten_log_is_ambiguous_even_with_more_lines(self):
        log=self.write("helper-rpc.log","Starting RPC server A\n"+self.graph(3))
        self.snap("before")
        with open(log,"w",encoding="utf-8") as f: f.write("Starting RPC server B (restarted)\n"+self.graph(9))
        self.snap("after")
        code,out=self.compare()
        self.assertEqual(code,2,out); self.assertIn("AMBIGUOUS",out); self.assertIn("rewritten",out)

    def test_shrunk_log_is_ambiguous(self):
        log=self.write("helper-rpc.log","Starting RPC server\n"+self.graph(9))
        self.snap("before")
        with open(log,"w",encoding="utf-8") as f: f.write("Starting RPC server\n"+self.graph(1))
        self.snap("after")
        code,out=self.compare()
        self.assertEqual(code,2,out); self.assertIn("shrank",out)

    def test_overlong_window_is_ambiguous(self):
        log=self.write("helper-rpc.log","Starting RPC server\n"+self.graph(3))
        self.snap("before")
        before=os.path.join(self.logs,"evidence","helper-run1-before.json")
        with open(before,encoding="utf-8-sig") as f: data=json.load(f)
        data["taken_at"]=(datetime.now(timezone.utc)-timedelta(minutes=30)).isoformat()   # a forgotten old "before"
        with open(before,"w",encoding="utf-8") as f: json.dump(data,f)
        with open(log,"a",encoding="utf-8") as f: f.write(self.graph(4))
        self.snap("after")
        code,out=self.compare()
        self.assertEqual(code,2,out); self.assertIn("window",out)

    def test_new_before_discards_an_old_after(self):
        self.write("helper-rpc.log","Starting RPC server\n"+self.graph(3))
        self.snap("before"); self.snap("after"); self.snap("before")
        code,out=self.compare()
        self.assertEqual(code,1,out); self.assertIn("NOT RUN",out)

    def line(self, name, verdict="PASS", session="run1", start="2026-11-08T15:01:00Z", end="2026-11-08T15:01:40Z"):   # same-prompt tests start here
        return f"{name} HELPER COMPUTE {verdict} : session={session} delta=12 window={start}/{end} graph_calls_before=3 after=15 (x)"

    def same(self, chat, *lines):
        return self.run_script("-Mode","SamePrompt","-ChatSentAt",chat,"-HelperResult","\n".join(lines))   # one pasted block; the script splits lines

    def test_same_prompt_passes_only_when_both_windows_contain_the_chat(self):
        code,out=self.same("2026-11-08T15:01:20Z",self.line("RODEL"),self.line("SHAFIN"))
        self.assertEqual(code,0,out); self.assertIn("SAME PROMPT PROOF PASS : RODEL, SHAFIN",out)
        code,out=self.same("2026-11-08T15:05:00Z",self.line("RODEL"),self.line("SHAFIN"))
        self.assertEqual(code,1,out); self.assertIn("does not contain the chat time",out)

    def test_same_prompt_rejects_fail_mixed_sessions_duplicates_and_missing_helpers(self):
        cases={"reported FAIL":(self.line("RODEL"),self.line("SHAFIN","FAIL")),
               "different sessions":(self.line("RODEL"),self.line("SHAFIN",session="run2")),
               "more than once":(self.line("SHAFIN"),self.line("SHAFIN")),
               "1 distinct helper":(self.line("SHAFIN"),),
               "reported AMBIGUOUS":(self.line("RODEL"),self.line("SHAFIN","AMBIGUOUS"))}
        for reason,lines in cases.items():
            with self.subTest(reason=reason):
                code,out=self.same("2026-11-08T15:01:20Z",*lines)
                self.assertEqual(code,1,out); self.assertIn("SAME PROMPT PROOF FAIL",out); self.assertIn(reason,out)

    def test_real_helper_compare_lines_feed_same_prompt(self):
        log=self.write("helper-rpc.log","Starting RPC server\n"+self.graph(3))
        self.snap("before")
        chat=datetime.now(timezone.utc).isoformat()
        with open(log,"a",encoding="utf-8") as f: f.write(self.graph(6))
        self.snap("after")
        code,shafin=self.compare(); self.assertEqual(code,0,shafin)
        shafin_line=[l for l in shafin.splitlines() if "HELPER COMPUTE" in l][0]
        window=re.search(r"window=(\S+)/(\S+)",shafin_line)
        rodel_line=self.line("RODEL",start=window.group(1),end=window.group(2))   # stands in for Rodel's laptop
        code,out=self.same(chat,rodel_line,shafin_line)
        self.assertEqual(code,0,out)


class EvidenceLabelContractTests(unittest.TestCase):
    """rpc_ready and online are readiness signals; nothing in selection may claim compute participation."""
    def setUp(self):
        registry.clear(); self.addCleanup(registry.clear)

    def test_candidates_carry_readiness_only_never_a_compute_claim(self):
        registry.register_node("Helper",16,50052,address="100.64.0.2",free_ram_gb=8.0,rpc_ready=True)
        d=workers.get_candidate_workers().as_dict()
        text=json.dumps(d).lower()
        for claim in ("compute","participat","verified","proof","weights","graph"):
            self.assertNotIn(claim,text)
        self.assertEqual(d["candidates"][0]["rpc_ready"],True)   # reported by the helper; the label stays "rpc_ready"

    def test_public_node_listing_has_no_compute_fields(self):
        registry.register_node("Helper",16,50052,address="100.64.0.2",rpc_ready=True)
        node=registry.get_all_nodes()[0]
        self.assertEqual(set(node),{"node_id","node_name","ram_gb","free_ram_gb","rpc_port","address","rpc_ready","status","last_seen_s"})


if __name__=="__main__":
    unittest.main(verbosity=2)
