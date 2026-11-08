"""LowLlama Mission Control: setup wizard (master or helper) plus the space-themed dashboard.

One local web page walks a laptop through setup from scratch:
    Master: Tailscale -> download runtime + model -> NVIDIA check -> start coordinator -> share an invite code
    Helper: Tailscale -> download CPU runtime -> paste invite code -> join (starts ggml-rpc-server + agent)

The browser never sees the admin token. When this server starts the coordinator it generates the token itself
and keeps it in memory; otherwise it reads LOWLLAMA_ADMIN_TOKEN from its own environment. It listens on
127.0.0.1 only and refuses requests for any other Host, and every POST must carry the X-LowLlama-UI header,
so other websites cannot drive it from the browser.

Run:
    python ui\\server.py                 (real setup on this laptop)
    python ui\\server.py --mock          (simulated laptops and processes; nothing real happens)
    python ui\\server.py --coordinator http://<COORDINATOR_IP>:8000   (coordinator already running elsewhere;
                                          set LOWLLAMA_ADMIN_TOKEN in this terminal first)
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import os
import random
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

UI_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = UI_DIR.parent
STATIC_DIR = UI_DIR / "static"
RUNTIME_DIR = PROJECT_ROOT / ".runtime"
LOG_DIR = RUNTIME_DIR / "logs"
CPU_RPC_EXE = RUNTIME_DIR / "llama-b12110-cpu" / "ggml-rpc-server.exe"
CUDA_SERVER_EXE = RUNTIME_DIR / "llama-b12110-cuda" / "llama-server.exe"
DEMO_MODEL_ID = "qwen2.5-0.5b-instruct-q4_k_m"
DEMO_MODEL_FILE = RUNTIME_DIR / "models" / f"{DEMO_MODEL_ID}.gguf"
INSTALL_SCRIPT = PROJECT_ROOT / "scripts" / "Install-Runtime.ps1"
TAILSCALE_EXE = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
TAILNET = ipaddress.ip_network("100.64.0.0/10")
COORDINATOR_PORT = 8000  # invites.create_invite always advertises port 8000
RPC_PORT = 50052
FORWARD_TIMEOUT_S = 15.0
CHAT_TIMEOUT_S = 130.0  # the coordinator itself waits up to 120 s for llama-server
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
AGENT_EVENTS = ("helper_agent_started", "stored_registration_loaded", "helper_registered", "helper_reconnected",
                "heartbeat_sent", "heartbeat_restored", "heartbeat_failed", "heartbeat_retry_failed",
                "enrollment_connection_failed", "enrollment_rejected", "enrollment_conflict",
                "enrollment_outcome_unknown", "registration_credentials_rejected", "reenrollment_scheduled",
                "reconnect_failed", "credential_persistence_failed", "helper_agent_startup_failed", "shutdown_requested")

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_models() -> list[str]:
    """Model IDs from the launcher catalog (read only), demo model first."""
    try:
        from coordinator.launcher import MODEL_ALLOWLIST
    except Exception:  # catalog unavailable: offer the demo model only
        return [DEMO_MODEL_ID]
    return sorted(MODEL_ALLOWLIST, key=lambda m: (m != DEMO_MODEL_ID, m))


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class ManagedProcess:
    """A child process started by this dashboard; output goes to a log file and a short in-memory tail."""

    def __init__(self, name: str, mock: bool) -> None:
        self.name = name
        self.mock = mock
        self.proc: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=300)
        self.log_path: Path | None = None
        self.started_at: float | None = None
        self.sim_running = False
        self.sim_exit: int | None = None

    def running(self) -> bool:
        if self.mock:
            return self.sim_running
        return self.proc is not None and self.proc.poll() is None

    def exit_code(self) -> int | None:
        if self.mock:
            return self.sim_exit
        if self.proc is None or self.running():
            return None
        return self.proc.returncode

    def start(self, args: list[str], env: dict[str, str] | None = None, log_name: str | None = None,
              sim_lines: tuple[str, ...] = (), sim_finishes: bool = False) -> None:
        if self.running():
            raise HTTPException(409, f"{self.name} is already running")
        self.lines.clear()
        self.started_at = time.time()
        self.log_path = LOG_DIR / (log_name or f"ui-{self.name}.log")
        if self.mock:
            self.lines.extend(f"[SIMULATION] {line}" for line in sim_lines)
            self.sim_running = not sim_finishes
            self.sim_exit = 0 if sim_finishes else None
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        child_env = dict(os.environ)
        child_env.update(env or {})
        try:
            self.proc = subprocess.Popen(args, cwd=PROJECT_ROOT, env=child_env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
        except OSError as exc:
            self.proc = None
            raise HTTPException(500, f"Could not start {self.name}: {exc.strerror or exc}")
        threading.Thread(target=self._pump, args=(self.proc, self.log_path), daemon=True).start()

    def _pump(self, proc: subprocess.Popen, path: Path) -> None:
        with open(path, "w", encoding="utf-8", errors="replace") as log:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                log.write(line + "\n")
                log.flush()
                self.lines.append(line)

    def stop(self) -> None:
        if self.mock:
            if self.sim_running:
                self.lines.append("[SIMULATION] stopped")
            self.sim_running = False
            return
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def info(self) -> dict[str, Any]:
        log = None
        if self.log_path:
            try:
                log = str(self.log_path.relative_to(PROJECT_ROOT))
            except ValueError:
                log = str(self.log_path)
        return {"running": self.running(), "exit_code": self.exit_code(), "log": log,
                "tail": list(self.lines)[-30:], "started_at": self.started_at}


class MockCoordinator:
    """In-memory stand-in for the coordinator so the UI can be built and rehearsed offline. Clearly fake."""

    def __init__(self) -> None:
        now = time.time()
        self.nodes = [
            {"node_id": "node-sim00001", "node_name": "sim-helper-alpha", "ram_gb": 15.7, "free_ram_gb": 2.5,
             "rpc_port": RPC_PORT, "address": "100.64.0.11", "rpc_ready": True, "status": "online", "seen": now},
            {"node_id": "node-sim00002", "node_name": "sim-helper-beta", "ram_gb": 7.8, "free_ram_gb": 1.9,
             "rpc_port": RPC_PORT, "address": "100.64.0.12", "rpc_ready": True, "status": "online", "seen": now},
            {"node_id": "node-sim00003", "node_name": "sim-helper-gamma", "ram_gb": 7.9, "free_ram_gb": 0.6,
             "rpc_port": RPC_PORT, "address": "100.64.0.13", "rpc_ready": False, "status": "offline", "seen": now - 240},
        ]
        self.state: dict[str, Any] = {"status": "stopped", "model_id": None, "worker_ids": None, "error": None}
        self.ready_at = 0.0

    def get_nodes(self) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for node in self.nodes:
            if node["status"] == "online":
                node["seen"] = now - random.uniform(0, 4)  # fake heartbeat
                node["free_ram_gb"] = round(max(0.3, node["free_ram_gb"] + random.uniform(-0.08, 0.08)), 2)
            item = {k: v for k, v in node.items() if k != "seen"}
            item["last_seen_s"] = round(now - node["seen"], 1)
            out.append(item)
        return out

    def invite(self) -> dict[str, Any]:
        code = "SIM-" + "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8))
        expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 900))
        return {"invite_code": code, "expires_at": expires}

    def status(self) -> dict[str, Any]:
        if self.state["status"] == "starting" and time.time() >= self.ready_at:
            self.state["status"] = "ready"
        return dict(self.state)

    def start(self, body: dict[str, Any]) -> dict[str, Any]:
        node_ids = body.get("node_ids") or []
        if self.status()["status"] in ("starting", "ready"):
            raise HTTPException(409, "Launcher is busy; stop the running model first")
        if not node_ids:
            raise HTTPException(400, "Selection must not be empty")
        by_id = {n["node_id"]: n for n in self.nodes}
        bad = [f"{nid}: {'unknown' if nid not in by_id else 'offline'}" for nid in node_ids
               if nid not in by_id or by_id[nid]["status"] != "online"]
        if bad:
            raise HTTPException(400, "Selected workers unavailable: " + "; ".join(bad))
        self.state = {"status": "starting", "model_id": body.get("model_id"), "worker_ids": list(node_ids), "error": None}
        self.ready_at = time.time() + 4.0
        return dict(self.state)

    def stop(self) -> dict[str, Any]:
        self.state = {"status": "stopped", "model_id": None, "worker_ids": None, "error": None}
        return {"status": "stopped"}

    async def chat(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.status()["status"] != "ready":
            raise HTTPException(503, f"Model not ready (status: {self.state['status']})")
        await asyncio.sleep(1.2)
        last = next((m.get("content", "") for m in reversed(body.get("messages", [])) if m.get("role") == "user"), "")
        text = (f"[SIMULATION] No model ran. A real answer to \"{last[:80]}\" will appear here once the "
                f"coordinator is connected and helpers {', '.join(self.state['worker_ids'] or [])} are serving.")
        return {"model": self.state["model_id"], "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


class SetupProbe:
    """Read-only checks of this laptop, cached briefly so polling stays cheap."""

    def __init__(self, mock: bool) -> None:
        self.mock = mock
        self._ts: tuple[float, dict[str, Any]] | None = None
        self._cuda: tuple[float, dict[str, Any]] | None = None

    def tailscale(self) -> dict[str, Any]:
        if self.mock:
            return {"ok": True, "ip": "100.64.0.7", "detail": "[SIMULATION] connected"}
        if self._ts and time.time() - self._ts[0] < 8:
            return self._ts[1]
        result = self._read_tailscale()
        self._ts = (time.time(), result)
        return result

    def _read_tailscale(self) -> dict[str, Any]:
        if not TAILSCALE_EXE.is_file():
            return {"ok": False, "ip": None, "detail": "Tailscale is not installed on this laptop"}
        try:
            out = subprocess.run([str(TAILSCALE_EXE), "ip", "-4"], capture_output=True, text=True, timeout=6,
                                 creationflags=NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            return {"ok": False, "ip": None, "detail": "Tailscale did not answer"}
        for line in out.stdout.splitlines():
            try:
                ip = ipaddress.ip_address(line.strip())
            except ValueError:
                continue
            if ip in TAILNET:
                return {"ok": True, "ip": str(ip), "detail": "connected"}
        return {"ok": False, "ip": None, "detail": "Tailscale is installed but not signed in to a network"}

    def runtime(self, role: str) -> dict[str, Any]:
        if self.mock:
            return {"ok": True, "detail": "[SIMULATION] runtime present"}
        if role == "master":
            missing = [p.name for p in (CUDA_SERVER_EXE, DEMO_MODEL_FILE) if not p.is_file()]
            return {"ok": not missing, "detail": "CUDA runtime and demo model present" if not missing else "missing: " + ", ".join(missing)}
        ok = CPU_RPC_EXE.is_file()
        return {"ok": ok, "detail": "CPU RPC runtime present" if ok else "missing: ggml-rpc-server.exe"}

    def cuda(self) -> dict[str, Any]:
        if self.mock:
            return {"ok": True, "detail": "[SIMULATION] CUDA0 found"}
        if not CUDA_SERVER_EXE.is_file():
            return {"ok": False, "detail": "download the runtime first"}
        if self._cuda and (self._cuda[1]["ok"] or time.time() - self._cuda[0] < 30):
            return self._cuda[1]
        try:
            out = subprocess.run([str(CUDA_SERVER_EXE), "--list-devices"], capture_output=True, text=True, timeout=40,
                                 cwd=CUDA_SERVER_EXE.parent, creationflags=NO_WINDOW)
            text = out.stdout + out.stderr
            gpu = next((line.strip() for line in text.splitlines() if line.strip().startswith("CUDA0")), "")
            result = {"ok": bool(gpu), "detail": gpu or "no CUDA0 device: this laptop cannot be the master"}
        except (OSError, subprocess.TimeoutExpired):
            result = {"ok": False, "detail": "llama-server --list-devices did not finish"}
        self._cuda = (time.time(), result)
        return result


def agent_event(lines: list[str]) -> str | None:
    for line in reversed(lines):
        for event in AGENT_EVENTS:
            if event in line:
                return event
    return None


def create_app(coordinator_url: str, mock: bool, port: int) -> FastAPI:
    sim = MockCoordinator() if mock else None
    probe = SetupProbe(mock)
    models = load_models()
    link = {"base": coordinator_url.rstrip("/"), "token": os.environ.get("LOWLLAMA_ADMIN_TOKEN", ""), "owned": False}
    procs = {name: ManagedProcess(name, mock) for name in ("install", "coordinator", "rpc", "agent")}
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    async def forward(method: str, path: str, body: Any = None, timeout: float = FORWARD_TIMEOUT_S) -> JSONResponse:
        if not link["token"]:
            raise HTTPException(503, "No admin token: start the coordinator from Setup, or set LOWLLAMA_ADMIN_TOKEN before running ui/server.py")
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, link["base"] + path, json=body,
                                            headers={"Authorization": f"Bearer {link['token']}"})
        except httpx.TimeoutException:
            raise HTTPException(504, "Coordinator did not answer in time")
        except httpx.RequestError:
            raise HTTPException(502, "Coordinator unreachable")
        try:
            data = resp.json()
        except ValueError:
            data = {"detail": "Coordinator returned a non-JSON response"}
        return JSONResponse(data, status_code=resp.status_code)

    async def coordinator_health(base: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.5) as client:
                return (await client.get(base + "/health")).status_code == 200
        except httpx.RequestError:
            return False

    async def json_body(request: Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Request body must be JSON")
        if not isinstance(body, dict):
            raise HTTPException(400, "Request body must be a JSON object")
        return body

    async def stop_coordinator() -> None:
        if procs["coordinator"].running() and not mock:
            try:
                await forward("POST", "/stop", timeout=30.0)  # stop llama-server first so it is not orphaned
            except HTTPException:
                pass
        await asyncio.to_thread(procs["coordinator"].stop)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        await stop_coordinator()
        for name in ("agent", "rpc", "install"):
            await asyncio.to_thread(procs[name].stop)

    app = FastAPI(title="LowLlama Mission Control", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)  # blocks DNS rebinding
        if request.method not in ("GET", "HEAD") and request.headers.get("x-lowllama-ui") != "1":
            return JSONResponse({"detail": "Missing X-LowLlama-UI header"}, status_code=403)  # blocks cross-site POSTs
        return await call_next(request)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/config")
    async def config() -> dict[str, Any]:
        return {"mock": mock, "models": models, "default_model": DEMO_MODEL_ID, "token_set": bool(link["token"]) or mock,
                "hostname": "sim-laptop" if mock else socket.gethostname()}

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        if sim:
            return {"coordinator": "simulation"}
        return {"coordinator": "online" if await coordinator_health(link["base"]) else "unreachable"}

    @app.get("/api/nodes")
    async def nodes():
        return sim.get_nodes() if sim else await forward("GET", "/nodes")

    @app.post("/api/invite")
    async def invite():
        return sim.invite() if sim else await forward("POST", "/invite", {})

    @app.get("/api/status")
    async def status():
        return sim.status() if sim else await forward("GET", "/status")

    @app.post("/api/start")
    async def start(request: Request):
        body = await json_body(request)
        clean = {"model_id": body.get("model_id"), "node_ids": body.get("node_ids")}  # only the fields /start accepts
        return sim.start(clean) if sim else await forward("POST", "/start", clean, timeout=CHAT_TIMEOUT_S)

    @app.post("/api/stop")
    async def stop():
        return sim.stop() if sim else await forward("POST", "/stop", timeout=30.0)

    @app.post("/api/chat")
    async def chat(request: Request):
        body = await json_body(request)
        clean = {"messages": body.get("messages", []), "max_tokens": body.get("max_tokens", 256),
                 "temperature": body.get("temperature", 0.7), "stream": False}
        return await sim.chat(clean) if sim else await forward("POST", "/chat", clean, timeout=CHAT_TIMEOUT_S)

    @app.get("/api/setup/state")
    async def setup_state(role: str = "helper") -> dict[str, Any]:
        ts = await asyncio.to_thread(probe.tailscale)
        result: dict[str, Any] = {"role": role, "tailscale": ts, "runtime": probe.runtime(role),
                                  "processes": {name: p.info() for name, p in procs.items()}}
        if role == "master":
            result["cuda"] = await asyncio.to_thread(probe.cuda) if result["runtime"]["ok"] else {"ok": False, "detail": "download the runtime first"}
            if mock:
                up = procs["coordinator"].running()
            else:
                up = await coordinator_health(link["base"])
            result["coordinator"] = {"reachable": up, "url": link["base"] if not mock else "simulation",
                                     "started_here": procs["coordinator"].running(), "token_known": bool(link["token"]) or mock}
        else:
            agent_lines = list(procs["agent"].lines)
            rpc_up = procs["rpc"].running() if mock else bool(ts["ip"]) and await asyncio.to_thread(tcp_open, ts["ip"], RPC_PORT, 0.5)
            result["helper"] = {"rpc_listening": rpc_up, "rpc_exe": str(CPU_RPC_EXE), "agent_event": agent_event(agent_lines) or ("helper_registered" if mock and procs["agent"].running() else None)}
        return result

    @app.post("/api/setup/install")
    async def setup_install(request: Request):
        role = (await json_body(request)).get("role")
        if role not in ("master", "helper"):
            raise HTTPException(400, "role must be master or helper")
        args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALL_SCRIPT),
                "-Role", "Master" if role == "master" else "Helper"]
        procs["install"].start(args, log_name=f"ui-install-{role}.log",
                               sim_lines=("Downloading pinned llama.cpp b12110", "SHA256 verified", "Runtime ready"),
                               sim_finishes=True)
        return procs["install"].info()

    @app.post("/api/setup/coordinator/start")
    async def setup_coordinator_start():
        ts = await asyncio.to_thread(probe.tailscale)
        if not ts["ok"]:
            raise HTTPException(400, "Connect Tailscale first: helpers reach the coordinator on this laptop's Tailscale address")
        ip = ts["ip"]
        if not mock and await asyncio.to_thread(tcp_open, ip, COORDINATOR_PORT, 0.5):
            raise HTTPException(409, f"Something already listens on {ip}:{COORDINATOR_PORT}. If that is a coordinator you "
                                     "started in a terminal, restart this dashboard with --coordinator and LOWLLAMA_ADMIN_TOKEN set.")
        token = link["token"] or secrets.token_urlsafe(32)
        args = [sys.executable, "-m", "uvicorn", "coordinator.main:app", "--host", ip, "--port", str(COORDINATOR_PORT)]
        procs["coordinator"].start(args, env={"LOWLLAMA_ADMIN_TOKEN": token, "LOWLLAMA_MASTER_IP": ip, "PYTHONUNBUFFERED": "1"},
                                   sim_lines=(f"Uvicorn running on http://{ip}:{COORDINATOR_PORT}",))
        link.update(base=f"http://{ip}:{COORDINATOR_PORT}", token=token, owned=True)
        return {"started": True, "url": link["base"]}

    @app.post("/api/setup/coordinator/stop")
    async def setup_coordinator_stop():
        await stop_coordinator()
        return {"stopped": True}

    def read_invite(code: Any) -> dict[str, Any]:
        if mock and isinstance(code, str) and code.strip().startswith("SIM-"):
            return {"master_ip": "100.64.0.1", "port": COORDINATOR_PORT, "expires": time.time() + 900}
        from coordinator.invites import InviteError, decode
        try:
            payload = decode(code)
        except InviteError:
            raise HTTPException(400, "That invite code is not readable. Copy the whole code again.")
        try:
            master_ip = str(ipaddress.ip_address(str(payload.get("master_ip", ""))))
            port_num = int(payload.get("port", COORDINATOR_PORT))
        except (ValueError, TypeError):
            raise HTTPException(400, "The invite code does not contain a usable master address")
        if not 1 <= port_num <= 65535:
            raise HTTPException(400, "The invite code contains an invalid port")
        return {"master_ip": master_ip, "port": port_num, "expires": payload.get("expires")}

    @app.post("/api/setup/invite-info")
    async def setup_invite_info(request: Request):
        info = read_invite((await json_body(request)).get("code"))
        base = f"http://{info['master_ip']}:{info['port']}"
        reachable = True if mock else await coordinator_health(base)
        expires = info.get("expires")
        expires_in = int(expires - time.time()) if isinstance(expires, (int, float)) else None
        in_tailnet = ipaddress.ip_address(info["master_ip"]) in TAILNET
        return {"master_ip": info["master_ip"], "port": info["port"], "reachable": reachable,
                "expires_in_s": expires_in, "in_tailnet": in_tailnet}

    @app.post("/api/setup/helper/start")
    async def setup_helper_start(request: Request):
        body = await json_body(request)
        info = read_invite(body.get("code"))
        name = str(body.get("node_name") or "").strip()
        if not 1 <= len(name) <= 64 or any(ord(c) < 32 for c in name):
            raise HTTPException(400, "Laptop name must be 1-64 characters")
        try:
            threads = int(body.get("threads", 4))
        except (TypeError, ValueError):
            threads = 4
        threads = max(1, min(threads, os.cpu_count() or 4))
        ts = await asyncio.to_thread(probe.tailscale)
        if not ts["ok"]:
            raise HTTPException(400, "Connect Tailscale first")
        if ts["ip"] == info["master_ip"]:
            raise HTTPException(400, "This invite points at this same laptop. A helper must be a different laptop than the master.")
        if not mock and not CPU_RPC_EXE.is_file():
            raise HTTPException(400, "Download the helper runtime first")
        if procs["agent"].running():
            raise HTTPException(409, "This laptop is already helping. Leave first to rejoin.")
        if not procs["rpc"].running():
            if not mock and await asyncio.to_thread(tcp_open, ts["ip"], RPC_PORT, 0.5):
                raise HTTPException(409, f"Port {RPC_PORT} is already in use here: an RPC server may be running in a terminal. Close it first.")
            procs["rpc"].start([str(CPU_RPC_EXE), "--device", "CPU", "--host", ts["ip"], "--port", str(RPC_PORT),
                                "--threads", str(threads)],
                               env={"GGML_RPC_DEBUG": "1"}, log_name="helper-rpc.log",
                               sim_lines=(f"Starting RPC server on {ts['ip']}:{RPC_PORT}", "Devices: CPU"))
        agent_env = {"LOWLLAMA_COORDINATOR_URL": f"http://{info['master_ip']}:{info['port']}", "LOWLLAMA_NODE_NAME": name,
                     "LOWLLAMA_INVITE_CODE": str(body.get("code")).strip(), "LOWLLAMA_RPC_HOST": ts["ip"],
                     "LOWLLAMA_RPC_PORT": str(RPC_PORT), "PYTHONUNBUFFERED": "1"}
        procs["agent"].start([sys.executable, "-m", "agent.agent"], env=agent_env, log_name="ui-agent.log",
                             sim_lines=("helper_agent_started", "helper_registered", "heartbeat_sent"))
        return {"started": True}

    @app.post("/api/setup/helper/stop")
    async def setup_helper_stop():
        await asyncio.to_thread(procs["agent"].stop)
        await asyncio.to_thread(procs["rpc"].stop)
        return {"stopped": True}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="LowLlama Mission Control: setup wizard and dashboard")
    parser.add_argument("--coordinator", default=os.environ.get("LOWLLAMA_COORDINATOR_URL", f"http://127.0.0.1:{COORDINATOR_PORT}"))
    parser.add_argument("--mock", action="store_true", help="simulated laptops and processes; nothing real happens")
    parser.add_argument("--port", type=int, default=8600)
    args = parser.parse_args()
    mode = "SIMULATION (nothing real)" if args.mock else f"coordinator {args.coordinator}"
    print(f"LowLlama Mission Control on http://127.0.0.1:{args.port}  [{mode}]", file=sys.stderr)
    uvicorn.run(create_app(args.coordinator, args.mock, args.port), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
