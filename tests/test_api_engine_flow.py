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

class TestApiEngineFlow(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("coordinator.launcher.MODEL_ALLOWLIST", {"qwen2.5-0.5b-instruct-q4_k_m": "path/to/model"})
    @patch("coordinator.workers.get_candidates")
    @patch("coordinator.launcher.start")
    def test_start_flow_success(self, mock_start, mock_get_candidates):
        # Mock selection
        selection = MagicMock()
        selection.candidates = ["node-1"]
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
    def test_start_flow_rejected_nodes(self, mock_get_candidates):
        selection = MagicMock()
        selection.candidates = []
        selection.rejected = {"node-2": "offline"}
        mock_get_candidates.return_value = selection
        
        response = self.client.post("/start", json={
            "model_id": "qwen2.5-0.5b-instruct-q4_k_m",
            "node_ids": ["node-2"]
        }, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        
        self.assertEqual(response.status_code, 400)
        self.assertIn("Rejected: node-2: offline", response.json()["detail"])

    @patch("coordinator.launcher.status")
    def test_status_transitions(self, mock_status):
        mock_status.return_value = {"status": "starting"}
        response = self.client.get("/status", headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        self.assertEqual(response.json()["status"], "starting")
        
        mock_status.return_value = {"status": "ready"}
        response = self.client.get("/status", headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        self.assertEqual(response.json()["status"], "ready")

    @patch("coordinator.launcher.status")
    def test_chat_streaming_rejected(self, mock_status):
        mock_status.return_value = {"status": "ready"}
        response = self.client.post("/chat", json={
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True
        }, headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Streaming not supported", response.json()["detail"])

    @patch("coordinator.launcher.stop")
    def test_stop_flow(self, mock_stop):
        response = self.client.post("/stop", headers={"Authorization": f"Bearer {DEFAULT_ADMIN_TOKEN}"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "stopped")
        mock_stop.assert_called_once()

if __name__ == '__main__':
    unittest.main()
