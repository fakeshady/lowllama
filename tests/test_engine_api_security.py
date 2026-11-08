import pytest
import os
import secrets
from fastapi.testclient import TestClient
from coordinator.main import app

# Use a fresh client for each test to avoid state issues
@pytest.fixture
def client(monkeypatch):
    # Set a dummy admin token for tests
    token = "test-admin-token"
    monkeypatch.setenv("LOWLLAMA_ADMIN_TOKEN", token)
    return TestClient(app)

def test_health_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}

def test_admin_routes_fail_without_token(client, monkeypatch):
    # Ensure token is set but we don't provide it
    admin_token = "secret-token"
    monkeypatch.setenv("LOWLLAMA_ADMIN_TOKEN", admin_token)
    
    routes = [
        ("/nodes", "GET"),
        ("/invite", "POST"),
        ("/register", "POST"),
        ("/start", "POST"),
        ("/status", "GET"),
        ("/stop", "POST"),
        ("/chat", "POST"),
    ]
    
    for route, method in routes:
        if method == "GET":
            response = client.get(route)
        else:
            response = client.post(route, json={})
        # FastAPI HTTPBearer returns 401 for missing credentials, 
        # and our verify_admin returns 403 for wrong/missing-env token.
        assert response.status_code in (401, 403), f"Route {route} did not fail closed without token"

def test_admin_routes_fail_with_wrong_token(client, monkeypatch):
    admin_token = "secret-token"
    monkeypatch.setenv("LOWLLAMA_ADMIN_TOKEN", admin_token)
    
    headers = {"Authorization": "Bearer wrong-token"}
    
    routes = [
        ("/nodes", "GET"),
        ("/invite", "POST"),
        ("/register", "POST"),
        ("/start", "POST"),
        ("/status", "GET"),
        ("/stop", "POST"),
        ("/chat", "POST"),
    ]
    
    for route, method in routes:
        if method == "GET":
            response = client.get(route, headers=headers)
        else:
            response = client.post(route, json={}, headers=headers)
        assert response.status_code == 403, f"Route {route} did not fail with wrong token"

def test_fail_closed_if_no_admin_token_env(client, monkeypatch):
    # Explicitly remove the env var
    monkeypatch.delenv("LOWLLAMA_ADMIN_TOKEN", raising=False)
    
    headers = {"Authorization": "Bearer any-token"}
    response = client.get("/nodes", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access not configured"

def test_input_safety_join(client):
    # Test oversized invite code
    huge_code = "a" * 2000
    response = client.post("/join", json={
        "invite_code": huge_code,
        "node_name": "test",
        "ram_gb": 8.0,
        "rpc_port": 50052,
        "client_id": "client1"
    })
    # Invites.validate raises InviteError -> 400
    assert response.status_code == 400
    assert "not readable" in response.json()["detail"]

def test_input_safety_start_model(client, monkeypatch):
    monkeypatch.setenv("LOWLLAMA_ADMIN_TOKEN", "test-token")
    headers = {"Authorization": "Bearer test-token"}
    
    # Test impossible RAM selection or malformed node IDs (handled by registry/workers usually)
    # Here we check the API boundary
    response = client.post("/start", json={
        "model_id": "qwen2.5-0.5b-instruct-q4_k_m",
        "node_ids": ["non-existent-node"]
    }, headers=headers)
    # Should be 400 if node not found
    assert response.status_code == 400

def test_heartbeat_requires_correct_credentials(client, monkeypatch):
    # This involves registry state, which might be tricky in pure API test without mocks
    # but we can try to see if it rejects malformed IDs
    response = client.post("/heartbeat", json={
        "node_id": "fake",
        "credentials": "fake"
    })
    assert response.status_code == 401 # Registry raises NodeAuthError or returns False -> 401

def test_no_secret_leak_in_errors(client, monkeypatch):
    monkeypatch.setenv("LOWLLAMA_ADMIN_TOKEN", "secret-token")
    headers = {"Authorization": "Bearer wrong-token"}
    response = client.get("/nodes", headers=headers)
    
    # Error message should not contain the secret
    content = response.text
    assert "secret-token" not in content
    assert "wrong-token" not in content
