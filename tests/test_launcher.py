"""Unit tests for the owned llama.cpp launcher lifecycle."""

from __future__ import annotations

import io
import importlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

launcher = None


class _Selection:
    def __init__(self, endpoints: str, node_ids: tuple[str, ...] = ("node-1",)):
        self._endpoints = endpoints
        self.candidates = tuple(SimpleNamespace(node_id=node_id) for node_id in node_ids)
        self.calls = 0

    def rpc_endpoints(self) -> str:
        self.calls += 1
        return self._endpoints


class _FakeProcess:
    def __init__(self) -> None:
        self.return_code: int | None = None
        self.stdout = io.StringIO("startup line\n")
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.return_code = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.return_code = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.return_code is None:
            raise subprocess.TimeoutExpired("llama-server", timeout)
        return self.return_code


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        global launcher
        launcher = importlib.import_module("coordinator.launcher")
        launcher.stop()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.binary = root / "llama-server.exe"
        self.model = root / "model.gguf"
        self.binary.write_bytes(b"binary")
        self.model.write_bytes(b"model")
        self.patches = [
            mock.patch.object(launcher, "SERVER_BINARY", self.binary),
            mock.patch.object(launcher, "MODEL_ALLOWLIST", {"test-model": self.model}),
            mock.patch.object(launcher, "HEALTH_POLL_INTERVAL_SECONDS", 0.001),
            mock.patch.object(launcher, "STARTUP_TIMEOUT_SECONDS", 0.1),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(launcher.stop)

    @staticmethod
    def _wait_for_state(expected: str) -> dict[str, object]:
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            state = launcher.status()
            if state["status"] == expected:
                return state
            time.sleep(0.005)
        raise AssertionError(f"launcher did not reach {expected}: {launcher.status()}")

    def test_start_uses_selection_probes_each_endpoint_and_becomes_ready(self) -> None:
        selection = _Selection(
            "100.64.0.2:50052,100.64.0.3:50053",
            ("node-a", "node-b"),
        )
        process = _FakeProcess()
        with (
            mock.patch.object(launcher, "_probe_rpc_endpoint") as probe,
            mock.patch.object(launcher, "_health_is_ready", return_value=True),
            mock.patch.object(launcher.subprocess, "Popen", return_value=process) as popen,
        ):
            started = launcher.start("test-model", selection)
            ready = self._wait_for_state("ready")

        self.assertIn(started["status"], {"starting", "ready"})
        self.assertEqual(selection.calls, 1)
        self.assertEqual(
            probe.call_args_list,
            [
                mock.call("100.64.0.2:50052", self.binary.resolve()),
                mock.call("100.64.0.3:50053", self.binary.resolve()),
            ],
        )
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--rpc") + 1], selection._endpoints)
        self.assertEqual(command[command.index("--device") + 1], "CUDA0,RPC0,RPC1")
        self.assertEqual(command[command.index("--tensor-split") + 1], "1,1,1")
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(ready["worker_ids"], ["node-a", "node-b"])
        self.assertIsNone(ready["error"])

    def test_empty_selection_is_rejected_without_starting_process(self) -> None:
        selection = _Selection("", ())
        with mock.patch.object(launcher.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(launcher.LauncherSelectionError, "No usable"):
                launcher.start("test-model", selection)
        popen.assert_not_called()
        self.assertEqual(launcher.status()["status"], "error")

    def test_duplicate_start_is_rejected(self) -> None:
        process = _FakeProcess()
        selection = _Selection("100.64.0.2:50052")
        with (
            mock.patch.object(launcher, "_probe_rpc_endpoint"),
            mock.patch.object(launcher, "_health_is_ready", return_value=False),
            mock.patch.object(launcher.subprocess, "Popen", return_value=process),
        ):
            launcher.start("test-model", selection)
            with self.assertRaises(launcher.LauncherBusyError):
                launcher.start("test-model", selection)

    def test_probe_failure_prevents_process_creation(self) -> None:
        selection = _Selection("100.64.0.2:50052")
        with (
            mock.patch.object(
                launcher,
                "_probe_rpc_endpoint",
                side_effect=launcher.RpcEndpointError("worker is incompatible"),
            ),
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            with self.assertRaises(launcher.RpcEndpointError):
                launcher.start("test-model", selection)
        popen.assert_not_called()
        self.assertEqual(launcher.status()["error"], "worker is incompatible")

    def test_occupied_server_port_prevents_process_creation(self) -> None:
        selection = _Selection("100.64.0.2:50052")
        with (
            mock.patch.object(launcher, "_probe_rpc_endpoint"),
            mock.patch.object(
                launcher,
                "_ensure_server_port_available",
                side_effect=launcher.LauncherConfigurationError(
                    "Local inference port 8080 is already in use"
                ),
            ),
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(launcher.LauncherConfigurationError, "in use"):
                launcher.start("test-model", selection)
        popen.assert_not_called()

    def test_endpoint_policy_rejects_public_and_hostname_addresses(self) -> None:
        for endpoint in ("8.8.8.8:50052", "helper.example:50052", "127.0.0.1:50052"):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(launcher.RpcEndpointError):
                    launcher._parse_endpoint(endpoint)

    def test_loopback_requires_explicit_diagnostic_opt_in(self) -> None:
        with mock.patch.dict(os.environ, {"LOWLLAMA_ALLOW_LOOPBACK_RPC": "1"}):
            self.assertEqual(
                launcher._parse_endpoint("127.0.0.1:50052"),
                ("127.0.0.1", 50052),
            )

    def test_probe_checks_tcp_and_llama_rpc_compatibility(self) -> None:
        connection = mock.MagicMock()
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="RPC0: 100.64.0.2:50052", stderr=""
        )
        with (
            mock.patch.object(launcher.socket, "create_connection", return_value=connection) as connect,
            mock.patch.object(launcher.subprocess, "run", return_value=completed) as run,
        ):
            launcher._probe_rpc_endpoint("100.64.0.2:50052", self.binary)

        connect.assert_called_once_with(
            ("100.64.0.2", 50052), timeout=launcher.RPC_CONNECT_TIMEOUT_SECONDS
        )
        connection.close.assert_called_once_with()
        self.assertEqual(
            run.call_args.args[0],
            [str(self.binary), "--rpc", "100.64.0.2:50052", "--list-devices"],
        )
        self.assertFalse(run.call_args.kwargs["shell"])

    def test_probe_rejects_tcp_listener_that_is_not_llama_rpc(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Available devices: CUDA0", stderr=""
        )
        with (
            mock.patch.object(launcher.socket, "create_connection") as connect,
            mock.patch.object(launcher.subprocess, "run", return_value=completed),
        ):
            connect.return_value.close.return_value = None
            with self.assertRaisesRegex(launcher.RpcEndpointError, "not llama.cpp"):
                launcher._probe_rpc_endpoint("100.64.0.2:50052", self.binary)

    def test_stop_terminates_only_owned_process_and_is_idempotent(self) -> None:
        process = _FakeProcess()
        with (
            mock.patch.object(launcher, "_probe_rpc_endpoint"),
            mock.patch.object(launcher, "_health_is_ready", return_value=True),
            mock.patch.object(launcher.subprocess, "Popen", return_value=process),
        ):
            launcher.start("test-model", _Selection("100.64.0.2:50052"))
            self._wait_for_state("ready")
            stopped = launcher.stop()
            stopped_again = launcher.stop()

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped_again["status"], "stopped")

    def test_process_crash_changes_status_to_error(self) -> None:
        process = _FakeProcess()
        with (
            mock.patch.object(launcher, "_probe_rpc_endpoint"),
            mock.patch.object(launcher, "_health_is_ready", return_value=False),
            mock.patch.object(launcher.subprocess, "Popen", return_value=process),
        ):
            launcher.start("test-model", _Selection("100.64.0.2:50052"))
            process.return_code = 7
            failed = self._wait_for_state("error")

        self.assertIn("code 7", str(failed["error"]))

    def test_missing_binary_or_model_and_unknown_model_fail_closed(self) -> None:
        selection = _Selection("100.64.0.2:50052")
        with self.assertRaisesRegex(launcher.LauncherConfigurationError, "allowlisted"):
            launcher.start("../../evil.gguf", selection)
        launcher.stop()
        self.model.unlink()
        with self.assertRaisesRegex(launcher.LauncherConfigurationError, "model file"):
            launcher.start("test-model", selection)


if __name__ == "__main__":
    unittest.main(verbosity=2)
