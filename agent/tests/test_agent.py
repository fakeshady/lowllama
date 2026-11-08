"""Unit tests for helper-agent orchestration."""

from __future__ import annotations

import logging
import sys
import unittest
from pathlib import Path
from threading import Event, Thread
from unittest.mock import MagicMock, patch

# Allow this file to be run directly as well as through unittest discovery.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.agent import AgentStartupError, HelperAgent, _Backoff, create_agent, main
from agent.communication import (
    CoordinatorAuthenticationError,
    CoordinatorConnectionError,
    CoordinatorEnrollmentError,
    CoordinatorHTTPError,
    CoordinatorTimeoutError,
    HealthResult,
    HeartbeatResult,
    InvalidCoordinatorResponseError,
    ReconnectResult,
    RegistrationResult,
)
from agent.config import AgentConfig
from agent.credentials import CredentialStoreError, StoredRegistration


def _config(invite_code: str | None = "valid-invite") -> AgentConfig:
    return AgentConfig(
        coordinator_url="https://coordinator.test:8000",
        client_id="local-client-id",
        node_display_name="Helper One",
        heartbeat_interval_seconds=10.0,
        request_timeout_seconds=2.5,
        reconnection_interval_seconds=3.0,
        rpc_host="100.64.0.20",
        rpc_port=50052,
        rpc_probe_timeout_seconds=0.5,
        state_dir=Path("test-state"),
        invite_code=invite_code,
    )


def _hardware(total_ram_gb: float | None = 16.0) -> dict[str, object]:
    return {
        "hostname": "helper-one",
        "operating_system": "Windows",
        "os_version": "10.0",
        "cpu_model": "Example CPU",
        "cpu_architecture": "AMD64",
        "cpu_core_count": 8,
        "total_ram_gb": total_ram_gb,
        "available_ram_gb": 8.0,
        "python_version": "3.12.3",
    }


