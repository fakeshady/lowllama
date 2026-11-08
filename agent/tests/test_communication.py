"""Unit tests for the helper coordinator HTTP client."""

from __future__ import annotations

import io
import json
import logging
import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

# Allow this file to be run directly as well as through unittest discovery.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.communication import (
    CoordinatorAuthenticationError,
    CoordinatorClient,
    CoordinatorConnectionError,
    CoordinatorEnrollmentError,
    CoordinatorEnrollmentConflictError,
    CoordinatorHTTPError,
    CoordinatorTimeoutError,
    InvalidCoordinatorResponseError,
)
from agent.config import ConfigurationError


def _response(status: int, body: object) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.read.return_value = (
        body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    )
    response.__enter__.return_value = response
    response.__exit__.return_value = None
    return response


class CoordinatorClientTests(unittest.TestCase):
    """Verify the approved join and heartbeat contracts."""

    def setUp(self) -> None:
        self.opener = MagicMock()
        self.logger = MagicMock(spec=logging.Logger)
        self.client = CoordinatorClient(
            "https://coordinator.test:8000/",
            2.5,
            opener=self.opener,
            logger=self.logger,
        )

    def test_join_sends_contract_payload_and_parses_response(self) -> None:
        self.opener.open.return_value = _response(
            200, {"node_id": "node-123", "credentials": "cred-secret"}
        )

        invite = "invite-must-not-be-logged"
        result = self.client.join(
            invite,
            "TestLaptop",
            16.0,
            50051,
            "client-installation-123",
        )

        self.assertEqual(result.node_id, "node-123")
        self.assertEqual(result.credentials, "cred-secret")
        self.assertNotIn("cred-secret", repr(result))
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://coordinator.test:8000/join")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(request.data),
            {
                "invite_code": invite,
                "node_name": "TestLaptop",
                "ram_gb": 16.0,
                "rpc_port": 50051,
                "client_id": "client-installation-123",
            },
        )
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 2.5)
        self.assertNotIn(invite, str(self.logger.method_calls))

    def test_health_check_uses_get_and_validates_status(self) -> None:
        self.opener.open.return_value = _response(200, {"status": "healthy"})

        result = self.client.check_health()

        self.assertEqual(result.status, "healthy")
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://coordinator.test:8000/health")
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)

    def test_unhealthy_health_response_is_rejected(self) -> None:
        self.opener.open.return_value = _response(200, {"status": "starting"})

        with self.assertRaises(InvalidCoordinatorResponseError):
            self.client.check_health()

    def test_heartbeat_sends_credentials_in_required_body(self) -> None:
        self.opener.open.return_value = _response(200, {"status": "ok"})

        result = self.client.send_heartbeat(
            "node-123",
            "cred-secret",
            free_ram_gb=6.25,
            rpc_ready=True,
        )

        self.assertEqual(result.status, "ok")
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://coordinator.test:8000/heartbeat")
        self.assertEqual(
            json.loads(request.data),
            {
                "node_id": "node-123",
                "credentials": "cred-secret",
                "free_ram_gb": 6.25,
                "rpc_ready": True,
            },
        )

    def test_reconnect_sends_only_stored_identity(self) -> None:
        self.opener.open.return_value = _response(200, {"status": "ok"})

        result = self.client.reconnect("node-123", "cred-secret")

        self.assertEqual(result.status, "ok")
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://coordinator.test:8000/reconnect")
        self.assertEqual(
            json.loads(request.data),
            {"node_id": "node-123", "credentials": "cred-secret"},
        )

    def test_reconnect_authentication_failure_is_specific(self) -> None:
        self.opener.open.side_effect = HTTPError(
            "https://coordinator.test:8000/reconnect",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"Invalid node ID or credentials"}'),
        )

        with self.assertRaises(CoordinatorAuthenticationError):
            self.client.reconnect("node-123", "cred-secret")

        self.assertNotIn("cred-secret", str(self.logger.method_calls))

    def test_authentication_failure_has_specific_exception_and_no_secret_log(self) -> None:
        credentials = "credential-must-not-be-logged"
        self.opener.open.side_effect = HTTPError(
            "https://coordinator.test:8000/heartbeat",
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"Invalid node ID or credentials"}'),
        )

        with self.assertRaises(CoordinatorAuthenticationError) as raised:
            self.client.send_heartbeat("node-123", credentials)

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(
            raised.exception.detail, "Invalid node ID or credentials"
        )
        self.assertNotIn(credentials, str(self.logger.method_calls))

    def test_forbidden_response_is_authentication_failure(self) -> None:
        self.opener.open.return_value = _response(
            403, {"detail": "Unauthorized"}
        )

        with self.assertRaises(CoordinatorAuthenticationError) as raised:
            self.client.send_heartbeat("node-123", "cred-secret")

        self.assertEqual(raised.exception.status_code, 403)

    def test_invalid_invite_has_specific_enrollment_error(self) -> None:
        invite = "rejected-invite-must-not-be-logged"
        self.opener.open.side_effect = HTTPError(
            "https://coordinator.test:8000/join",
            400,
            "Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"Invalid or expired invite code"}'),
        )

        with self.assertRaises(CoordinatorEnrollmentError) as raised:
            self.client.join(invite, "TestLaptop", 16.0, 50051, "client-123")

        self.assertEqual(raised.exception.status_code, 400)
        self.assertNotIn(invite, str(self.logger.method_calls))

    def test_existing_client_id_has_specific_conflict_error(self) -> None:
        self.opener.open.side_effect = HTTPError(
            "https://coordinator.test:8000/join",
            409,
            "Conflict",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"this helper is already enrolled"}'),
        )

        with self.assertRaises(CoordinatorEnrollmentConflictError):
            self.client.join("invite", "TestLaptop", 16.0, 50051, "client-123")

    def test_other_unsuccessful_response_preserves_status_and_detail(self) -> None:
        self.opener.open.side_effect = HTTPError(
            "https://coordinator.test:8000/join",
            501,
            "Not Implemented",
            hdrs=None,
            fp=io.BytesIO(b'{"detail":"Registry module not implemented"}'),
        )

        with self.assertRaises(CoordinatorHTTPError) as raised:
            self.client.join("invite", "TestLaptop", 16.0, 50051, "client-123")

        self.assertEqual(raised.exception.status_code, 501)
        self.assertEqual(raised.exception.detail, "Registry module not implemented")

    def test_connection_failure_has_specific_exception(self) -> None:
        self.opener.open.side_effect = URLError("connection refused")

        with self.assertRaises(CoordinatorConnectionError):
            self.client.join("invite", "TestLaptop", 16.0, 50051, "client-123")

    def test_direct_timeout_has_specific_exception(self) -> None:
        self.opener.open.side_effect = socket.timeout("timed out")

        with self.assertRaises(CoordinatorTimeoutError):
            self.client.join("invite", "TestLaptop", 16.0, 50051, "client-123")

    def test_wrapped_timeout_has_specific_exception(self) -> None:
        self.opener.open.side_effect = URLError(socket.timeout("timed out"))

        with self.assertRaises(CoordinatorTimeoutError):
            self.client.join("invite", "TestLaptop", 16.0, 50051, "client-123")

    def test_invalid_json_response_is_rejected(self) -> None:
        self.opener.open.return_value = _response(200, b"not-json")

        with self.assertRaises(InvalidCoordinatorResponseError):
            self.client.join(
                "invite", "TestLaptop", 16.0, 50051, "client-123"
            )

    def test_missing_or_wrong_response_fields_are_rejected(self) -> None:
        invalid_responses = (
            {"credentials": "cred-secret"},
            {"node_id": 123, "credentials": "cred-secret"},
            {"node_id": "node-123", "credentials": ""},
        )

        for response_body in invalid_responses:
            with self.subTest(response_body=response_body):
                self.opener.open.return_value = _response(200, response_body)
                with self.assertRaises(InvalidCoordinatorResponseError):
                    self.client.join(
                        "invite", "TestLaptop", 16.0, 50051, "client-123"
                    )

    def test_success_log_contains_structure_without_payload(self) -> None:
        self.opener.open.return_value = _response(200, {"status": "ok"})

        self.client.send_heartbeat("node-123", "cred-secret")

        success_call = self.logger.log.call_args_list[-1]
        self.assertEqual(success_call.args[0], logging.INFO)
        self.assertEqual(success_call.args[1], "coordinator_request_succeeded")
        self.assertEqual(
            success_call.kwargs["extra"],
            {
                "lowllama_event": "coordinator_request_succeeded",
                "operation": "heartbeat",
                "status_code": 200,
            },
        )
        self.assertNotIn("cred-secret", str(self.logger.method_calls))

    def test_rejects_unprotected_http_before_sending_credentials(self) -> None:
        with self.assertRaises(ConfigurationError):
            CoordinatorClient(
                "http://coordinator.example.test:8000",
                2.5,
                opener=self.opener,
            )

        self.opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()
