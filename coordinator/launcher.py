"""Safe lifecycle manager for the master llama.cpp server process.

``coordinator.main`` calls :func:`start` with a model ID and Shafin's
``CandidateSelection``.  Worker eligibility is advisory, so this module
validates and probes every RPC endpoint again before launching llama-server.
"""

from __future__ import annotations

import atexit
import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_BINARY = PROJECT_ROOT / ".runtime" / "llama-b12110-cuda" / "llama-server.exe"
MODEL_ALLOWLIST = {
    "llama-3-8b": PROJECT_ROOT / "models" / "llama-3-8b-instruct.Q4_K_M.gguf",
    "phi-3-mini": PROJECT_ROOT / "models" / "phi-3-mini-4k-instruct.Q4_K_M.gguf",
    "qwen2.5-0.5b-instruct-q4_k_m": (
        PROJECT_ROOT
        / ".runtime"
        / "models"
        / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
    ),
}

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8080
RPC_CONNECT_TIMEOUT_SECONDS = 2.0
RPC_COMPATIBILITY_TIMEOUT_SECONDS = 15.0
STARTUP_TIMEOUT_SECONDS = 60.0
HEALTH_POLL_INTERVAL_SECONDS = 0.25
STOP_TIMEOUT_SECONDS = 5.0
MAX_DIAGNOSTIC_LINES = 200

_TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)


class LauncherError(RuntimeError):
    """Base class for safe launcher failures."""


class LauncherBusyError(LauncherError):
    """Raised when a model process is already starting or ready."""


class LauncherConfigurationError(LauncherError):
    """Raised when a binary, model, or model ID is unavailable."""


class LauncherSelectionError(LauncherError):
    """Raised when the worker selection is absent or malformed."""


class RpcEndpointError(LauncherError):
    """Raised when a selected RPC endpoint is unsafe or unusable."""


_lock = threading.RLock()
_process: subprocess.Popen[str] | None = None
_monitor_thread: threading.Thread | None = None
_output_thread: threading.Thread | None = None
_generation = 0
_diagnostics: deque[str] = deque(maxlen=MAX_DIAGNOSTIC_LINES)
_snapshot: dict[str, Any] = {
    "status": "stopped",
    "model_id": None,
    "worker_ids": [],
    "error": None,
}


def start(model_id: str, selection: object) -> dict[str, Any]:
    """Start one allowlisted model with independently verified RPC workers.

    The function returns once the owned child has started and its readiness
    monitor is running.  Call :func:`status` until it reports ``ready`` or
    ``error``.  An empty selection is rejected so a requested distributed run
    can never silently fall back to local-only inference.
    """

    global _process, _monitor_thread, _output_thread, _generation

    with _lock:
        _refresh_process_state_locked()
        if _process is not None and _process.poll() is None:
            raise LauncherBusyError("A model process is already running")
        if _snapshot["status"] in {"starting", "ready"}:
            raise LauncherBusyError("A model process is already running")
        _generation += 1
        generation = _generation
        _set_snapshot_locked("starting", model_id, [], None)
        _diagnostics.clear()

    try:
        binary, model = _resolve_runtime(model_id)
        endpoints = _selection_endpoints(selection)
        worker_ids = _selection_worker_ids(selection, len(endpoints))
        for endpoint in endpoints:
            _probe_rpc_endpoint(endpoint, binary)
        _ensure_server_port_available()
        command = _build_command(binary, model, model_id, endpoints)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            creationflags=_child_creation_flags(),
        )
    except Exception as exc:
        error = _safe_error(exc)
        with _lock:
            if generation == _generation:
                _set_snapshot_locked("error", model_id, [], error)
        if isinstance(exc, LauncherError):
            raise
        raise LauncherError(error) from exc

    with _lock:
        if generation != _generation:
            _terminate_process(process)
            raise LauncherError("Model startup was cancelled")
        _process = process
        _set_snapshot_locked("starting", model_id, worker_ids, None)
        _output_thread = threading.Thread(
            target=_drain_output,
            args=(process,),
            name="lowllama-launcher-output",
            daemon=True,
        )
        _monitor_thread = threading.Thread(
            target=_monitor_startup,
            args=(process, generation),
            name="lowllama-launcher-readiness",
            daemon=True,
        )
        _output_thread.start()
        _monitor_thread.start()
        return _snapshot_copy_locked()


