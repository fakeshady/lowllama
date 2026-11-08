"""Unit tests for helper RPC readiness probing."""

from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.rpc import is_rpc_ready


class RpcReadinessTests(unittest.TestCase):
    """Verify bounded TCP probing and connection cleanup."""

    @patch("agent.rpc.socket.create_connection")
    def test_reachable_rpc_endpoint_is_ready(self, create_connection) -> None:
        connection = MagicMock()
        create_connection.return_value = connection

        self.assertTrue(is_rpc_ready("100.64.0.20", 50052, 0.5))

        create_connection.assert_called_once_with(("100.64.0.20", 50052), 0.5)
        connection.close.assert_called_once_with()

    @patch("agent.rpc.socket.create_connection", side_effect=ConnectionRefusedError)
    def test_refused_rpc_endpoint_is_not_ready(self, create_connection) -> None:
        self.assertFalse(is_rpc_ready("127.0.0.1", 50052, 0.5))
        create_connection.assert_called_once()

    @patch("agent.rpc.socket.create_connection", side_effect=socket.timeout)
    def test_timed_out_rpc_endpoint_is_not_ready(self, create_connection) -> None:
        self.assertFalse(is_rpc_ready("100.64.0.20", 50052, 0.5))
        create_connection.assert_called_once()


if __name__ == "__main__":
    unittest.main()
