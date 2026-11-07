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
        environment = {
            "LOWLLAMA_COORDINATOR_URL": "https://coordinator.example.test/api/",
            "LOWLLAMA_NODE_ID": "helper-01",
            "LOWLLAMA_NODE_NAME": "Demo Helper",
            "LOWLLAMA_HEARTBEAT_INTERVAL_SECONDS": "15",
            "LOWLLAMA_REQUEST_TIMEOUT_SECONDS": "2.5",
            "LOWLLAMA_RECONNECTION_INTERVAL_SECONDS": "3",
            "LOWLLAMA_AUTH_TOKEN": "test-token-from-environment",
        }

        config = load_config(environment=environment)

        self.assertEqual(config.coordinator_url, "https://coordinator.example.test/api")
        self.assertEqual(config.node_id, "helper-01")
        self.assertEqual(config.node_display_name, "Demo Helper")
        self.assertEqual(config.heartbeat_interval_seconds, 15.0)
        self.assertEqual(config.request_timeout_seconds, 2.5)
        self.assertEqual(config.reconnection_interval_seconds, 3.0)
        self.assertEqual(config.auth_token, "test-token-from-environment")
        self.assertNotIn("test-token-from-environment", repr(config))

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
        self.assertIsNone(config.auth_token)

    def test_generates_and_reuses_persistent_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            first = load_config(environment={}, state_dir=state_dir)
            second = load_config(environment={}, state_dir=state_dir)
            persisted_value = (state_dir / "node_id").read_text(encoding="utf-8").strip()

        parsed_id = uuid.UUID(first.node_id)
        self.assertEqual(parsed_id.version, 4)
        self.assertEqual(second.node_id, first.node_id)
        self.assertEqual(persisted_value, first.node_id)

    def test_never_persists_authentication_token(self) -> None:
        token = "credential-that-must-stay-in-memory"
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            config = load_config(
                environment={"LOWLLAMA_AUTH_TOKEN": token},
                state_dir=state_dir,
            )
            persisted_value = (state_dir / "node_id").read_text(encoding="utf-8")

        self.assertEqual(config.auth_token, token)
        self.assertNotIn(token, persisted_value)
        self.assertEqual(persisted_value.strip(), config.node_id)

    def test_environment_node_id_does_not_write_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            config = load_config(
                environment={"LOWLLAMA_NODE_ID": "team-assigned-id"},
                state_dir=state_dir,
            )

            self.assertEqual(config.node_id, "team-assigned-id")
            self.assertFalse((state_dir / "node_id").exists())

    def test_rejects_invalid_persisted_node_id(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            state_dir = Path(temporary_directory)
            (state_dir / "node_id").write_text("invalid node id\n", encoding="utf-8")

            with self.assertRaises(ConfigurationError):
                load_config(environment={}, state_dir=state_dir)

    def test_rejects_invalid_environment_values(self) -> None:
        invalid_values = {
            "LOWLLAMA_COORDINATOR_URL": "ftp://example.test",
            "LOWLLAMA_NODE_ID": "invalid node id",
            "LOWLLAMA_NODE_NAME": "   ",
            "LOWLLAMA_HEARTBEAT_INTERVAL_SECONDS": "0",
            "LOWLLAMA_REQUEST_TIMEOUT_SECONDS": "nan",
            "LOWLLAMA_RECONNECTION_INTERVAL_SECONDS": "3601",
            "LOWLLAMA_AUTH_TOKEN": "   ",
        }

        for name, invalid_value in invalid_values.items():
            with self.subTest(name=name):
                environment = {"LOWLLAMA_NODE_ID": "valid-node"}
                environment[name] = invalid_value
                with self.assertRaises(ConfigurationError):
                    load_config(environment=environment)

    def test_rejects_url_with_embedded_credentials(self) -> None:
        environment = {
            "LOWLLAMA_NODE_ID": "valid-node",
            "LOWLLAMA_COORDINATOR_URL": "https://user:password@example.test",
        }

        with self.assertRaises(ConfigurationError):
            load_config(environment=environment)

    def test_rejects_unprotected_remote_http_coordinator(self) -> None:
        coordinator_urls = (
            "http://coordinator.example.test:8000",
            "http://192.168.1.20:8000",
        )

        for coordinator_url in coordinator_urls:
            with self.subTest(coordinator_url=coordinator_url):
                with self.assertRaises(ConfigurationError):
                    load_config(
                        environment={
                            "LOWLLAMA_NODE_ID": "valid-node",
                            "LOWLLAMA_COORDINATOR_URL": coordinator_url,
                        }
                    )

    def test_allows_tailscale_http_addresses(self) -> None:
        coordinator_urls = (
            "http://100.100.20.30:8000",
            "http://[fd7a:115c:a1e0::1234]:8000",
        )

        for coordinator_url in coordinator_urls:
            with self.subTest(coordinator_url=coordinator_url):
                config = load_config(
                    environment={
                        "LOWLLAMA_NODE_ID": "valid-node",
                        "LOWLLAMA_COORDINATOR_URL": coordinator_url,
                    }
                )
                self.assertEqual(config.coordinator_url, coordinator_url)


if __name__ == "__main__":
    unittest.main()
