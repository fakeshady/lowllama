"""Main orchestration loop for a LowLlama helper laptop."""

from __future__ import annotations

import logging
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from types import FrameType
from typing import Callable

# Support direct execution from an IDE or with ``python agent/agent.py``.
if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from agent.communication import (
    CommunicationError,
    CoordinatorAuthenticationError,
    CoordinatorClient,
    InvalidCoordinatorResponseError,
    RegistrationResult,
)
from agent.config import AgentConfig, ConfigurationError, get_config
from agent.hardware import HardwareInfo, get_hardware_info


LOGGER = logging.getLogger(__name__)
DEFAULT_RPC_PORT = 50052
MAX_RETRY_INTERVAL_SECONDS = 60.0


class AgentStartupError(RuntimeError):
    """Raised when required local information is unavailable."""


@dataclass(slots=True)
class _Backoff:
    """Deterministic exponential backoff with a fixed upper bound."""

    initial_seconds: float
    maximum_seconds: float = MAX_RETRY_INTERVAL_SECONDS
    _next_seconds: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize the first retry delay within the configured cap."""

        self._next_seconds = min(self.initial_seconds, self.maximum_seconds)

    def next_delay(self) -> float:
        """Return the next delay and advance the backoff."""

        delay = self._next_seconds
        self._next_seconds = min(self._next_seconds * 2, self.maximum_seconds)
        return delay

    def reset(self) -> None:
        """Reset the next delay after a successful operation."""

        self._next_seconds = min(self.initial_seconds, self.maximum_seconds)


class HelperAgent:
    """Coordinate registration, heartbeats, retries, and shutdown."""

    def __init__(
        self,
        config: AgentConfig,
        hardware_info: HardwareInfo,
        client: CoordinatorClient,
        *,
        rpc_port: int = DEFAULT_RPC_PORT,
        stop_event: Event | None = None,
        wait: Callable[[float], bool] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._rpc_port = rpc_port
        self._stop_event = stop_event or Event()
        self._wait = wait or self._stop_event.wait
        self._logger = logger or LOGGER
        self._ram_gb = self._required_ram_gb(hardware_info)

    @staticmethod
    def _required_ram_gb(hardware_info: HardwareInfo) -> float:
        """Return usable total RAM or fail before sending invalid registration."""

        ram_gb = hardware_info.get("total_ram_gb")
        if not isinstance(ram_gb, (int, float)) or isinstance(ram_gb, bool):
            raise AgentStartupError("Total RAM could not be detected")
        if ram_gb <= 0:
            raise AgentStartupError("Detected total RAM must be greater than zero")
        return float(ram_gb)

    def request_stop(self) -> None:
        """Request shutdown and interrupt any current retry or heartbeat wait."""

        self._stop_event.set()

    def run(self) -> None:
        """Run until shutdown is requested.

        A successful registration is retained across temporary heartbeat
        failures. Re-registration occurs only when no registration exists or
        the coordinator explicitly rejects its credentials.
        """

        self._log(
            logging.INFO,
            "helper_agent_started",
            node_name=self._config.node_display_name,
            rpc_port=self._rpc_port,
        )
        try:
            self._run_until_stopped()
        finally:
            self._log(logging.INFO, "helper_agent_stopped")

    def _run_until_stopped(self) -> None:
        """Execute the registration and heartbeat state machine."""

        registration: RegistrationResult | None = None
        registration_backoff = _Backoff(
            self._config.reconnection_interval_seconds
        )
        heartbeat_backoff = _Backoff(self._config.reconnection_interval_seconds)
        heartbeat_failure_active = False

        while not self._stop_event.is_set():
            if registration is None:
                try:
                    candidate = self._client.register(
                        self._config.node_display_name,
                        self._ram_gb,
                        self._rpc_port,
                    )
                    registration = self._verify_registration(candidate)
                except CommunicationError as exc:
                    delay = registration_backoff.next_delay()
                    self._log(
                        logging.WARNING,
                        "registration_failed",
                        error_type=type(exc).__name__,
                        retry_in_seconds=delay,
                    )
                    if self._wait(delay):
                        break
                    continue

                heartbeat_backoff.reset()
                heartbeat_failure_active = False
                self._log(
                    logging.INFO,
                    "helper_registered",
                    node_id=registration.node_id,
                )

            try:
                heartbeat = self._client.send_heartbeat(
                    registration.node_id,
                    registration.credentials,
                )
                if not heartbeat.status.strip():
                    raise InvalidCoordinatorResponseError(
                        "Coordinator heartbeat status is empty"
                    )
            except CoordinatorAuthenticationError as exc:
                self._log(
                    logging.WARNING,
                    "registration_credentials_rejected",
                    node_id=registration.node_id,
                    status_code=exc.status_code,
                    retry_in_seconds=(delay := registration_backoff.next_delay()),
                )
                registration = None
                heartbeat_backoff.reset()
                heartbeat_failure_active = False
                if self._wait(delay):
                    break
            except CommunicationError as exc:
                delay = heartbeat_backoff.next_delay()
                if not heartbeat_failure_active:
                    self._log(
                        logging.WARNING,
                        "heartbeat_failed",
                        node_id=registration.node_id,
                        error_type=type(exc).__name__,
                        retry_in_seconds=delay,
                    )
                else:
                    self._log(
                        logging.DEBUG,
                        "heartbeat_retry_failed",
                        node_id=registration.node_id,
                        error_type=type(exc).__name__,
                        retry_in_seconds=delay,
                    )
                heartbeat_failure_active = True
                if self._wait(delay):
                    break
            else:
                if heartbeat_failure_active:
                    self._log(
                        logging.INFO,
                        "heartbeat_restored",
                        node_id=registration.node_id,
                    )
                heartbeat_failure_active = False
                registration_backoff.reset()
                heartbeat_backoff.reset()
                self._log(
                    logging.DEBUG,
                    "heartbeat_sent",
                    node_id=registration.node_id,
                    status=heartbeat.status,
                )
                if self._wait(self._config.heartbeat_interval_seconds):
                    break

    @staticmethod
    def _verify_registration(result: object) -> RegistrationResult:
        """Require the typed, complete registration produced by the HTTP client."""

        if not isinstance(result, RegistrationResult):
            raise InvalidCoordinatorResponseError(
                "Coordinator registration result has an unexpected type"
            )
        if not result.node_id.strip() or not result.credentials.strip():
            raise InvalidCoordinatorResponseError(
                "Coordinator registration result is incomplete"
            )
        return result

    def _log(self, level: int, event: str, **fields: object) -> None:
        """Emit structured state changes without coordinator credentials."""

        # Credentials and response bodies are intentionally never included.
        self._logger.log(
            level,
            event,
            extra={"lowllama_event": event, **fields},
        )


def create_agent(*, stop_event: Event | None = None) -> HelperAgent:
    """Load dependencies and construct the process helper agent."""

    config = get_config()
    hardware_info = get_hardware_info()
    client = CoordinatorClient(
        config.coordinator_url,
        config.request_timeout_seconds,
    )
    return HelperAgent(
        config,
        hardware_info,
        client,
        stop_event=stop_event,
    )


def _install_signal_handlers(stop_event: Event) -> None:
    """Translate supported process termination signals into a stop request."""

    def handle_signal(signum: int, _frame: FrameType | None) -> None:
        LOGGER.info(
            "shutdown_requested",
            extra={"lowllama_event": "shutdown_requested", "signal": signum},
        )
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)


def main() -> int:
    """Run the helper process and return a command-line exit code."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    stop_event = Event()
    agent: HelperAgent | None = None

    try:
        agent = create_agent(stop_event=stop_event)
        _install_signal_handlers(stop_event)
        agent.run()
    except KeyboardInterrupt:
        LOGGER.info(
            "shutdown_requested",
            extra={"lowllama_event": "shutdown_requested", "signal": "Ctrl+C"},
        )
        stop_event.set()
        if agent is not None:
            agent.request_stop()
    except (ConfigurationError, AgentStartupError) as exc:
        LOGGER.error(
            "helper_agent_startup_failed",
            extra={
                "lowllama_event": "helper_agent_startup_failed",
                "error_type": type(exc).__name__,
            },
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
