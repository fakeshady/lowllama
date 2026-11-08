import unittest
from unittest.mock import patch, MagicMock
import sys
import os

# Add the project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

DEFAULT_ADMIN_TOKEN = "test-admin-token"
os.environ["LOWLLAMA_ADMIN_TOKEN"] = DEFAULT_ADMIN_TOKEN

from fastapi.testclient import TestClient
from coordinator.main import app

class TestApiEngineFlow(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("coordinator.launcher.MODEL_ALLOWLIST", {"qwen2.5-0.5b-instruct-q4_k_m": "path/to/model"})
    @patch("coordinator.workers.get_candidates")
    @patch("coordinator.launcher.start")
    async def test_start_flow_success(self, mock_start, mock_get_candidates):
        # Mock selection
        selection = MagicMock()
        selection.candidates = ["node-1"]
        selection.selected_rejections.return_value = ()
        mock_get_candidates.return_value = selection
        
        # Mock launcher start
        mock_start.return_value = {"status": "starting", "model_id": "qwen2.5-0.5b-instruct-q4_k_m"}
        
        response = self.client.post("/start", json={
            "model_id": "qwen2.5-0.5b-instruct-q4_k_m",
            "node_ids": ["node-1"]
        }, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "starting")
        mock_start.assert_called_once_with("qwen2.5-0.5b-instruct-q4_k_m", selection)

    @patch("coordinator.launcher.MODEL_ALLOWLIST", {"qwen2.5-0.5b-instruct-q4_k_m": "path/to/model"})
    @patch("coordinator.workers.get_candidates")
    async def test_start_flow_rejected_nodes(self, mock_get_candidates):
        selection = MagicMock()
        selection.candidates = []
        mock_rejected = MagicMock()
        mock_rejected.node_id = "node-2"
        mock_rejected.reason = "offline"
        selection.selected_rejections.return_value = (mock_rejected,)
        mock_get_candidates.return_value = selection
        
        response = self.client.post("/start", json={
            "model_id": "qwen2.5-0.5b-instruct-q4_k_m",
            "node_ids": ["node-2"]
        }, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        
        self.assertEqual(response.status_code, 400)
        self.assertIn("Selected workers unavailable: node-2: offline", response.json()["detail"])

    @patch("coordinator.launcher.MODEL_ALLOWLIST", {"qwen2.5-0.5b-instruct-q4_k_m": "path/to/model"})
    @patch("coordinator.workers.get_candidates")
    async def test_start_flow_partial_rejected_nodes(self, mock_get_candidates):
        # Mock selection with one candidate and one rejected worker
        selection = MagicMock()
        selection.candidates = ["node-1"]
        mock_rejected = MagicMock()
        mock_rejected.node_id = "node-2"
        mock_rejected.reason = "offline"
        selection.selected_rejections.return_value = (mock_rejected,)
        mock_get_candidates.return_value = selection
        
        response = self.client.post("/start", json={
            "model_id": "qwen2.5-0.5b-instruct-q4_k_m",
            "node_ids": ["node-1", "node-2"]
        }, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        
        self.assertEqual(response.status_code, 400)
        self.assertIn("Selected workers unavailable: node-2: offline", response.json()["detail"])

    @patch("coordinator.launcher.status")
    @patch("httpx.AsyncClient.post")
    async def test_chat_forwards_model_alias(self, mock_post, mock_status):
        mock_status.return_value = {"status": "ready", "model_id": "qwen-alias"}
        
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"choices": [{"message": {"content": "hello"}}]}
        mock_post.return_value = mock_response

        response = self.client.post("/chat", json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": False
        }, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})

        self.assertEqual(response.status_code, 200)
        
        # Verify mock_post was called with model="qwen-alias"
        args, kwargs = mock_post.call_args
        self.assertEqual(kwargs["json"]["model"], "qwen-alias")

    @patch("coordinator.launcher.status")
    async def test_status_transitions(self, mock_status):
        mock_status.return_value = {"status": "starting"}
        response = self.client.get("/status", headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        self.assertEqual(response.json()["status"], "starting")
        
        mock_status.return_value = {"status": "ready"}
        response = self.client.get("/status", headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        self.assertEqual(response.json()["status"], "ready")

    @patch("coordinator.launcher.status")
    async def test_chat_streaming_rejected(self, mock_status):
        mock_status.return_value = {"status": "ready"}
        response = self.client.post("/chat", json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True
        }, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Streaming not supported", response.json()["detail"])

    @patch("coordinator.launcher.stop")
    async def test_stop_flow(self, mock_stop):
        response = self.client.post("/stop", headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "stopped")
        mock_stop.assert_called_once()

if __name__ == '__main__':
    unittest.main()
