"""HTTP client for the approved LowLlama coordinator agent API.

The coordinator contract is defined in ``coordinator/main.py``:

* ``POST /register`` accepts ``node_name``, ``ram_gb``, and ``rpc_port``.
* ``POST /heartbeat`` accepts ``node_id`` and ``credentials``.

This module deliberately contains no server, registry, invitation, or agent
orchestration logic.
"""

from __future__ import annotations

import json
import logging
import socket
from dataclasses import dataclass, field
from json import JSONDecodeError
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import OpenerDirector, Request, build_opener

from agent.config import validate_coordinator_url


LOGGER = logging.getLogger(__name__)


class CommunicationError(Exception):
    """Base class for coordinator communication failures."""


class CoordinatorConnectionError(CommunicationError):
    """Raised when a connection to the coordinator cannot be established."""


class CoordinatorTimeoutError(CommunicationError):
    """Raised when a coordinator request exceeds its configured timeout."""


class CoordinatorHTTPError(CommunicationError):
    """Raised when the coordinator returns an unsuccessful HTTP status."""

    def __init__(self, status_code: int, detail: object | None = None) -> None:
        super().__init__(f"Coordinator returned HTTP {status_code}")
        self.status_code = status_code
        self.detail = detail


class CoordinatorAuthenticationError(CoordinatorHTTPError):
    """Raised for HTTP 401 or 403 authentication failures."""


class InvalidCoordinatorResponseError(CommunicationError):
    """Raised when a successful response does not match the API contract."""


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """Coordinator identity and credential returned by registration."""

    node_id: str
    credentials: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class HeartbeatResult:
    """Status returned by a successful heartbeat."""

    status: str


class _Response(Protocol):
    """Small response interface used to keep the client easy to test."""

    status: int

    def read(self) -> bytes:
        """Read the response body."""

        ...

    def __enter__(self) -> _Response:
        """Enter the response context manager."""

        ...

    def __exit__(self, *args: object) -> None:
        """Close the response context manager."""

        ...