def stop() -> dict[str, Any]:
    """Stop only the llama-server child owned by this launcher."""

    global _process, _monitor_thread, _output_thread, _generation

    with _lock:
        _generation += 1
        process = _process
        monitor = _monitor_thread
        output = _output_thread
        _process = None
        _monitor_thread = None
        _output_thread = None
        _set_snapshot_locked("stopped", None, [], None)

    if process is not None:
        _terminate_process(process)
    current = threading.current_thread()
    for thread in (monitor, output):
        if thread is not None and thread is not current:
            thread.join(timeout=STOP_TIMEOUT_SECONDS)
    return status()


def status() -> dict[str, Any]:
    """Return a non-secret snapshot of the owned model process state."""

    with _lock:
        _refresh_process_state_locked()
        return _snapshot_copy_locked()


def _resolve_runtime(model_id: str) -> tuple[Path, Path]:
    if not isinstance(model_id, str) or model_id not in MODEL_ALLOWLIST:
        raise LauncherConfigurationError("Model ID is not allowlisted")
    binary = SERVER_BINARY.resolve()
    model = Path(MODEL_ALLOWLIST[model_id]).resolve()
    if not binary.is_file():
        raise LauncherConfigurationError("Pinned llama-server binary is missing")
    if not model.is_file():
        raise LauncherConfigurationError("Allowlisted model file is missing")
    return binary, model


def _selection_endpoints(selection: object) -> list[str]:
    endpoint_method = getattr(selection, "rpc_endpoints", None)
    if not callable(endpoint_method):
        raise LauncherSelectionError("Worker selection does not provide rpc_endpoints()")
    raw = endpoint_method()
    if not isinstance(raw, str):
        raise LauncherSelectionError("rpc_endpoints() must return text")
    endpoints = [item.strip() for item in raw.split(",") if item.strip()]
    if not endpoints:
        raise LauncherSelectionError("No usable RPC workers were selected")
    if len(set(endpoints)) != len(endpoints):
        raise LauncherSelectionError("Worker selection contains duplicate endpoints")
    for endpoint in endpoints:
        _parse_endpoint(endpoint)
    return endpoints


def _selection_worker_ids(selection: object, endpoint_count: int) -> list[str]:
    candidates = getattr(selection, "candidates", None)
    if candidates is None:
        raise LauncherSelectionError("Worker selection does not provide candidates")
    try:
        worker_ids = [candidate.node_id for candidate in candidates]
    except (AttributeError, TypeError) as exc:
        raise LauncherSelectionError("Worker candidates have an invalid shape") from exc
    if len(worker_ids) != endpoint_count:
        raise LauncherSelectionError("Worker IDs do not match RPC endpoints")
    if any(not isinstance(node_id, str) or not node_id.strip() for node_id in worker_ids):
        raise LauncherSelectionError("Worker IDs must be non-empty text")
    return worker_ids


def _parse_endpoint(endpoint: str) -> tuple[str, int]:
    if endpoint.startswith("["):
        closing = endpoint.find("]")
        if closing < 0 or closing + 1 >= len(endpoint) or endpoint[closing + 1] != ":":
            raise RpcEndpointError("RPC endpoint has an invalid IPv6 format")
        host = endpoint[1:closing]
        port_text = endpoint[closing + 2 :]
    else:
        host, separator, port_text = endpoint.rpartition(":")
        if not separator or ":" in host:
            raise RpcEndpointError("RPC endpoint must be IP:port")
    try:
        address = ipaddress.ip_address(host)
        port = int(port_text)
    except ValueError as exc:
        raise RpcEndpointError("RPC endpoint must contain a valid IP and port") from exc
    if str(port) != port_text or not 1 <= port <= 65535:
        raise RpcEndpointError("RPC endpoint port must be between 1 and 65535")
    allowed = any(
        address.version == network.version and address in network
        for network in _TAILSCALE_NETWORKS
    )
    allow_loopback = os.environ.get("LOWLLAMA_ALLOW_LOOPBACK_RPC") == "1"
    if not allowed and not (allow_loopback and address.is_loopback):
        raise RpcEndpointError("RPC endpoint must be a Tailscale address")
    return str(address), port


