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
        self.real_port_check = launcher._ensure_server_port_available  # kept for the one test that binds a real socket
        self.real_master_probe = launcher._probe_master_runtime
        self.real_group_probe = launcher._probe_rpc_group
        self.patches = [
            mock.patch.object(launcher, "_ensure_server_port_available"),  # never depend on 8080 being free on this laptop
            mock.patch.object(launcher, "_probe_master_runtime"),
            mock.patch.object(launcher, "_probe_rpc_group"),
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

    def _start_ok(self, selection, health=True):  # start with probe/health/Popen faked; returns (process, popen mock)
        process = _FakeProcess()
        patches = [
            mock.patch.object(launcher, "_probe_rpc_endpoint"),
            mock.patch.object(launcher, "_health_is_ready", return_value=health),
            mock.patch.object(launcher.subprocess, "Popen", return_value=process),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)
        launcher.start("test-model", selection)
        return process, mocks[2]

    def test_missing_binary_fails_closed_without_probing(self) -> None:
        self.binary.unlink()
        with (
            mock.patch.object(launcher, "_probe_rpc_endpoint") as probe,
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(launcher.LauncherConfigurationError, "binary is missing"):
                launcher.start("test-model", _Selection("100.64.0.2:50052"))
        probe.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(launcher.status()["status"], "error")

    def test_non_text_model_id_is_rejected(self) -> None:
        for bad in (None, 5, ["test-model"]):
            with self.subTest(model_id=bad):
                with self.assertRaises(launcher.LauncherConfigurationError):
                    launcher.start(bad, _Selection("100.64.0.2:50052"))
                launcher.stop()

    def test_duplicate_endpoints_are_rejected_before_any_probe(self) -> None:
        selection = _Selection("100.64.0.2:50052,100.64.0.2:50052", ("node-a", "node-b"))
        with (
            mock.patch.object(launcher, "_probe_rpc_endpoint") as probe,
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(launcher.LauncherSelectionError, "duplicate"):
                launcher.start("test-model", selection)
        probe.assert_not_called()
        popen.assert_not_called()

    def test_malformed_selection_shapes_are_rejected(self) -> None:
        cases = {
            "no rpc_endpoints": SimpleNamespace(candidates=()),
            "non-text endpoints": SimpleNamespace(rpc_endpoints=lambda: ["100.64.0.2:50052"], candidates=()),
            "no candidates": SimpleNamespace(rpc_endpoints=lambda: "100.64.0.2:50052"),
            "ids do not match endpoints": _Selection("100.64.0.2:50052,100.64.0.3:50052", ("node-a",)),
            "blank node id": _Selection("100.64.0.2:50052", (" ",)),
            "candidate without node_id": SimpleNamespace(rpc_endpoints=lambda: "100.64.0.2:50052", candidates=(object(),)),
        }
        for label, selection in cases.items():
            with self.subTest(label):
                with mock.patch.object(launcher.subprocess, "Popen") as popen:
                    with self.assertRaises(launcher.LauncherSelectionError):
                        launcher.start("test-model", selection)
                popen.assert_not_called()
                launcher.stop()

    def test_invalid_endpoint_formats_are_rejected(self) -> None:
        for endpoint in (
            "100.64.0.2",  # no port
            "100.64.0.2:0",
            "100.64.0.2:70000",
            "100.64.0.2:050052",  # leading zero would hide a different port
            "100.64.0.2:port",
            "fd7a:115c:a1e0::5:50052",  # IPv6 must be bracketed
            "[fd7a:115c:a1e0::5",
            "[fd7a:115c:a1e0::5]50052",
            ":50052",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(launcher.RpcEndpointError):
                    launcher._parse_endpoint(endpoint)

    def test_private_lan_and_public_addresses_are_not_tailscale(self) -> None:
        for endpoint in (
            "10.0.0.5:50052",
            "192.168.1.20:50052",
            "172.16.0.9:50052",
            "100.63.255.255:50052",  # just below 100.64.0.0/10
            "100.128.0.1:50052",  # just above it
            "[2001:4860:4860::8888]:50052",
            "[::1]:50052",  # loopback without the opt-in
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaisesRegex(launcher.RpcEndpointError, "Tailscale"):
                    launcher._parse_endpoint(endpoint)

    def test_tailscale_ipv4_and_bracketed_ipv6_are_accepted(self) -> None:
        self.assertEqual(launcher._parse_endpoint("100.127.255.254:50052"), ("100.127.255.254", 50052))
        self.assertEqual(launcher._parse_endpoint("[fd7a:115c:a1e0::5]:50052"), ("fd7a:115c:a1e0::5", 50052))

    def test_loopback_opt_in_must_be_exactly_one(self) -> None:
        for value in ("true", "yes", "0", ""):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"LOWLLAMA_ALLOW_LOOPBACK_RPC": value}):
                    with self.assertRaises(launcher.RpcEndpointError):
                        launcher._parse_endpoint("127.0.0.1:50052")

    def test_unreachable_tcp_skips_the_llama_probe(self) -> None:
        with (
            mock.patch.object(launcher.socket, "create_connection", side_effect=ConnectionRefusedError()),
            mock.patch.object(launcher.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(launcher.RpcEndpointError, "unreachable"):
                launcher._probe_rpc_endpoint("100.64.0.2:50052", self.binary)
        run.assert_not_called()

    def test_tcp_open_but_probe_times_out_or_cannot_run(self) -> None:  # TCP-only false positive guard
        for error in (subprocess.TimeoutExpired("llama-server", 15), OSError("cannot execute")):
            with self.subTest(error=type(error).__name__):
                with (
                    mock.patch.object(launcher.socket, "create_connection"),
                    mock.patch.object(launcher.subprocess, "run", side_effect=error) as run,
                ):
                    with self.assertRaisesRegex(launcher.RpcEndpointError, "probe failed"):
                        launcher._probe_rpc_endpoint("100.64.0.2:50052", self.binary)
                self.assertEqual(run.call_args.kwargs["timeout"], launcher.RPC_COMPATIBILITY_TIMEOUT_SECONDS)

    def test_probe_nonzero_exit_is_rejected_even_if_rpc0_is_printed(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="RPC0: 100.64.0.2:50052", stderr="")
        with (
            mock.patch.object(launcher.socket, "create_connection"),
            mock.patch.object(launcher.subprocess, "run", return_value=completed),
        ):
            with self.assertRaisesRegex(launcher.RpcEndpointError, "not llama.cpp"):
                launcher._probe_rpc_endpoint("100.64.0.2:50052", self.binary)

    def test_probe_accepts_rpc0_on_stderr(self) -> None:  # llama.cpp may print the device list on stderr
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="  RPC0: RPC[100.64.0.2:50052]")
        with (
            mock.patch.object(launcher.socket, "create_connection"),
            mock.patch.object(launcher.subprocess, "run", return_value=completed),
        ):
            launcher._probe_rpc_endpoint("100.64.0.2:50052", self.binary)

    def test_second_worker_probe_failure_stops_the_whole_launch(self) -> None:
        selection = _Selection("100.64.0.2:50052,100.64.0.3:50052", ("node-a", "node-b"))
        def probe(endpoint, binary):
            if endpoint.startswith("100.64.0.3"):
                raise launcher.RpcEndpointError(f"RPC worker {endpoint} is unreachable")
        with (
            mock.patch.object(launcher, "_probe_rpc_endpoint", side_effect=probe) as probed,
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            with self.assertRaises(launcher.RpcEndpointError):
                launcher.start("test-model", selection)
        self.assertEqual(probed.call_count, 2)
        popen.assert_not_called()  # never starts with only node-a after node-b was asked for
        state = launcher.status()
        self.assertEqual((state["status"], state["worker_ids"]), ("error", []))
        self.assertIn("100.64.0.3", state["error"])

    def test_real_port_check_detects_an_occupied_port(self) -> None:
        import socket as _socket
        holder = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        self.addCleanup(holder.close)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        with mock.patch.object(launcher, "SERVER_PORT", holder.getsockname()[1]):
            with self.assertRaisesRegex(launcher.LauncherConfigurationError, "already in use"):
                self.real_port_check()
        with _socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            free_port = free.getsockname()[1]
        with mock.patch.object(launcher, "SERVER_PORT", free_port):
            self.real_port_check()  # a free port passes

    def test_command_keeps_rpc_order_and_one_device_per_endpoint(self) -> None:
        for endpoints, ids, devices, split in (
            ("100.64.0.9:50052", ("node-z",), "CUDA0,RPC0", "1,1"),
            ("100.64.0.9:50052,100.64.0.2:50052", ("node-z", "node-a"), "CUDA0,RPC0,RPC1", "1,1,1"),
        ):
            with self.subTest(devices=devices):
                process, popen = self._start_ok(_Selection(endpoints, ids))
                command = popen.call_args.args[0]
                self.assertEqual(command[command.index("--rpc") + 1], endpoints)  # selection order = RPC0, RPC1
                self.assertEqual(command[command.index("--device") + 1], devices)
                self.assertEqual(command[command.index("--tensor-split") + 1], split)
                self.assertEqual(command[command.index("-m") + 1], str(self.model.resolve()))
                self.assertEqual(command[command.index("--alias") + 1], "test-model")
                self.assertEqual(
                    (command[command.index("--host") + 1], command[command.index("--port") + 1]),
                    ("127.0.0.1", "8080"),
                )
                self.assertEqual(launcher.status()["worker_ids"], list(ids))
                launcher.stop()

    def test_real_candidate_selection_with_a_rejected_worker_only_claims_the_survivor(self) -> None:
        from coordinator import workers
        survivor = workers.WorkerEndpoint("node-a", "A", "100.64.0.2", 50052, 16.0, 8.0, True, "online", 1.0)
        selection = workers.CandidateSelection(
            (survivor,), (workers.RejectedWorker("node-b", "B", workers.RPC_NOT_READY),)
        )
        process, popen = self._start_ok(selection)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--rpc") + 1], "100.64.0.2:50052")
        self.assertEqual(command[command.index("--device") + 1], "CUDA0,RPC0")  # no RPC1 for the rejected node
        self.assertEqual(launcher.status()["worker_ids"], ["node-a"])  # refusing a partial selection is /start's job, not the launcher's

    def test_master_device_is_hardcoded_cuda0(self) -> None:  # current contract; a CPU fallback needs Itmam's agreement first
        process, popen = self._start_ok(_Selection("100.64.0.2:50052"))
        command = popen.call_args.args[0]
        self.assertTrue(command[command.index("--device") + 1].startswith("CUDA0,"))

    def test_master_runtime_requires_pinned_build_and_cuda0(self) -> None:
        version = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="version 0.9.0 (build 12110, commit cd959bae9)", stderr=""
        )
        devices = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="Available devices:\n  CUDA0: RTX 4060", stderr=""
        )
        with mock.patch.object(launcher.subprocess, "run", side_effect=[version, devices]) as run:
            self.real_master_probe(self.binary)
        self.assertEqual(run.call_args_list[0].args[0], [str(self.binary), "--version"])
        self.assertEqual(run.call_args_list[1].args[0], [str(self.binary), "--list-devices"])

        wrong_build = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="version 0.9.0 (build 12000)", stderr=""
        )
        with mock.patch.object(launcher.subprocess, "run", return_value=wrong_build):
            with self.assertRaisesRegex(launcher.LauncherConfigurationError, "pinned build 12110"):
                self.real_master_probe(self.binary)

    def test_missing_cuda_fails_before_worker_probe_or_popen(self) -> None:
        selection = _Selection("100.64.0.2:50052")
        with (
            mock.patch.object(
                launcher,
                "_probe_master_runtime",
                side_effect=launcher.LauncherConfigurationError("Master CUDA0 device is unavailable"),
            ),
            mock.patch.object(launcher, "PINNED_SERVER_BINARY", self.binary),
            mock.patch.object(launcher, "_probe_rpc_endpoint") as rpc_probe,
            mock.patch.object(launcher.subprocess, "Popen") as popen,
        ):
            with self.assertRaisesRegex(launcher.LauncherConfigurationError, "CUDA0"):
                launcher.start("test-model", selection)
        rpc_probe.assert_not_called()
        popen.assert_not_called()

    def test_combined_rpc_probe_requires_every_ordered_device(self) -> None:
        endpoints = ["100.64.0.9:50052", "100.64.0.2:50052"]
        complete = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="RPC0: first\nRPC1: second", stderr=""
        )
        with mock.patch.object(launcher.subprocess, "run", return_value=complete) as run:
            self.real_group_probe(endpoints, self.binary)
        self.assertEqual(
            run.call_args.args[0],
            [str(self.binary), "--rpc", ",".join(endpoints), "--list-devices"],
        )

        incomplete = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="RPC0: first", stderr=""
        )
        with mock.patch.object(launcher.subprocess, "run", return_value=incomplete):
            with self.assertRaisesRegex(launcher.RpcEndpointError, "RPC1"):
                self.real_group_probe(endpoints, self.binary)

    def test_startup_timeout_terminates_child_and_reports_error(self) -> None:
        process, _ = self._start_ok(_Selection("100.64.0.2:50052"), health=False)
        failed = self._wait_for_state("error")
        self.assertIn("startup timeout", failed["error"])
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(failed["model_id"], "test-model")

    def test_death_after_ready_is_reported_by_status(self) -> None:
        process, _ = self._start_ok(_Selection("100.64.0.2:50052"))
        self._wait_for_state("ready")
        process.return_code = 3
        state = launcher.status()
        self.assertEqual(state["status"], "error")
        self.assertIn("exited unexpectedly (code 3)", state["error"])
        self.assertIsNone(launcher._process)

    def test_new_start_is_allowed_after_an_error(self) -> None:
        process, popen = self._start_ok(_Selection("100.64.0.2:50052"))
        self._wait_for_state("ready")
        process.return_code = 3
        self.assertEqual(launcher.status()["status"], "error")
        popen.return_value = _FakeProcess()  # a fresh child, like a real second Popen
        second = launcher.start("test-model", _Selection("100.64.0.3:50052", ("node-2",)))
        self.assertIn(second["status"], {"starting", "ready"})
        self.assertEqual(second["worker_ids"], ["node-2"])

    def test_busy_start_leaves_the_running_child_alone(self) -> None:
        process, popen = self._start_ok(_Selection("100.64.0.2:50052"), health=False)
        with self.assertRaises(launcher.LauncherBusyError):
            launcher.start("test-model", _Selection("100.64.0.3:50052", ("node-2",)))
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(process.terminate_calls, 0)
        self.assertEqual(launcher.status()["worker_ids"], ["node-1"])

    def test_stop_during_starting_wins_over_the_readiness_monitor(self) -> None:
        process, _ = self._start_ok(_Selection("100.64.0.2:50052"), health=False)
        self.assertEqual(launcher.status()["status"], "starting")
        stopped = launcher.stop()
        self.assertEqual(stopped, {"status": "stopped", "model_id": None, "worker_ids": [], "error": None})
        self.assertEqual(process.terminate_calls, 1)
        time.sleep(0.15)  # longer than the patched startup timeout
        self.assertEqual(launcher.status()["status"], "stopped")  # the old monitor must not flip it to error

    def test_stop_kills_a_child_that_ignores_terminate(self) -> None:
        process, _ = self._start_ok(_Selection("100.64.0.2:50052"))
        self._wait_for_state("ready")
        process.terminate = lambda: None  # stubborn child
        with mock.patch.object(launcher, "STOP_TIMEOUT_SECONDS", 0.01):
            launcher.stop()
        self.assertEqual(process.kill_calls, 1)

    def test_stop_without_a_process_is_safe(self) -> None:
        self.assertEqual(launcher.stop()["status"], "stopped")
        self.assertEqual(launcher.stop()["status"], "stopped")

    def test_unexpected_errors_are_not_leaked(self) -> None:
        for error, message in (
            (OSError(r"C:\secret\path\llama-server.exe"), "could not be started"),
            (RuntimeError("token=abc123"), "Model startup failed"),
        ):
            with self.subTest(error=type(error).__name__):
                with (
                    mock.patch.object(launcher, "_probe_rpc_endpoint"),
                    mock.patch.object(launcher.subprocess, "Popen", side_effect=error),
                ):
                    with self.assertRaises(launcher.LauncherError) as caught:
                        launcher.start("test-model", _Selection("100.64.0.2:50052"))
                state = launcher.status()
                self.assertIn(message, state["error"])
                self.assertIn(message, str(caught.exception))
                self.assertNotIn("secret", state["error"])
                self.assertNotIn("abc123", state["error"])
                launcher.stop()

    def test_status_returns_a_copy(self) -> None:
        self._start_ok(_Selection("100.64.0.2:50052"))
        state = launcher.status()
        state["worker_ids"].append("intruder")
        state["status"] = "hacked"
        self.assertNotIn("intruder", launcher.status()["worker_ids"])
        self.assertNotEqual(launcher.status()["status"], "hacked")

    def test_output_is_drained_into_bounded_diagnostics(self) -> None:
        process = _FakeProcess()
        process.stdout = io.StringIO("".join(f"line {i}\n" for i in range(launcher.MAX_DIAGNOSTIC_LINES + 50)))
        log_directory = Path(self.temp.name) / "logs"
        with mock.patch.object(launcher, "LAUNCH_LOG_DIRECTORY", log_directory):
            launcher._drain_output(process)
        self.assertEqual(len(launcher._diagnostics), launcher.MAX_DIAGNOSTIC_LINES)
        self.assertEqual(launcher._diagnostics[-1], f"line {launcher.MAX_DIAGNOSTIC_LINES + 49}")
        self.assertTrue(process.stdout.closed)
        logs = list(log_directory.glob("api-launcher-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertEqual(len(logs[0].read_text(encoding="utf-8").splitlines()), launcher.MAX_DIAGNOSTIC_LINES + 50)

    def test_log_write_failure_does_not_stop_output_drain(self) -> None:
        class BrokenLog:
            def write(self, _line):
                raise OSError("disk unavailable")

            def close(self):
                return None

        process = _FakeProcess()
        process.stdout = io.StringIO("first\nsecond\n")
        with mock.patch.object(launcher, "_open_launch_log", return_value=BrokenLog()):
            launcher._drain_output(process)
        self.assertEqual(list(launcher._diagnostics)[-2:], ["first", "second"])
        self.assertTrue(process.stdout.closed)

    def test_health_check_requires_ok_json(self) -> None:
        def response(status, body):
            r = mock.MagicMock(status=status)
            r.read.return_value = body
            r.__enter__.return_value = r
            return r
        for status, body, expected in (
            (200, b'{"status":"ok"}', True),
            (200, b'{"status":"healthy"}', True),
            (200, b'{"status":"loading model"}', False),
            (503, b'{"status":"ok"}', False),
            (200, b"not json", False),
            (200, b"[]", False),
        ):
            with self.subTest(body=body, status=status):
                with mock.patch.object(launcher.urllib.request, "urlopen", return_value=response(status, body)):
                    self.assertIs(launcher._health_is_ready(), expected)
        with mock.patch.object(launcher.urllib.request, "urlopen", side_effect=ConnectionRefusedError()):
            self.assertFalse(launcher._health_is_ready())

    def tearDown(self) -> None:
        launcher.stop()
        self.assertIsNone(launcher._process)  # no child left behind for the next test


class LauncherCatalogTests(unittest.TestCase):
    """Reads the REAL module constants (no patching): the shared demo-model contract."""

    DEMO_MODEL_ID = "qwen2.5-0.5b-instruct-q4_k_m"

    def setUp(self) -> None:
        self.launcher = importlib.import_module("coordinator.launcher")

    def test_demo_model_maps_to_the_canonical_runtime_path(self) -> None:
        expected = PROJECT_ROOT / ".runtime" / "models" / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
        self.assertIn(self.DEMO_MODEL_ID, self.launcher.MODEL_ALLOWLIST)
        self.assertEqual(Path(self.launcher.MODEL_ALLOWLIST[self.DEMO_MODEL_ID]).resolve(), expected.resolve())

    def test_pinned_binary_and_local_only_server(self) -> None:
        self.assertEqual(self.launcher.SERVER_BINARY.parent.name, "llama-b12110-cuda")
        self.assertEqual((self.launcher.SERVER_HOST, self.launcher.SERVER_PORT), ("127.0.0.1", 8080))

    def test_every_catalog_path_stays_inside_the_project(self) -> None:
        root = PROJECT_ROOT.resolve()
        for model_id, path in self.launcher.MODEL_ALLOWLIST.items():
            with self.subTest(model_id=model_id):
                self.assertTrue(Path(path).resolve().is_relative_to(root))
                self.assertEqual(Path(path).suffix, ".gguf")


if __name__ == "__main__":
    unittest.main(verbosity=2)
