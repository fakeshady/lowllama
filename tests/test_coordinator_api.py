import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Add the project root to sys.path to allow importing coordinator
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set dummy admin token for tests
os.environ["LOWLLAMA_ADMIN_TOKEN"] = "test-admin-token"

from fastapi.testclient import TestClient
from coordinator.main import app

class TestCoordinatorAPI(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_root(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"message": "LowLlama coordinator running"})

    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "healthy"})

    @patch("coordinator.registry.register_node")
    def test_register_success(self, mock_register_node):
        mock_register_node.return_value = {"node_id": "node-123", "credentials": "cred-xyz"}
        
        response = self.client.post("/register", json={
            "node_name": "TestLaptop",
            "ram_gb": 16.0,
            "rpc_port": 50051,
            "admin_token": "test-admin-token"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"node_id": "node-123", "credentials": "cred-xyz"})
        # Note: address is now passed
        mock_register_node.assert_called_once()
        args, kwargs = mock_register_node.call_args
        self.assertEqual(kwargs['name'], "TestLaptop")
        self.assertEqual(kwargs['ram'], 16.0)
        self.assertEqual(kwargs['port'], 50051)

    def test_register_unauthorized(self):
        response = self.client.post("/register", json={
            "node_name": "TestLaptop",
            "ram_gb": 16.0,
            "rpc_port": 50051,
            "admin_token": "wrong-token"
        })
        self.assertEqual(response.status_code, 403)

    @patch("coordinator.registry.update_heartbeat")
    def test_heartbeat_success(self, mock_update_heartbeat):
        mock_update_heartbeat.return_value = True
        
        response = self.client.post("/heartbeat", json={
            "node_id": "node-123",
            "credentials": "cred-xyz",
            "free_ram_gb": 4.5,
            "rpc_ready": True
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        mock_update_heartbeat.assert_called_once_with(
            node_id="node-123", 
            credentials="cred-xyz",
            free_ram_gb=4.5,
            rpc_ready=True
        )

    @patch("coordinator.registry.update_heartbeat")
    def test_heartbeat_fail(self, mock_update_heartbeat):
        mock_update_heartbeat.return_value = False
        
        response = self.client.post("/heartbeat", json={
            "node_id": "unknown",
            "credentials": "wrong"
        })
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Invalid node ID or credentials")

    @patch("coordinator.registry.get_all_nodes")
    def test_get_nodes(self, mock_get_all_nodes):
        nodes_data = [
            {
                "node_id": "node-1", 
                "node_name": "L1", 
                "ram_gb": 8.0, 
                "free_ram_gb": 2.0,
                "rpc_port": 5000, 
                "address": "100.64.0.1",
                "rpc_ready": True,
                "status": "online",
                "last_seen_s": 0.5
            }
        ]
        mock_get_all_nodes.return_value = nodes_data
        
        response = self.client.get("/nodes")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), nodes_data)

    @patch("coordinator.invites.create_invite")
    def test_create_invite_success(self, mock_create_invite):
        mock_create_invite.return_value = {"invite_code": "INV-123", "expires_at": "2026-12-31"}
        
        response = self.client.post("/invite", json={"admin_token": "test-admin-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"invite_code": "INV-123", "expires_at": "2026-12-31"})

    def test_create_invite_unauthorized(self):
        response = self.client.post("/invite", json={"admin_token": "wrong-token"})
        self.assertEqual(response.status_code, 403)

    @patch("coordinator.registry.register_node")
    @patch("coordinator.invites.validate")
    def test_join_success(self, mock_validate, mock_register_node):
        mock_validate.return_value = {"token": "tok-123"}
        mock_register_node.return_value = {"node_id": "node-456", "credentials": "cred-abc"}
        
        response = self.client.post("/join", json={
            "invite_code": "INV-123",
            "node_name": "NewLaptop",
            "ram_gb": 8.0,
            "rpc_port": 6000,
            "client_id": "client-orig-123"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"node_id": "node-456", "credentials": "cred-abc"})

    def test_join_invalid_invite(self):
        with patch("coordinator.invites.validate") as mock_validate:
            mock_validate.side_effect = Exception("invite code expired")
            response = self.client.post("/join", json={
                "invite_code": "EXPIRED",
                "node_name": "NewLaptop",
                "ram_gb": 8.0,
                "rpc_port": 6000,
                "client_id": "client-orig-123"
            })
            self.assertEqual(response.status_code, 400)
            self.assertIn("expired", response.json()["detail"])

    def test_invalid_request_data(self):
        # Missing fields (e.g., missing ram_gb)
        response = self.client.post("/join", json={"node_name": "Test"})
        self.assertEqual(response.status_code, 422)

    def test_admin_fail_closed(self):
        # Remove admin token from env
        del os.environ["LOWLLAMA_ADMIN_TOKEN"]
        try:
            response = self.client.post("/invite", json={"admin_token": "any"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["detail"], "Admin access not configured")
        finally:
            # Restore for other tests if needed
            os.environ["LOWLLAMA_ADMIN_TOKEN"] = "test-admin-token"

if __name__ == '__main__':
    unittest.main()
