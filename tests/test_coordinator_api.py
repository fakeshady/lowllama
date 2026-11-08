import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Add the project root to sys.path to allow importing coordinator
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Set dummy admin token and master IP for tests
DEFAULT_ADMIN_TOKEN = "test-admin-token"
os.environ["LOWLLAMA_ADMIN_TOKEN"] = DEFAULT_ADMIN_TOKEN
os.environ["LOWLLAMA_MASTER_IP"] = "100.64.0.1"

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
            "rpc_port": 50051
        }, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
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
            "rpc_port": 50051
        }, headers={"Authorization": "Bearer wrong-token"})
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
        
        with patch.dict(os.environ, {"LOWLLAMA_ADMIN_TOKEN": "test-token"}):
            response = self.client.get("/nodes", headers={"Authorization": "Bearer test-token"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), nodes_data)

    def test_get_nodes_unauthorized(self):
        with patch.dict(os.environ, {"LOWLLAMA_ADMIN_TOKEN": "test-token"}):
            # No header -> 401 with HTTPBearer
            response = self.client.get("/nodes")
            self.assertEqual(response.status_code, 401)
            
            # Wrong token -> 403 (raised by verify_admin)
            response = self.client.get("/nodes", headers={"Authorization": "Bearer wrong-token"})
            self.assertEqual(response.status_code, 403)

    @patch("coordinator.invites.create_invite")
    def test_create_invite_success(self, mock_create_invite):
        mock_create_invite.return_value = {"invite_code": "INV-123", "expires_at": "2026-12-31"}
        
        response = self.client.post("/invite", json={}, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"invite_code": "INV-123", "expires_at": "2026-12-31"})

    def test_create_invite_unauthorized(self):
        response = self.client.post("/invite", json={}, headers={"Authorization": "Bearer wrong-token"})
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
        from coordinator.invites import InviteError
        with patch("coordinator.invites.validate") as mock_validate:
            mock_validate.side_effect = InviteError("invite code expired")
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
        # Remove admin token from env for this test
        old_token = os.environ.get("LOWLLAMA_ADMIN_TOKEN")
        if old_token is not None:
            del os.environ["LOWLLAMA_ADMIN_TOKEN"]
        try:
            response = self.client.post("/invite", json={}, headers={"Authorization": "Bearer any"})
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["detail"], "Admin access not configured")
        finally:
            # Restore
            if old_token is not None:
                os.environ["LOWLLAMA_ADMIN_TOKEN"] = old_token

    @patch("coordinator.registry.reconnect_node", create=True)
    def test_reconnect_success(self, mock_reconnect):
        mock_reconnect.return_value = True
        response = self.client.post("/reconnect", json={
            "node_id": "node-123",
            "credentials": "cred-xyz"
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    @patch("coordinator.registry.reconnect_node", create=True)
    def test_reconnect_fail(self, mock_reconnect):
        mock_reconnect.return_value = False
        response = self.client.post("/reconnect", json={
            "node_id": "node-123",
            "credentials": "wrong"
        })
        self.assertEqual(response.status_code, 401)

    @patch("coordinator.invites.create_invite")
    def test_invite_no_ip_error(self, mock_create_invite):
        # First test: env var is missing
        old_ip = os.environ.get("LOWLLAMA_MASTER_IP")
        if old_ip is not None:
            del os.environ["LOWLLAMA_MASTER_IP"]
        try:
            response = self.client.post("/invite", json={}, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["detail"], "set LOWLLAMA_MASTER_IP")
        finally:
            if old_ip is not None:
                os.environ["LOWLLAMA_MASTER_IP"] = old_ip

    @patch("coordinator.invites.create_invite")
    def test_invite_module_error(self, mock_create_invite):
        from coordinator.invites import InviteError
        os.environ["LOWLLAMA_MASTER_IP"] = "100.64.0.1"
        mock_create_invite.side_effect = InviteError("Other error")
        try:
            response = self.client.post("/invite", json={}, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
            self.assertEqual(response.status_code, 503)
            # detail will be str(e)
            self.assertEqual(response.json()["detail"], "Other error")
        finally:
            if "LOWLLAMA_MASTER_IP" in os.environ:
                del os.environ["LOWLLAMA_MASTER_IP"]

    def test_model_start_invalid_model(self):
        response = self.client.post("/start", 
                                    json={"model_id": "invalid-model"}, 
                                    headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("not in allowlist", response.json()["detail"])

    def test_model_start_unauthorized(self):
        response = self.client.post("/start", json={"model_id": "llama-3-8b"})
        self.assertEqual(response.status_code, 401)

    def test_model_status_success(self):
        launcher_mock = MagicMock()
        launcher_mock.status.return_value = {"status": "ready", "model_id": "llama-3-8b"}
        with patch("coordinator.launcher.status", launcher_mock.status):
            response = self.client.get("/status", headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "ready")

    def test_model_status_unauthorized(self):
        response = self.client.get("/status")
        self.assertEqual(response.status_code, 401)

    def test_model_stop_success(self):
        launcher_mock = MagicMock()
        with patch("coordinator.launcher.stop", launcher_mock.stop):
            response = self.client.post("/stop", headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "stopped")
            launcher_mock.stop.assert_called_once()

    @patch("coordinator.launcher.MODEL_ALLOWLIST", {"llama-3-8b": "path", "qwen2.5-7b": "path"})
    def test_model_start_success(self):
        workers_mock = MagicMock()
        selection_mock = MagicMock()
        selection_mock.candidates = [MagicMock()]
        selection_mock.selected_rejections.return_value = ()
        workers_mock.get_candidates.return_value = selection_mock
        launcher_mock = MagicMock()
        launcher_mock.start.return_value = {"status": "starting", "model_id": "llama-3-8b"}
        
        with patch("coordinator.workers.get_candidates", workers_mock.get_candidates):
            with patch("coordinator.launcher.start", launcher_mock.start):
                response = self.client.post("/start", 
                                            json={"model_id": "llama-3-8b", "node_ids": ["n1"]},
                                            headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "starting")

    @patch("httpx.AsyncClient.post")
    def test_chat_success(self, mock_post):
        launcher_mock = MagicMock()
        launcher_mock.status.return_value = {"status": "ready"}
        
        # Mock httpx response
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"message": {"content": "Hello"}}]}
        mock_post.return_value = mock_response
        
        with patch("coordinator.launcher.status", launcher_mock.status):
            response = self.client.post("/chat", json={
                "messages": [{"role": "user", "content": "Hi"}]
            }, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["choices"][0]["message"]["content"], "Hello")

    def test_chat_unauthorized(self):
        response = self.client.post("/chat", json={
            "messages": [{"role": "user", "content": "Hi"}]
        })
        self.assertEqual(response.status_code, 401)

    def test_chat_not_ready(self):
        launcher_mock = MagicMock()
        launcher_mock.status.return_value = {"status": "starting"}
        
        with patch("coordinator.launcher.status", launcher_mock.status):
            response = self.client.post("/chat", json={
                "messages": [{"role": "user", "content": "Hi"}]
            }, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
            self.assertEqual(response.status_code, 503)
            self.assertIn("not ready", response.json()["detail"])

    @patch("coordinator.launcher.MODEL_ALLOWLIST", {"qwen2.5-7b": "path"})
    def test_model_start_qwen(self):
        workers_mock = MagicMock()
        selection_mock = MagicMock()
        selection_mock.candidates = [MagicMock()]
        selection_mock.selected_rejections.return_value = ()
        workers_mock.get_candidates.return_value = selection_mock
        launcher_mock = MagicMock()
        launcher_mock.start.return_value = {"status": "starting", "model_id": "qwen2.5-7b"}
        
        with patch("coordinator.workers.get_candidates", workers_mock.get_candidates):
            with patch("coordinator.launcher.start", launcher_mock.start):
                response = self.client.post("/start", 
                                            json={"model_id": "qwen2.5-7b", "node_ids": ["n1"]},
                                            headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["model_id"], "qwen2.5-7b")

    @patch("coordinator.launcher.MODEL_ALLOWLIST", {"llama-3-8b": "path"})
    def test_model_start_empty_nodes(self):
        response = self.client.post("/start", 
                                    json={"model_id": "llama-3-8b", "node_ids": []},
                                    headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Selection must not be empty")

if __name__ == '__main__':
    unittest.main()
