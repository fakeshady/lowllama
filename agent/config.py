"""Central configuration for the LowLlama helper agent.

Configuration is read from these environment variables:

``LOWLLAMA_COORDINATOR_URL``
    Coordinator base URL. Defaults to ``http://127.0.0.1:8000`` for local
    development. Remote coordinators must use HTTPS or a literal Tailscale IP
    address so coordinator-issued credentials are not sent over an unprotected
    connection. Production deployments should set this explicitly.
``LOWLLAMA_NODE_ID``
    Stable node identifier. When omitted, a UUID is generated and stored in
    the local agent state directory.
``LOWLLAMA_NODE_NAME``
    Human-readable display name. Defaults to the computer hostname.
``LOWLLAMA_HEARTBEAT_INTERVAL_SECONDS``
    Seconds between heartbeats. Defaults to ``5`` so the helper reports well
    before the coordinator's current 10-second offline cutoff.
``LOWLLAMA_REQUEST_TIMEOUT_SECONDS``
    HTTP request timeout in seconds. Defaults to ``10``.
``LOWLLAMA_RECONNECTION_INTERVAL_SECONDS``
    Seconds to wait before reconnecting. Defaults to ``5``.
``LOWLLAMA_AUTH_TOKEN``
    Optional opaque coordinator credential. It is retained only in memory,
    excluded from ``repr()``, and never written to the node ID file.
``LOWLLAMA_STATE_DIR``
    Optional location for non-secret local state. Defaults to the Windows
    Local AppData directory (or the platform's user state directory).

There are no built-in production credentials. If the coordinator requires
authentication, ``LOWLLAMA_AUTH_TOKEN`` must be supplied by the environment.
"""

from __future__ import annotations

import math
import os
import re
import socket
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlsplit


DEFAULT_COORDINATOR_URL = "http://127.0.0.1:8000"
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 5.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
DEFAULT_RECONNECTION_INTERVAL_SECONDS = 5.0

_NODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_NODE_ID_FILENAME = "node_id"
_TAILSCALE_NETWORKS = (
    ip_network("100.64.0.0/10"),
    ip_network("fd7a:115c:a1e0::/48"),
)


class ConfigurationError(ValueError):
    """Raised when helper-agent configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Validated configuration used by helper-agent modules."""

    coordinator_url: str
    node_id: str
    node_display_name: str
    heartbeat_interval_seconds: float
    request_timeout_seconds: float
    reconnection_interval_seconds: float
    auth_token: str | None = field(default=None, repr=False)


def _is_protected_http_host(hostname: str) -> bool:
    """Return whether HTTP is protected by loopback or the Tailscale tunnel."""

    if hostname.lower() == "localhost":
        return True
    try:
        address = ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or any(
        address.version == network.version and address in network
        for network in _TAILSCALE_NETWORKS
    )