class HelperAgentTests(unittest.TestCase):
    """Verify enrollment state, credential reuse, retries, and shutdown."""

    def setUp(self) -> None:
        self.client = MagicMock()
        self.client.check_health.return_value = HealthResult("healthy")
        self.client.reconnect.return_value = ReconnectResult("ok")
        self.credential_store = MagicMock()
        self.credential_store.load.return_value = None
        self.stop_event = MagicMock()
        self.stop_event.is_set.return_value = False
        self.logger = MagicMock(spec=logging.Logger)
        self.available_ram_probe = MagicMock(return_value=7.5)
        self.rpc_probe = MagicMock(return_value=True)

    def _agent(self, wait) -> HelperAgent:
        return HelperAgent(
            _config(),
            _hardware(),  # type: ignore[arg-type]
            self.client,
            self.credential_store,
            available_ram_probe=self.available_ram_probe,
            rpc_probe=self.rpc_probe,
            stop_event=self.stop_event,
            wait=wait,
            logger=self.logger,
        )

    def test_joins_once_persists_credentials_then_starts_heartbeat(self) -> None:
        registration = RegistrationResult("server-node", "secret-credential")
        self.client.join.return_value = registration

        def heartbeat_then_stop(node_id: str, credentials: str) -> HeartbeatResult:
            self.stop_event.is_set.return_value = True
            return HeartbeatResult("ok")

        self.client.send_heartbeat.side_effect = heartbeat_then_stop
        agent = self._agent(self.stop_event.wait)

        agent.run()

        self.client.join.assert_called_once_with(
            "valid-invite", "Helper One", 16.0, 50052, "local-client-id"
        )
        self.credential_store.save.assert_called_once_with(
            StoredRegistration(
                "https://coordinator.test:8000",
                "server-node",
                "secret-credential",
            )
        )
        self.client.send_heartbeat.assert_called_once_with(
            "server-node",
            "secret-credential",
            free_ram_gb=7.5,
            rpc_ready=True,
        )

    def test_enrollment_connection_retries_with_capped_backoff(self) -> None:
        self.client.check_health.side_effect = CoordinatorConnectionError("offline")
        waits: list[float] = []

        def stop_after_seven_waits(delay: float) -> bool:
            waits.append(delay)
            return len(waits) == 7

        self._agent(stop_after_seven_waits).run()

        self.assertEqual(waits, [3.0, 6.0, 12.0, 24.0, 48.0, 60.0, 60.0])
        self.assertEqual(self.client.check_health.call_count, 7)
        self.client.join.assert_not_called()
        self.client.send_heartbeat.assert_not_called()

    def test_backoff_cap_survives_reset(self) -> None:
        backoff = _Backoff(initial_seconds=120.0, maximum_seconds=60.0)

        self.assertEqual(backoff.next_delay(), 60.0)
        backoff.reset()
        self.assertEqual(backoff.next_delay(), 60.0)

    def test_coordinator_unavailable_at_startup_then_recovers(self) -> None:
        enrollment_attempt = 0

        def health_after_outage() -> HealthResult:
            nonlocal enrollment_attempt
            enrollment_attempt += 1
            if enrollment_attempt < 3:
                raise CoordinatorConnectionError("offline")
            return HealthResult("healthy")

        def heartbeat_then_stop(*_args) -> HeartbeatResult:
            self.stop_event.is_set.return_value = True
            return HeartbeatResult("ok")

        self.client.check_health.side_effect = health_after_outage
        self.client.join.return_value = RegistrationResult(
            "server-node", "secret-credential"
        )
        self.client.send_heartbeat.side_effect = heartbeat_then_stop
        waits: list[float] = []

        self._agent(lambda delay: waits.append(delay) or False).run()

        self.assertEqual(self.client.check_health.call_count, 3)
        self.client.join.assert_called_once()
        self.client.send_heartbeat.assert_called_once()
        self.assertEqual(waits, [3.0, 6.0, 10.0])

    def test_temporary_heartbeat_failure_does_not_reenroll(self) -> None:
        self.client.join.return_value = RegistrationResult(
            "server-node", "secret-credential"
        )

        def heartbeat_recovered(*_args) -> HeartbeatResult:
            self.stop_event.is_set.return_value = True
            return HeartbeatResult("ok")

        heartbeat_attempt = 0

        def heartbeat_side_effect(*args) -> HeartbeatResult:
            nonlocal heartbeat_attempt
            heartbeat_attempt += 1
            if heartbeat_attempt == 1:
                raise CoordinatorTimeoutError("timeout")
            return heartbeat_recovered(*args)

        self.client.send_heartbeat.side_effect = heartbeat_side_effect
        waits: list[float] = []

        self._agent(lambda delay: waits.append(delay) or False).run()

        self.client.join.assert_called_once()
        self.assertEqual(self.client.send_heartbeat.call_count, 2)
        self.assertEqual(waits, [3.0, 10.0])
        logged_events = [call.args[1] for call in self.logger.log.call_args_list]
        self.assertIn("heartbeat_failed", logged_events)
        self.assertIn("heartbeat_restored", logged_events)

    def test_repeated_heartbeat_timeouts_use_capped_backoff(self) -> None:
        self.client.join.return_value = RegistrationResult(
            "server-node", "secret-credential"
        )
        self.client.send_heartbeat.side_effect = CoordinatorTimeoutError("timeout")
        waits: list[float] = []

        def stop_after_seven_waits(delay: float) -> bool:
            waits.append(delay)
            return len(waits) == 7

        self._agent(stop_after_seven_waits).run()

        self.client.join.assert_called_once()
        self.assertEqual(self.client.send_heartbeat.call_count, 7)
        self.assertEqual(waits, [3.0, 6.0, 12.0, 24.0, 48.0, 60.0, 60.0])

    def test_heartbeat_http_error_retries_existing_registration(self) -> None:
        self.client.join.return_value = RegistrationResult(
            "server-node", "secret-credential"
        )
        self.client.send_heartbeat.side_effect = CoordinatorHTTPError(
            503, "temporarily unavailable"
        )

        self._agent(lambda _delay: True).run()

        self.client.join.assert_called_once()
        self.client.send_heartbeat.assert_called_once()
        log_text = str(self.logger.method_calls)
        self.assertIn("CoordinatorHTTPError", log_text)
        self.assertNotIn("temporarily unavailable", log_text)

    def test_invalid_heartbeat_response_retries_existing_registration(self) -> None:
        self.client.join.return_value = RegistrationResult(
            "server-node", "secret-credential"
        )
        self.client.send_heartbeat.side_effect = InvalidCoordinatorResponseError(
            "invalid response"
        )

        self._agent(lambda _delay: True).run()

        self.client.join.assert_called_once()
        self.client.send_heartbeat.assert_called_once()

    def test_restart_uses_stored_registration_without_duplicate_join(self) -> None:
        self.credential_store.load.return_value = StoredRegistration(
            "https://coordinator.test:8000",
            "stored-node",
            "stored-credential",
        )

        def heartbeat_then_stop(*_args) -> HeartbeatResult:
            self.stop_event.is_set.return_value = True
            return HeartbeatResult("ok")

        self.client.send_heartbeat.side_effect = heartbeat_then_stop

        self._agent(self.stop_event.wait).run()

        self.client.join.assert_not_called()
        self.credential_store.save.assert_not_called()
        self.client.reconnect.assert_called_once_with(
            "stored-node", "stored-credential"
        )
        self.client.send_heartbeat.assert_called_once_with(
            "stored-node",
            "stored-credential",
            free_ram_gb=7.5,
            rpc_ready=True,
        )

    def test_rejected_stored_credentials_use_explicit_invite_once(self) -> None:
        self.credential_store.load.return_value = StoredRegistration(
            "https://coordinator.test:8000",
            "stored-node",
            "stored-credential",
        )
        self.client.join.return_value = RegistrationResult(
            "replacement-node", "replacement-credential"
        )
        self.client.reconnect.side_effect = CoordinatorAuthenticationError(401, "invalid")
        self.client.send_heartbeat.return_value = HeartbeatResult("ok")
        waits: list[float] = []

        def wait_then_stop(delay: float) -> bool:
            waits.append(delay)
            if delay == 10.0:
                self.stop_event.is_set.return_value = True
            return False

        self._agent(wait_then_stop).run()

        self.credential_store.clear.assert_called_once_with()
        self.client.join.assert_called_once_with(
            "valid-invite", "Helper One", 16.0, 50052, "local-client-id"
        )
        self.assertEqual(waits, [3.0, 10.0])

    def test_rejected_new_credentials_do_not_loop_enrollment(self) -> None:
        self.client.join.return_value = RegistrationResult(
            "new-node", "new-credential"
        )
        self.client.send_heartbeat.side_effect = CoordinatorAuthenticationError(
            401, "invalid"
        )

        with self.assertRaises(AgentStartupError):
            self._agent(lambda _delay: False).run()

        self.client.join.assert_called_once()
        self.client.send_heartbeat.assert_called_once()
        self.credential_store.clear.assert_called_once_with()

    def test_first_run_without_invite_requires_enrollment(self) -> None:
        agent = HelperAgent(
            _config(invite_code=None),
            _hardware(),  # type: ignore[arg-type]
            self.client,
            self.credential_store,
            logger=self.logger,
        )

        with self.assertRaises(AgentStartupError):
            agent.run()

        self.client.join.assert_not_called()
        self.client.send_heartbeat.assert_not_called()

    def test_unreadable_stored_credentials_fail_before_networking(self) -> None:
        self.credential_store.load.side_effect = CredentialStoreError("corrupt")

        with self.assertRaises(AgentStartupError):
            self._agent(lambda _delay: False).run()

        self.client.join.assert_not_called()
        self.client.send_heartbeat.assert_not_called()

    def test_rejected_invite_fails_without_logging_invite(self) -> None:
        invite = "invite-must-not-appear-in-logs"
        self.client.join.side_effect = CoordinatorEnrollmentError(400, "invalid")

        with self.assertRaises(AgentStartupError):
            HelperAgent(
                _config(invite),
                _hardware(),  # type: ignore[arg-type]
                self.client,
                self.credential_store,
                logger=self.logger,
            ).run()

        self.assertNotIn(invite, str(self.logger.method_calls))

    def test_enrollment_timeout_fails_closed_to_avoid_duplicate(self) -> None:
        self.client.join.side_effect = CoordinatorTimeoutError("timeout")

        with self.assertRaises(AgentStartupError):
            self._agent(lambda _delay: False).run()

        self.client.join.assert_called_once()
        self.client.send_heartbeat.assert_not_called()

    def test_successful_heartbeats_use_configured_schedule(self) -> None:
        self.client.join.return_value = RegistrationResult(
            "server-node", "secret-credential"
        )
        heartbeat_number = 0

        def heartbeat(*_args) -> HeartbeatResult:
            nonlocal heartbeat_number
            heartbeat_number += 1
            if heartbeat_number == 2:
                self.stop_event.is_set.return_value = True
            return HeartbeatResult("ok")

        self.client.send_heartbeat.side_effect = heartbeat
        waits: list[float] = []

        self._agent(lambda delay: waits.append(delay) or False).run()

        self.client.join.assert_called_once()
        self.assertEqual(self.client.send_heartbeat.call_count, 2)
        self.assertEqual(waits, [10.0, 10.0])

    def test_invalid_enrollment_response_fails_closed_without_heartbeat(self) -> None:
        self.client.join.return_value = object()

        with self.assertRaises(AgentStartupError):
            self._agent(lambda _delay: False).run()

        self.client.join.assert_called_once()
        self.client.send_heartbeat.assert_not_called()

    def test_shutdown_interrupts_enrollment_retry(self) -> None:
        self.client.check_health.side_effect = CoordinatorConnectionError("offline")

        self._agent(lambda _delay: True).run()

        self.client.check_health.assert_called_once()
        self.client.join.assert_not_called()
        self.client.send_heartbeat.assert_not_called()

    def test_request_stop_leaves_no_agent_thread_running(self) -> None:
        attempted = Event()
        stop_event = Event()
        client = MagicMock()

        def unavailable(*_args) -> RegistrationResult:
            attempted.set()
            raise CoordinatorConnectionError("offline")

        client.check_health.side_effect = unavailable
        credential_store = MagicMock()
        credential_store.load.return_value = None
        agent = HelperAgent(
            _config(),
            _hardware(),  # type: ignore[arg-type]
            client,
            credential_store,
            stop_event=stop_event,
            logger=self.logger,
        )
        agent_thread = Thread(target=agent.run, name="test-lowllama-agent")

        agent_thread.start()
        self.assertTrue(attempted.wait(timeout=1))
        agent.request_stop()
        agent_thread.join(timeout=1)

        self.assertFalse(agent_thread.is_alive())
        logged_events = [call.args[1] for call in self.logger.log.call_args_list]
        self.assertEqual(logged_events[-1], "helper_agent_stopped")

    def test_credentials_are_never_logged(self) -> None:
        credential = "credential-must-not-be-logged"
        self.client.join.return_value = RegistrationResult(
            "server-node", credential
        )

        def heartbeat_then_stop(*_args) -> HeartbeatResult:
            self.stop_event.is_set.return_value = True
            return HeartbeatResult("ok")

        self.client.send_heartbeat.side_effect = heartbeat_then_stop

        self._agent(self.stop_event.wait).run()

        self.assertNotIn(credential, str(self.logger.method_calls))

    def test_missing_total_ram_prevents_startup(self) -> None:
        with self.assertRaises(AgentStartupError):
            HelperAgent(
                _config(),
                _hardware(None),  # type: ignore[arg-type]
                self.client,
                self.credential_store,
            )


