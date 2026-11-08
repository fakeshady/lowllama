"""Local readiness probe for the helper's separately managed RPC service."""

from __future__ import annotations

import socket


def is_rpc_ready(host: str, port: int, timeout_seconds: float) -> bool:
    """Return whether a TCP listener is reachable at the configured RPC address.

    This proves only that the local RPC endpoint accepts TCP connections. The
    master launcher must still perform its own llama.cpp RPC compatibility and
    computation checks before assigning work.
    """

    try:
        connection = socket.create_connection((host, port), timeout_seconds)
    except (OSError, TimeoutError):
        return False
    try:
        return True
    finally:
        connection.close()