def _probe_rpc_endpoint(endpoint: str, binary: Path) -> None:
    """Require both TCP reachability and llama.cpp RPC device discovery."""

    host, port = _parse_endpoint(endpoint)
    try:
        connection = socket.create_connection(
            (host, port), timeout=RPC_CONNECT_TIMEOUT_SECONDS
        )
    except (OSError, TimeoutError) as exc:
        raise RpcEndpointError(f"RPC worker {endpoint} is unreachable") from exc
    else:
        connection.close()

    try:
        result = subprocess.run(
            [str(binary), "--rpc", endpoint, "--list-devices"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RPC_COMPATIBILITY_TIMEOUT_SECONDS,
            shell=False,
            creationflags=_probe_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RpcEndpointError(
            f"RPC compatibility probe failed for {endpoint}"
        ) from exc
    output = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0 or "RPC0:" not in output:
        raise RpcEndpointError(f"RPC worker {endpoint} is not llama.cpp compatible")


def _ensure_server_port_available() -> None:
    """Fail before launch when another process already owns the chat port."""

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((SERVER_HOST, SERVER_PORT))
    except OSError as exc:
        raise LauncherConfigurationError(
            f"Local inference port {SERVER_PORT} is already in use"
        ) from exc
    finally:
        probe.close()


def _build_command(
    binary: Path,
    model: Path,
    model_id: str,
    endpoints: list[str],
) -> list[str]:
    device_names = ["CUDA0", *(f"RPC{index}" for index in range(len(endpoints)))]
    split = ",".join("1" for _ in device_names)
    return [
        str(binary),
        "-m",
        str(model),
        "--alias",
        model_id,
        "--host",
        SERVER_HOST,
        "--port",
        str(SERVER_PORT),
        "--rpc",
        ",".join(endpoints),
        "--device",
        ",".join(device_names),
        "--split-mode",
        "layer",
        "--tensor-split",
        split,
        "-ngl",
        "all",
        "--fit",
        "off",
        "-c",
        "2048",
        "-np",
        "1",
        "-fa",
        "off",
        "-lv",
        "5",
    ]


def _monitor_startup(process: subprocess.Popen[str], generation: int) -> None:
    global _process

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with _lock:
            if generation != _generation or _process is not process:
                return
        return_code = process.poll()
        if return_code is not None:
            with _lock:
                if generation == _generation and _process is process:
                    _process = None
                    _set_snapshot_locked(
                        "error",
                        _snapshot["model_id"],
                        _snapshot["worker_ids"],
                        f"llama-server exited before readiness (code {return_code})",
                    )
            return
        if _health_is_ready():
            with _lock:
                if generation == _generation and _process is process:
                    _set_snapshot_locked(
                        "ready",
                        _snapshot["model_id"],
                        _snapshot["worker_ids"],
                        None,
                    )
            return
        time.sleep(HEALTH_POLL_INTERVAL_SECONDS)

    _terminate_process(process)
    with _lock:
        if generation == _generation and _process is process:
            _process = None
            _set_snapshot_locked(
                "error",
                _snapshot["model_id"],
                _snapshot["worker_ids"],
                "llama-server did not become healthy before the startup timeout",
            )


def _health_is_ready() -> bool:
    try:
        with urllib.request.urlopen(
            f"http://{SERVER_HOST}:{SERVER_PORT}/health", timeout=1.0
        ) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return False
    return isinstance(payload, dict) and payload.get("status") in {"ok", "healthy"}


def _drain_output(process: subprocess.Popen[str]) -> None:
    stream = process.stdout
    if stream is None:
        return
    try:
        for line in stream:
            clean = line.rstrip("\r\n")
            if clean:
                with _lock:
                    _diagnostics.append(clean[:2000])
    finally:
        stream.close()


def _terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass


def _refresh_process_state_locked() -> None:
    global _process

    if _process is None or _snapshot["status"] not in {"starting", "ready"}:
        return
    return_code = _process.poll()
    if return_code is None:
        return
    _process = None
    _set_snapshot_locked(
        "error",
        _snapshot["model_id"],
        _snapshot["worker_ids"],
        f"llama-server exited unexpectedly (code {return_code})",
    )


def _set_snapshot_locked(
    state: str,
    model_id: str | None,
    worker_ids: list[str],
    error: str | None,
) -> None:
    _snapshot.update(
        status=state,
        model_id=model_id,
        worker_ids=list(worker_ids),
        error=error,
    )


def _snapshot_copy_locked() -> dict[str, Any]:
    return {
        "status": _snapshot["status"],
        "model_id": _snapshot["model_id"],
        "worker_ids": list(_snapshot["worker_ids"]),
        "error": _snapshot["error"],
    }


def _safe_error(error: Exception) -> str:
    if isinstance(error, LauncherError):
        return str(error)
    if isinstance(error, OSError):
        return "The llama-server process could not be started"
    return "Model startup failed"


def _child_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)) | int(
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def _probe_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


atexit.register(stop)
