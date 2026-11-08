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
    CoordinatorEnrollmentConflictError,
    CoordinatorEnrollmentError,
    InvalidCoordinatorResponseError,
    RegistrationResult,
)
from agent.config import AgentConfig, ConfigurationError, get_config
from agent.credentials import (
    CredentialStore,
    CredentialStoreError,
    StoredRegistration,
)
from agent.hardware import HardwareInfo, get_available_ram_gb, get_hardware_info
from agent.rpc import is_rpc_ready


LOGGER = logging.getLogger(__name__)
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
    """Coordinate enrollment, heartbeats, retries, and shutdown."""

    def __init__(
        self,
        config: AgentConfig,
        hardware_info: HardwareInfo,
        client: CoordinatorClient,
        credential_store: CredentialStore,
        *,
        available_ram_probe: Callable[[], float | None] = get_available_ram_gb,
        rpc_probe: Callable[[str, int, float], bool] = is_rpc_ready,
        stop_event: Event | None = None,
        wait: Callable[[float], bool] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._credential_store = credential_store
        self._available_ram_probe = available_ram_probe
        self._rpc_probe = rpc_probe
        self._rpc_port = config.rpc_port
        self._stop_event = stop_event or Event()
        self._wait = wait or self._stop_event.wait
        self._logger = logger or LOGGER
        self._ram_gb = self._required_ram_gb(hardware_info)

    @staticmethod
    def _required_ram_gb(hardware_info: HardwareInfo) -> float:
        """Return usable total RAM or fail before sending invalid enrollment."""

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

        A successful enrollment is retained across temporary heartbeat
        failures. Server-issued credentials are loaded before any enrollment
        attempt and are removed if the coordinator explicitly rejects them.
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
        """Execute the enrollment and heartbeat state machine."""

        registration = self._load_registration()
        reconnect_required = registration is not None
        enrollment_backoff = _Backoff(
            self._config.reconnection_interval_seconds
        )
        heartbeat_backoff = _Backoff(self._config.reconnection_interval_seconds)
        heartbeat_failure_active = False
        invite_used = False

        while not self._stop_event.is_set():
            if registration is not None and reconnect_required:
                try:
                    reconnect = self._client.reconnect(
                        registration.node_id,
                        registration.credentials,
                    )
                    self._require_ok_status(reconnect.status, "reconnect")
                except CoordinatorAuthenticationError as exc:
                    self._log(
                        logging.WARNING,
                        "registration_credentials_rejected",
                        node_id=registration.node_id,
                        status_code=exc.status_code,
                    )
                    self._clear_rejected_registration()
                    registration = None
                    reconnect_required = False
                    if not self._config.invite_code:
                        raise AgentStartupError(
                            "Stored credentials were rejected; obtain a new invite and restart"
                        ) from exc
                    delay = enrollment_backoff.next_delay()
                    self._log(
                        logging.INFO,
                        "reenrollment_scheduled",
                        retry_in_seconds=delay,
                    )
                    if self._wait(delay):
                        break
                    continue
                except CommunicationError as exc:
                    delay = heartbeat_backoff.next_delay()
                    self._log(
                        logging.WARNING,
                        "reconnect_failed",
                        error_type=type(exc).__name__,
                        retry_in_seconds=delay,
                    )
                    if self._wait(delay):
                        break
                    continue
                reconnect_required = False
                heartbeat_backoff.reset()
                self._log(
                    logging.INFO,
                    "helper_reconnected",
                    node_id=registration.node_id,
                )

            if registration is None:
                if not self._config.invite_code:
                    raise AgentStartupError(
                        "Enrollment is required; set LOWLLAMA_INVITE_CODE and restart"
                    )
                try:
                    self._client.check_health()
                except CommunicationError as exc:
                    delay = enrollment_backoff.next_delay()
                    self._log(
                        logging.WARNING,
                        "enrollment_connection_failed",
                        error_type=type(exc).__name__,
                        retry_in_seconds=delay,
                    )
                    if self._wait(delay):
                        break
                    continue

                try:
                    candidate = self._client.join(
                        self._config.invite_code,
                        self._config.node_display_name,
                        self._ram_gb,
                        self._rpc_port,
                        self._config.client_id,
                    )
                    registration = self._verify_registration(candidate)
                    self._credential_store.save(
                        StoredRegistration(
                            coordinator_url=self._config.coordinator_url,
                            node_id=registration.node_id,
                            credentials=registration.credentials,
                        )
                    )
                    invite_used = True
                except CoordinatorEnrollmentConflictError as exc:
                    self._log(
                        logging.ERROR,
                        "enrollment_conflict",
                        status_code=exc.status_code,
                    )
                    raise AgentStartupError(
                        "This client is already enrolled; restore its credentials or ask the administrator to remove it"
                    ) from exc
                except CoordinatorEnrollmentError as exc:
                    self._log(
                        logging.ERROR,
                        "enrollment_rejected",
                        status_code=exc.status_code,
                    )
                    raise AgentStartupError(
                        "Coordinator rejected the invite; obtain a new invite and restart"
                    ) from exc
                except CommunicationError as exc:
                    self._log(
                        logging.ERROR,
                        "enrollment_outcome_unknown",
                        error_type=type(exc).__name__,
                    )
                    raise AgentStartupError(
                        "Enrollment outcome is unknown; check the coordinator before retrying"
                    ) from exc
                except CredentialStoreError as exc:
                    self._log(
                        logging.ERROR,
                        "credential_persistence_failed",
                        error_type=type(exc).__name__,
                    )
                    raise AgentStartupError(
                        "Coordinator credentials could not be stored securely"
                    ) from exc

                heartbeat_backoff.reset()
                heartbeat_failure_active = False
                self._log(
                    logging.INFO,
                    "helper_registered",
                    node_id=registration.node_id,
                )

            try:
                free_ram_gb, rpc_ready = self._runtime_status()
                heartbeat = self._client.send_heartbeat(
                    registration.node_id,
                    registration.credentials,
                    free_ram_gb=free_ram_gb,
                    rpc_ready=rpc_ready,
                )
                self._require_ok_status(heartbeat.status, "heartbeat")
            except CoordinatorAuthenticationError as exc:
                self._log(
                    logging.WARNING,
                    "registration_credentials_rejected",
                    node_id=registration.node_id,
                    status_code=exc.status_code,
                )
                self._clear_rejected_registration()
                registration = None
                heartbeat_backoff.reset()
                heartbeat_failure_active = False
                if invite_used or not self._config.invite_code:
                    raise AgentStartupError(
                        "Stored credentials were rejected; obtain a new invite and restart"
                    ) from exc
                delay = enrollment_backoff.next_delay()
                self._log(
                    logging.INFO,
                    "reenrollment_scheduled",
                    retry_in_seconds=delay,
                )
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
                enrollment_backoff.reset()
                heartbeat_backoff.reset()
                self._log(
                    logging.DEBUG,
                    "heartbeat_sent",
                    node_id=registration.node_id,
                    status=heartbeat.status,
                )
                if self._wait(self._config.heartbeat_interval_seconds):
                    break

    def _load_registration(self) -> RegistrationResult | None:
        """Load server-issued credentials before considering enrollment."""

        try:
            stored = self._credential_store.load(self._config.coordinator_url)
        except CredentialStoreError as exc:
            raise AgentStartupError(
                "Stored coordinator credentials could not be loaded securely"
            ) from exc
        if stored is None:
            return None
        registration = self._verify_registration(
            RegistrationResult(stored.node_id, stored.credentials)
        )
        self._log(
            logging.INFO,
            "stored_registration_loaded",
            node_id=registration.node_id,
        )
        return registration

    def _clear_rejected_registration(self) -> None:
        """Remove credentials that the coordinator explicitly rejected."""

        try:
            self._credential_store.clear()
        except CredentialStoreError as exc:
            raise AgentStartupError(
                "Rejected coordinator credentials could not be removed"
            ) from exc

    def _runtime_status(self) -> tuple[float | None, bool]:
        """Collect current RAM and bounded local RPC reachability safely."""

        try:
            free_ram_gb = self._available_ram_probe()
        except Exception:
            free_ram_gb = None
        try:
            rpc_ready = self._rpc_probe(
                self._config.rpc_host,
                self._config.rpc_port,
                self._config.rpc_probe_timeout_seconds,
            )
        except Exception:
            rpc_ready = False
        return free_ram_gb, bool(rpc_ready)

    @staticmethod
    def _require_ok_status(status: object, operation: str) -> None:
        """Require the coordinator's documented successful status value."""

        if not isinstance(status, str) or status.strip().lower() != "ok":
            raise InvalidCoordinatorResponseError(
                f"Coordinator {operation} response has an invalid status"
            )

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
    credential_store = CredentialStore(config.state_dir)
    return HelperAgent(
        config,
        hardware_info,
        client,
        credential_store,
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
