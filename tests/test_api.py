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