class CoordinatorClient:
    """Reusable client for helper registration and heartbeat requests."""

    def __init__(
        self,
        coordinator_url: str,
        request_timeout_seconds: float,
        *,
        opener: OpenerDirector | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create a coordinator client.

        ``opener`` and ``logger`` may be injected by tests. URL and timeout
        validation remains centralized in ``agent.config`` for normal use.
        """

        self._coordinator_url = validate_coordinator_url(coordinator_url)
        self._request_timeout_seconds = request_timeout_seconds
        self._opener = opener or build_opener()
        self._logger = logger or LOGGER

    def register(self, node_name: str, ram_gb: float, rpc_port: int) -> RegistrationResult:
        """Register a helper using the coordinator's ``POST /register`` contract."""

        response = self._post_json(
            "/register",
            {
                "node_name": node_name,
                "ram_gb": ram_gb,
                "rpc_port": rpc_port,
            },
            operation="register",
        )
        node_id = self._required_nonempty_string(response, "node_id", "register")
        credentials = self._required_nonempty_string(
            response, "credentials", "register"
        )
        return RegistrationResult(node_id=node_id, credentials=credentials)

    def send_heartbeat(self, node_id: str, credentials: str) -> HeartbeatResult:
        """Send a helper heartbeat using the coordinator-issued credential."""

        response = self._post_json(
            "/heartbeat",
            {
                "node_id": node_id,
                "credentials": credentials,
            },
            operation="heartbeat",
        )
        status = self._required_nonempty_string(response, "status", "heartbeat")
        return HeartbeatResult(status=status)

    def _post_json(
        self,
        path: str,
        payload: dict[str, object],
        *,
        operation: str,
    ) -> dict[str, Any]:
        """POST JSON and return a validated top-level JSON object."""

        url = f"{self._coordinator_url}{path}"
        request = Request(
            url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        self._log(logging.DEBUG, "coordinator_request_started", operation=operation)

        try:
            with self._opener.open(
                request, timeout=self._request_timeout_seconds
            ) as response:
                status_code = response.status
                response_body = response.read()
        except HTTPError as exc:
            detail = self._read_http_error_detail(exc)
            self._raise_for_status(exc.code, detail, operation)
            raise AssertionError("unreachable")
        except (socket.timeout, TimeoutError) as exc:
            self._log(logging.WARNING, "coordinator_request_timeout", operation=operation)
            raise CoordinatorTimeoutError(
                f"Coordinator {operation} request timed out"
            ) from exc
        except URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                self._log(
                    logging.WARNING,
                    "coordinator_request_timeout",
                    operation=operation,
                )
                raise CoordinatorTimeoutError(
                    f"Coordinator {operation} request timed out"
                ) from exc
            self._log(
                logging.WARNING,
                "coordinator_connection_failed",
                operation=operation,
            )
            raise CoordinatorConnectionError(
                f"Unable to connect to coordinator for {operation}"
            ) from exc
        except OSError as exc:
            self._log(
                logging.WARNING,
                "coordinator_connection_failed",
                operation=operation,
            )
            raise CoordinatorConnectionError(
                f"Unable to connect to coordinator for {operation}"
            ) from exc

        if not 200 <= status_code < 300:
            detail = self._decode_error_detail(response_body)
            self._raise_for_status(status_code, detail, operation)

        response_data = self._decode_json_object(response_body, operation)
        self._log(
            logging.INFO,
            "coordinator_request_succeeded",
            operation=operation,
            status_code=status_code,
        )
        return response_data

    def _raise_for_status(
        self, status_code: int, detail: object | None, operation: str
    ) -> None:
        """Translate an unsuccessful status into the appropriate exception."""

        if status_code in {401, 403}:
            self._log(
                logging.WARNING,
                "coordinator_authentication_failed",
                operation=operation,
                status_code=status_code,
            )
            raise CoordinatorAuthenticationError(status_code, detail)

        self._log(
            logging.WARNING,
            "coordinator_http_error",
            operation=operation,
            status_code=status_code,
        )
        raise CoordinatorHTTPError(status_code, detail)

    def _decode_json_object(self, body: bytes, operation: str) -> dict[str, Any]:
        """Decode a successful response without exposing its raw body."""

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (JSONDecodeError, UnicodeDecodeError) as exc:
            self._log(
                logging.ERROR,
                "coordinator_invalid_response",
                operation=operation,
            )
            raise InvalidCoordinatorResponseError(
                f"Coordinator {operation} response is not valid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            self._log(
                logging.ERROR,
                "coordinator_invalid_response",
                operation=operation,
            )
            raise InvalidCoordinatorResponseError(
                f"Coordinator {operation} response must be a JSON object"
            )
        return decoded

    def _required_nonempty_string(
        self, response: dict[str, Any], field_name: str, operation: str
    ) -> str:
        """Extract a required non-empty string from a contract response."""

        value = response.get(field_name)
        if not isinstance(value, str) or not value.strip():
            self._log(
                logging.ERROR,
                "coordinator_invalid_response",
                operation=operation,
            )
            raise InvalidCoordinatorResponseError(
                f"Coordinator {operation} response has an invalid {field_name} field"
            )
        return value

    @staticmethod
    def _read_http_error_detail(error: HTTPError) -> object | None:
        """Read a FastAPI error detail when the error stream remains available."""

        try:
            return CoordinatorClient._decode_error_detail(error.read())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _decode_error_detail(body: bytes) -> object | None:
        """Decode only the documented FastAPI ``detail`` error field."""

        try:
            decoded = json.loads(body.decode("utf-8"))
        except (JSONDecodeError, UnicodeDecodeError):
            return None
        if isinstance(decoded, dict):
            return decoded.get("detail")
        return None

    def _log(self, level: int, event: str, **fields: object) -> None:
        """Emit structured metadata without request or credential contents."""

        # Payloads, credentials, and response bodies are intentionally excluded.
        self._logger.log(
            level,
            event,
            extra={"lowllama_event": event, **fields},
        )