class AgentConstructionTests(unittest.TestCase):
    """Verify dependency construction and command-line shutdown."""

    @patch("agent.agent.CredentialStore")
    @patch("agent.agent.CoordinatorClient")
    @patch("agent.agent.get_hardware_info")
    @patch("agent.agent.get_config")
    def test_create_agent_loads_dependencies_in_order(
        self,
        get_config,
        get_hardware_info,
        coordinator_client,
        credential_store,
    ) -> None:
        config = _config()
        hardware = _hardware()
        client = MagicMock()
        store = MagicMock()
        get_config.return_value = config
        get_hardware_info.return_value = hardware
        coordinator_client.return_value = client
        credential_store.return_value = store

        agent = create_agent()

        self.assertIsInstance(agent, HelperAgent)
        get_config.assert_called_once_with()
        get_hardware_info.assert_called_once_with()
        coordinator_client.assert_called_once_with(
            config.coordinator_url,
            config.request_timeout_seconds,
        )
        credential_store.assert_called_once_with(config.state_dir)

    @patch("agent.agent._install_signal_handlers")
    @patch("agent.agent.create_agent")
    def test_main_handles_ctrl_c_cleanly(
        self,
        create_agent_mock,
        install_signal_handlers,
    ) -> None:
        agent = MagicMock()
        agent.run.side_effect = KeyboardInterrupt
        create_agent_mock.return_value = agent

        result = main()

        self.assertEqual(result, 0)
        install_signal_handlers.assert_called_once()
        agent.request_stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
