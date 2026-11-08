"""Unit tests for helper-agent configuration."""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

# Allow this file to be run directly as well as through unittest discovery.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.config import ConfigurationError, load_config


class LoadConfigTests(unittest.TestCase):
    """Verify configuration loading, persistence, and validation."""

    def test_loads_valid_environment_configuration(self) -> None:
        invite = "invite-that-must-stay-secret"
        with tempfile.TemporaryDirectory() as temporary_directory:
            environment = {
                "LOWLLAMA_COORDINATOR_URL": "https://coordinator.example.test/api/",
                "LOWLLAMA_CLIENT_ID": "helper-installation-01",
                "LOWLLAMA_NODE_NAME": "Demo Helper",
                "LOWLLAMA_INVITE_CODE": invite,
                "LOWLLAMA_HEARTBEAT_INTERVAL_SECONDS": "15",
                "LOWLLAMA_REQUEST_TIMEOUT_SECONDS": "2.5",
                "LOWLLAMA_RECONNECTION_INTERVAL_SECONDS": "3",
                "LOWLLAMA_RPC_HOST": "100.100.20.31",
                "LOWLLAMA_RPC_PORT": "50053",
                "LOWLLAMA_RPC_PROBE_TIMEOUT_SECONDS": "0.25",
            }

            config = load_config(
                environment=environment,
                state_dir=temporary_directory,
            )

        self.assertEqual(config.coordinator_url, "https://coordinator.example.test/api")
        self.assertEqual(config.client_id, "helper-installation-01")
        self.assertEqual(config.node_display_name, "Demo Helper")
        self.assertEqual(config.heartbeat_interval_seconds, 15.0)
        self.assertEqual(config.request_timeout_seconds, 2.5)
        self.assertEqual(config.reconnection_interval_seconds, 3.0)
        self.assertEqual(config.rpc_host, "100.100.20.31")
        self.assertEqual(config.rpc_port, 50053)
        self.assertEqual(config.rpc_probe_timeout_seconds, 0.25)
        self.assertEqual(config.invite_code, invite)
        self.assertNotIn(invite, repr(config))
        self.assertEqual(config.state_dir, Path(temporary_directory))

    def test_uses_safe_development_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch("agent.config.socket.gethostname", return_value="helper-host"):
                config = load_config(
                    environment={},
                    state_dir=temporary_directory,
                )

        self.assertEqual(config.coordinator_url, "http://127.0.0.1:8000")
        self.assertEqual(config.node_display_name, "helper-host")
        self.assertEqual(config.heartbeat_interval_seconds, 5.0)
        self.assertEqual(config.request_timeout_seconds, 10.0)
        self.assertEqual(config.reconnection_interval_seconds, 5.0)
        self.assertEqual(config.rpc_host, "127.0.0.1")
        self.assertEqual(config.rpc_port, 50052)
        self.assertEqual(config.rpc_probe_timeout_seconds, 0.5)
        self.assertIsNone(config.invite_code)

    def test_generates_and_reuses_persistent_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            first = load_config(environment={}, state_dir=state_dir)
            second = load_config(environment={}, state_dir=state_dir)
            persisted_value = (state_dir / "client_id").read_text(encoding="utf-8").strip()

        parsed_id = uuid.UUID(first.client_id)
        self.assertEqual(parsed_id.version, 4)
        self.assertEqual(second.client_id, first.client_id)
        self.assertEqual(persisted_value, first.client_id)

    def test_never_persists_invite_code(self) -> None:
        invite = "invite-that-must-stay-in-memory"
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            config = load_config(
                environment={"LOWLLAMA_INVITE_CODE": invite},
                state_dir=state_dir,
            )
            persisted_value = (state_dir / "client_id").read_text(encoding="utf-8")

        self.assertEqual(config.invite_code, invite)
        self.assertNotIn(invite, persisted_value)
        self.assertEqual(persisted_value.strip(), config.client_id)

    def test_environment_client_id_does_not_write_local_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            config = load_config(
                environment={"LOWLLAMA_CLIENT_ID": "team-assigned-id"},
                state_dir=state_dir,
            )

            self.assertEqual(config.client_id, "team-assigned-id")
            self.assertFalse((state_dir / "client_id").exists())

    def test_migrates_legacy_local_node_id_to_client_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            (state_dir / "node_id").write_text("legacy-local-id\n", encoding="utf-8")

            config = load_config(environment={}, state_dir=state_dir)

            self.assertEqual(config.client_id, "legacy-local-id")
            self.assertEqual(
                (state_dir / "client_id").read_text(encoding="utf-8").strip(),
                "legacy-local-id",
            )

    def test_rejects_invalid_persisted_client_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            (state_dir / "client_id").write_text("invalid client id\n", encoding="utf-8")

            with self.assertRaises(ConfigurationError):
                load_config(environment={}, state_dir=state_dir)

    def test_rejects_invalid_environment_values(self) -> None:
        invalid_values = {
            "LOWLLAMA_COORDINATOR_URL": "ftp://example.test",
            "LOWLLAMA_CLIENT_ID": "invalid client id",
            "LOWLLAMA_NODE_NAME": "   ",
            "LOWLLAMA_HEARTBEAT_INTERVAL_SECONDS": "0",
            "LOWLLAMA_REQUEST_TIMEOUT_SECONDS": "nan",
            "LOWLLAMA_RECONNECTION_INTERVAL_SECONDS": "3601",
            "LOWLLAMA_INVITE_CODE": "   ",
            "LOWLLAMA_RPC_HOST": "192.168.1.20",
            "LOWLLAMA_RPC_PORT": "65536",
            "LOWLLAMA_RPC_PROBE_TIMEOUT_SECONDS": "0",
        }

        for name, invalid_value in invalid_values.items():
            with self.subTest(name=name):
                environment = {"LOWLLAMA_CLIENT_ID": "valid-client"}
                environment[name] = invalid_value
                with tempfile.TemporaryDirectory() as temporary_directory:
                    with self.assertRaises(ConfigurationError):
                        load_config(
                            environment=environment,
                            state_dir=temporary_directory,
                        )

    def test_rejects_ambiguous_legacy_node_id_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ConfigurationError):
                load_config(
                    environment={"LOWLLAMA_NODE_ID": "ambiguous-id"},
                    state_dir=temporary_directory,
                )

    def test_rejects_node_name_longer_than_server_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ConfigurationError):
                load_config(
                    environment={
                        "LOWLLAMA_CLIENT_ID": "valid-client",
                        "LOWLLAMA_NODE_NAME": "x" * 65,
                    },
                    state_dir=temporary_directory,
                )

    def test_rejects_url_with_embedded_credentials(self) -> None:
        environment = {
            "LOWLLAMA_CLIENT_ID": "valid-client",
            "LOWLLAMA_COORDINATOR_URL": "https://user:password@example.test",
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaises(ConfigurationError):
                load_config(environment=environment, state_dir=temporary_directory)

    def test_rejects_unprotected_remote_http_coordinator(self) -> None:
        coordinator_urls = (
            "http://coordinator.example.test:8000",
            "http://192.168.1.20:8000",
        )

        for coordinator_url in coordinator_urls:
            with self.subTest(coordinator_url=coordinator_url):
                with self.assertRaises(ConfigurationError):
                    with tempfile.TemporaryDirectory() as temporary_directory:
                        load_config(
                            environment={
                                "LOWLLAMA_CLIENT_ID": "valid-client",
                                "LOWLLAMA_COORDINATOR_URL": coordinator_url,
                            },
                            state_dir=temporary_directory,
                        )

    def test_allows_tailscale_http_addresses(self) -> None:
        coordinator_urls = (
            "http://100.100.20.30:8000",
            "http://[fd7a:115c:a1e0::1234]:8000",
        )

        for coordinator_url in coordinator_urls:
            with self.subTest(coordinator_url=coordinator_url):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    config = load_config(
                        environment={
                            "LOWLLAMA_CLIENT_ID": "valid-client",
                            "LOWLLAMA_COORDINATOR_URL": coordinator_url,
                        },
                        state_dir=temporary_directory,
                    )
                self.assertEqual(config.coordinator_url, coordinator_url)


if __name__ == "__main__":
    unittest.main()