def validate_coordinator_url(value: str) -> str:
    """Validate and normalize a coordinator URL safe for credential transport.

    HTTPS is accepted for any valid host. Plain HTTP is limited to loopback
    development or literal Tailscale addresses, whose traffic is encrypted by
    the project's Tailscale tunnel. Other remote HTTP URLs are rejected before
    registration can return credentials or a heartbeat can transmit them.
    """

    value = value.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ConfigurationError("LOWLLAMA_COORDINATOR_URL is not a valid URL") from exc

    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(
            "LOWLLAMA_COORDINATOR_URL must be an absolute HTTP or HTTPS URL"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ConfigurationError(
            "LOWLLAMA_COORDINATOR_URL must not contain embedded credentials"
        )
    if parsed.query or parsed.fragment:
        raise ConfigurationError(
            "LOWLLAMA_COORDINATOR_URL must not contain a query or fragment"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ConfigurationError("LOWLLAMA_COORDINATOR_URL contains an invalid port")
    if parsed.scheme == "http" and not _is_protected_http_host(parsed.hostname):
        raise ConfigurationError(
            "LOWLLAMA_COORDINATOR_URL must use HTTPS unless its host is "
            "loopback or a literal Tailscale address"
        )

    return value.rstrip("/")


def _validate_node_id(value: str, source: str = "LOWLLAMA_NODE_ID") -> str:
    """Return a safe node identifier or raise ``ConfigurationError``."""

    value = value.strip()
    if not _NODE_ID_PATTERN.fullmatch(value):
        raise ConfigurationError(
            f"{source} must be 1-128 characters using letters, digits, '.', '_', ':', or '-'"
        )
    return value


def _validate_display_name(value: str) -> str:
    """Validate the human-readable helper name."""

    value = value.strip()
    if not value or len(value) > 100:
        raise ConfigurationError("LOWLLAMA_NODE_NAME must contain 1-100 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigurationError("LOWLLAMA_NODE_NAME must not contain control characters")
    return value


def _parse_interval(
    environment: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    """Parse a finite interval constrained to the supplied range."""

    raw_value = environment.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value.strip())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigurationError(
            f"{name} must be between {minimum:g} and {maximum:g} seconds"
        )
    return value


def _validate_auth_token(environment: Mapping[str, str]) -> str | None:
    """Read the optional in-memory token without providing a secret default."""

    if "LOWLLAMA_AUTH_TOKEN" not in environment:
        return None
    token = environment["LOWLLAMA_AUTH_TOKEN"].strip()
    if not token:
        raise ConfigurationError("LOWLLAMA_AUTH_TOKEN must not be empty when set")
    if len(token) > 4096:
        raise ConfigurationError("LOWLLAMA_AUTH_TOKEN is unexpectedly long")
    return token


def _default_state_dir(environment: Mapping[str, str]) -> Path:
    """Resolve the per-user directory used only for non-secret agent state."""

    configured = environment.get("LOWLLAMA_STATE_DIR")
    if configured is not None:
        configured = configured.strip()
        if not configured:
            raise ConfigurationError("LOWLLAMA_STATE_DIR must not be empty when set")
        return Path(configured).expanduser()

    if os.name == "nt":
        local_app_data = environment.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "LowLlama" / "Agent"
        return Path.home() / "AppData" / "Local" / "LowLlama" / "Agent"

    xdg_state_home = environment.get("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "lowllama"
    return Path.home() / ".local" / "state" / "lowllama"


def _read_node_id(path: Path) -> str:
    """Read and validate a previously persisted node ID."""

    try:
        value = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(f"Unable to read persistent node ID from {path}") from exc
    return _validate_node_id(value, f"persistent node ID in {path}")


def _load_or_create_node_id(state_dir: Path) -> str:
    """Atomically create or reuse the helper's stable local UUID."""

    node_id_path = state_dir / _NODE_ID_FILENAME
    if node_id_path.exists():
        return _read_node_id(node_id_path)

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigurationError(f"Unable to create agent state directory {state_dir}") from exc

    generated_id = str(uuid.uuid4())
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(node_id_path, flags, 0o600)
    except FileExistsError:
        # Another agent process won the creation race; use its identity.
        return _read_node_id(node_id_path)
    except OSError as exc:
        raise ConfigurationError(f"Unable to persist node ID at {node_id_path}") from exc

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as node_id_file:
            node_id_file.write(f"{generated_id}\n")
    except OSError as exc:
        raise ConfigurationError(f"Unable to persist node ID at {node_id_path}") from exc
    return generated_id


def _default_display_name(node_id: str) -> str:
    """Use the hostname when available and a non-secret fallback otherwise."""

    try:
        hostname = socket.gethostname().strip()
    except Exception:
        hostname = ""
    return hostname or f"helper-{node_id[:8]}"


def load_config(
    *,
    environment: Mapping[str, str] | None = None,
    state_dir: str | os.PathLike[str] | None = None,
) -> AgentConfig:
    """Load and validate helper configuration.

    ``environment`` and ``state_dir`` are injectable to keep tests isolated.
    In normal use, omit both arguments to read ``os.environ`` and the standard
    per-user state directory.
    """

    environment = os.environ if environment is None else environment

    configured_node_id = environment.get("LOWLLAMA_NODE_ID")
    if configured_node_id is not None:
        node_id = _validate_node_id(configured_node_id)
    else:
        resolved_state_dir = (
            Path(state_dir).expanduser()
            if state_dir is not None
            else _default_state_dir(environment)
        )
        node_id = _load_or_create_node_id(resolved_state_dir)

    configured_name = environment.get("LOWLLAMA_NODE_NAME")
    node_display_name = _validate_display_name(
        configured_name if configured_name is not None else _default_display_name(node_id)
    )

    return AgentConfig(
        coordinator_url=validate_coordinator_url(
            environment.get("LOWLLAMA_COORDINATOR_URL", DEFAULT_COORDINATOR_URL)
        ),
        node_id=node_id,
        node_display_name=node_display_name,
        heartbeat_interval_seconds=_parse_interval(
            environment,
            "LOWLLAMA_HEARTBEAT_INTERVAL_SECONDS",
            DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
            minimum=1,
            maximum=3600,
        ),
        request_timeout_seconds=_parse_interval(
            environment,
            "LOWLLAMA_REQUEST_TIMEOUT_SECONDS",
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
            minimum=0.1,
            maximum=300,
        ),
        reconnection_interval_seconds=_parse_interval(
            environment,
            "LOWLLAMA_RECONNECTION_INTERVAL_SECONDS",
            DEFAULT_RECONNECTION_INTERVAL_SECONDS,
            minimum=0.1,
            maximum=3600,
        ),
        auth_token=_validate_auth_token(environment),
    )


@lru_cache(maxsize=1)
def get_config() -> AgentConfig:
    """Return the process-wide helper configuration singleton."""

    return load_config()
