import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Add the project root to sys.path to allow importing coordinator
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock registry and invites BEFORE importing app
mock_registry = MagicMock()
mock_invites = MagicMock()
sys.modules['coordinator.registry'] = mock_registry
sys.modules['coordinator.invites'] = mock_invites

from fastapi.testclient import TestClient
from coordinator.main import app

class TestCoordinatorAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        # Reset mocks before each test
        mock_registry.reset_mock()
        mock_invites.reset_mock()

    def test_root(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "LowLlama coordinator running"})

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "healthy"})

    def test_register_success(self):
        mock_registry.register_node.return_value = {"node_id": "node-123", "credentials": "cred-xyz"}
        
        response = self.client.post("/register", json={
            "node_name": "TestLaptop",
            "ram_gb": 16.0,
            "rpc_port": 50051
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"node_id": "node-123", "credentials": "cred-xyz"})
        mock_registry.register_node.assert_called_once_with(name="TestLaptop", ram=16.0, port=50051)

    def test_heartbeat_success(self):
        mock_registry.update_heartbeat.return_value = True
        
        response = self.client.post("/heartbeat", json={
            "node_id": "node-123",
            "credentials": "cred-xyz"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        mock_registry.update_heartbeat.assert_called_once_with(node_id="node-123", credentials="cred-xyz")

    def test_heartbeat_fail(self):
        mock_registry.update_heartbeat.return_value = False
        
        response = self.client.post("/heartbeat", json={
            "node_id": "unknown",
            "credentials": "wrong"
        })
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid node ID or credentials")

    def test_get_nodes(self):
        nodes_data = [
            {"node_id": "node-1", "node_name": "L1", "ram_gb": 8.0, "rpc_port": 5000, "status": "online"}
        ]
        mock_registry.get_all_nodes.return_value = nodes_data
        
        response = self.client.get("/nodes")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), nodes_data)

    def test_create_invite_success(self):
        mock_invites.create_invite.return_value = {"invite_code": "INV-123", "expires_at": "2026-12-31"}
        
        response = self.client.post("/invite", json={"admin_token": "secret-admin-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"invite_code": "INV-123", "expires_at": "2026-12-31"})

    def test_create_invite_unauthorized(self):
        response = self.client.post("/invite", json={"admin_token": "wrong-token"})
        self.assertEqual(response.status_code, 403)

    def test_join_success(self):
        mock_invites.validate_and_use_invite.return_value = True
        mock_registry.register_node.return_value = {"node_id": "node-456", "credentials": "cred-abc"}
        
        response = self.client.post("/join", json={
            "invite_code": "INV-123",
            "node_name": "NewLaptop",
            "ram_gb": 8.0,
            "rpc_port": 6000
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"node_id": "node-456", "credentials": "cred-abc"})

    def test_invalid_request_data(self):
        # Missing fields
        response = self.client.post("/register", json={"node_name": "Test"})
        self.assertEqual(response.status_code, 422)

if __name__ == '__main__':
    unittest.main()
