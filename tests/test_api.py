"""FastAPI Endpoint Tests for Saha API."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.api import app
from backend.db import init_db
from backend.ledger import Ledger
from backend.tools.files import write_file
from backend.tools.sandbox import resolve_workspace_path


@pytest.fixture
def client(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    test_db = Path(tmp_dir) / "test_api.db"
    test_ws = Path(tmp_dir) / "workspace"
    test_ws.mkdir()

    monkeypatch.setenv("SAHA_DB_PATH", str(test_db))
    monkeypatch.setenv("SAHA_WORKSPACE_ROOT", str(test_ws))
    monkeypatch.setenv("SAHA_DEMO_MODE", "true")
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "false")

    init_db(str(test_db))
    return TestClient(app)


def test_health_endpoint(client):
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "ok"
    assert data["service"] == "Saha"
    assert "public_mode" in data
    assert "demo_mode" in data


def test_metrics_endpoint(client):
    res = client.get("/metrics")
    assert res.status_code == 200
    data = res.json()
    assert "total_calls" in data
    assert "models" in data


def test_task_crud_and_detail(client):
    # Mock generate_plan to avoid external LLM during quick unit test
    mock_steps = [
        MagicMock(idx=1, intent="Write test file", tool="write_file", args={"path": "test.txt", "content_brief": "hello"}, probe={"expected_file": "test.txt"}, depends_on=[]),
        MagicMock(idx=2, intent="Read test file", tool="read_file", args={"path": "test.txt"}, probe={}, depends_on=[1]),
    ]
    with patch("backend.api.generate_plan", return_value=mock_steps):
        res = client.post("/tasks", json={"goal": "Test API task creation"})
        assert res.status_code == 200
        data = res.json()
        assert "task" in data
        task_id = data["task"]["id"]

        # List tasks
        list_res = client.get("/tasks")
        assert list_res.status_code == 200
        assert len(list_res.json()["tasks"]) >= 1

        # Get task detail
        detail_res = client.get(f"/tasks/{task_id}")
        assert detail_res.status_code == 200
        detail = detail_res.json()
        assert detail["task"]["id"] == task_id
        assert len(detail["steps"]) == 2
        assert detail["steps"][0]["depends_on"] == []
        assert detail["steps"][1]["depends_on"] == [1]


def test_sabotage_endpoint(client):
    task = Ledger.create_task("Test Sabotage Endpoint")
    steps_data = [
        {"idx": 1, "intent": "Write artifact", "tool": "write_file", "args": {"path": "artifact.txt", "content": "vital data"}, "probe": {}},
    ]
    Ledger.add_steps(task.id, steps_data)

    # Actually write file to disk
    write_file("artifact.txt", "vital data", task_id=task.id)
    disk_path = resolve_workspace_path("artifact.txt", task_id=task.id)
    assert disk_path.exists()

    # Call sabotage endpoint
    res = client.post(f"/tasks/{task.id}/sabotage")
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "sabotaged"
    assert data["deleted_file"] == "artifact.txt"
    assert "path" not in data
    assert "message" in data
    assert not disk_path.exists()

    # Verify ledger was NOT touched
    detail = client.get(f"/tasks/{task.id}").json()
    assert detail["task"]["status"] == "planning"


def test_public_mode_goal_limit(client, monkeypatch):
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    headers = {"X-Saha-Owner": "test_public_mode_token_32_characters_1"}
    long_goal = "X" * 305
    res = client.post("/tasks", json={"goal": long_goal}, headers=headers)
    assert res.status_code == 422
    assert "300 characters" in res.json()["detail"]


def test_public_mode_ip_rate_limit(client, monkeypatch):
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    headers = {"X-Saha-Owner": "test_public_mode_token_32_characters_2"}
    from backend.api import IP_POST_HISTORY, OWNER_POST_HISTORY
    IP_POST_HISTORY.clear()
    OWNER_POST_HISTORY.clear()

    mock_steps = [
        MagicMock(idx=1, intent="Step", tool="write_file", args={"path": "f.txt", "content": "c"}, probe={}, depends_on=[]),
    ]
    with patch("backend.api.generate_plan", return_value=mock_steps):
        # 10 creations should succeed
        for i in range(10):
            res = client.post("/tasks", json={"goal": f"Goal {i}"}, headers=headers)
            assert res.status_code == 200

        # 11th creation must return 429
        res11 = client.post("/tasks", json={"goal": "Goal 11"}, headers=headers)
        assert res11.status_code == 429
        assert "rate limit exceeded" in res11.json()["detail"].lower()


def test_sabotage_disabled_when_not_demo(client, monkeypatch):
    monkeypatch.setenv("SAHA_DEMO_MODE", "false")
    task = Ledger.create_task("Test Non Demo Sabotage")
    res = client.post(f"/tasks/{task.id}/sabotage")
    assert res.status_code == 403


def test_proxy_forwarded_for_ip_rate_limiting_separate_buckets(tmp_path, monkeypatch):
    """With FORWARDED_ALLOW_IPS=* and X-Forwarded-For set, Uvicorn updates request.client.host,
    ensuring distinct forwarded IPs receive isolated rate limit buckets through the real ASGI stack."""
    import json
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request
    import uvicorn
    from backend.api import app, IP_POST_HISTORY, OWNER_POST_HISTORY
    from backend.db import init_db

    db_path = str(tmp_path / "proxy_test.db")
    ws_path = str(tmp_path / "workspace")
    monkeypatch.setenv("SAHA_DB_PATH", db_path)
    monkeypatch.setenv("SAHA_WORKSPACE_ROOT", ws_path)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    monkeypatch.setenv("SAHA_DEMO_MODE", "true")
    monkeypatch.setenv("SAHA_WATCHER_ENABLED", "false")
    monkeypatch.setenv("NEBIUS_API_KEY", "")

    init_db(db_path)
    IP_POST_HISTORY.clear()
    OWNER_POST_HISTORY.clear()

    # Find free port
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    mock_step = MagicMock(idx=1, intent="Test", tool="write_file", args={"path": "a.txt", "content": "1"}, probe={}, depends_on=[])

    with patch("backend.api.generate_plan", return_value=[mock_step]):
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", forwarded_allow_ips="*")
        server = uvicorn.Server(config)
        t = threading.Thread(target=server.run, daemon=True)
        t.start()

        try:
            # Wait for server ready
            deadline = time.time() + 5.0
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as r:
                        if r.status == 200:
                            break
                except Exception:
                    time.sleep(0.05)

            # 1. Send 10 tasks from IP 198.51.100.1 with 10 different owners
            for i in range(10):
                owner = f"owner_fwd1_{i:02d}_12345678901234567890"
                data = json.dumps({"goal": f"Task {i}"}).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/tasks",
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "X-Saha-Owner": owner,
                        "X-Forwarded-For": "198.51.100.1",
                    },
                )
                with urllib.request.urlopen(req) as resp:
                    assert resp.status == 200

            # 2. 11th request from IP 198.51.100.1 must be rate limited (429)
            owner_11 = "owner_fwd1_11_12345678901234567890"
            data_11 = json.dumps({"goal": "Task 11"}).encode("utf-8")
            req_11 = urllib.request.Request(
                f"http://127.0.0.1:{port}/tasks",
                data=data_11,
                headers={
                    "Content-Type": "application/json",
                    "X-Saha-Owner": owner_11,
                    "X-Forwarded-For": "198.51.100.1",
                },
            )
            got_429 = False
            try:
                urllib.request.urlopen(req_11)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    got_429 = True
            assert got_429, "Expected 429 for 11th request from IP 198.51.100.1"

            # 3. Request from IP 198.51.100.2 must succeed (separate IP bucket)
            owner_ip2 = "owner_fwd2_01_12345678901234567890"
            data_ip2 = json.dumps({"goal": "Task IP2"}).encode("utf-8")
            req_ip2 = urllib.request.Request(
                f"http://127.0.0.1:{port}/tasks",
                data=data_ip2,
                headers={
                    "Content-Type": "application/json",
                    "X-Saha-Owner": owner_ip2,
                    "X-Forwarded-For": "198.51.100.2",
                },
            )
            with urllib.request.urlopen(req_ip2) as resp_ip2:
                assert resp_ip2.status == 200
        finally:
            server.should_exit = True
            t.join(timeout=3.0)
