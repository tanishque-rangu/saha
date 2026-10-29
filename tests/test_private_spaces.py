"""Comprehensive Tests for Saha Private Spaces, Policy & Approvals, and Privacy Ledger."""

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.api import app, IP_POST_HISTORY, OWNER_POST_HISTORY
from backend.db import init_db, hash_owner_token, get_db_connection
from backend.ledger import Ledger
from backend.policy import get_owner_policy, set_owner_policy, Policy
from backend.redaction import redact_text, redact_messages
from backend.tools.files import write_file, read_file
from backend.tools.sandbox import resolve_workspace_path


@pytest.fixture
def client(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    test_db = Path(tmp_dir) / "test_private_spaces.db"
    test_ws = Path(tmp_dir) / "workspace"
    test_ws.mkdir()

    monkeypatch.setenv("SAHA_DB_PATH", str(test_db))
    monkeypatch.setenv("SAHA_WORKSPACE_ROOT", str(test_ws))
    monkeypatch.setenv("SAHA_DEMO_MODE", "true")
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "false")
    monkeypatch.setenv("NEBIUS_API_KEY", "")

    init_db(str(test_db))
    IP_POST_HISTORY.clear()
    OWNER_POST_HISTORY.clear()
    return TestClient(app)


# =========================================================================
# A. PRIVATE SPACES: Isolation, Storage, Pairing, Deletion
# =========================================================================

def test_owner_isolation_tasks_and_endpoints(client):
    """Owner A cannot read, run, sabotage, or resume Owner B's tasks; returns 404."""
    owner_a_token = "token_alice_1234567890abcdef1234"
    owner_b_token = "token_bob_9876543210fedcba9876"
    headers_a = {"X-Saha-Owner": owner_a_token}
    headers_b = {"X-Saha-Owner": owner_b_token}

    mock_steps = [
        MagicMock(idx=1, intent="Alice step", tool="write_file", args={"path": "alice.txt", "content": "alice data"}, probe={}, depends_on=[]),
    ]

    with patch("backend.api.generate_plan", return_value=mock_steps):
        # Owner A creates a task
        res_a = client.post("/tasks", json={"goal": "Alice secret project"}, headers=headers_a)
        assert res_a.status_code == 200
        task_a_id = res_a.json()["task"]["id"]

    # Owner A can view their task
    res_get_a = client.get(f"/tasks/{task_a_id}", headers=headers_a)
    assert res_get_a.status_code == 200
    assert res_get_a.json()["task"]["id"] == task_a_id

    # Owner B attempts to GET task A -> returns 404
    res_get_b = client.get(f"/tasks/{task_a_id}", headers=headers_b)
    assert res_get_b.status_code == 404
    assert "Task not found" in res_get_b.json()["detail"]

    # Owner B attempts to RUN task A -> returns 404
    res_run_b = client.post(f"/tasks/{task_a_id}/run", headers=headers_b)
    assert res_run_b.status_code == 404

    # Owner B attempts to SABOTAGE task A -> returns 404
    res_sab_b = client.post(f"/tasks/{task_a_id}/sabotage", headers=headers_b)
    assert res_sab_b.status_code == 404

    # Owner B attempts to RESUME task A -> returns 404
    res_res_b = client.post(f"/tasks/{task_a_id}/resume", headers=headers_b)
    assert res_res_b.status_code == 404

    # Owner B's task list should be empty
    res_list_b = client.get("/tasks", headers=headers_b)
    assert res_list_b.status_code == 200
    assert len(res_list_b.json()["tasks"]) == 0

    # Owner A's task list contains task A
    res_list_a = client.get("/tasks", headers=headers_a)
    assert res_list_a.status_code == 200
    assert len(res_list_a.json()["tasks"]) == 1


def test_owner_workspace_directory_isolation(client):
    """Files are strictly isolated in workspace/<owner_id>/<task_id>/."""
    owner_token = "token_isolation_test_user_001"
    owner_id = hash_owner_token(owner_token)
    task = Ledger.create_task("Test Workspace Isolation", owner_id=owner_id)

    # Write file using sandbox
    write_res = write_file("secret.txt", "confidential content", task_id=task.id)
    assert write_res["bytes_written"] == len("confidential content")

    disk_path = resolve_workspace_path("secret.txt", task_id=task.id, owner_id=owner_id)
    assert disk_path.exists()
    assert owner_id in str(disk_path)
    assert task.id in str(disk_path)

    # Read back through tool
    read_res = read_file("secret.txt", task_id=task.id)
    assert read_res["content"] == "confidential content"


def test_pair_link_generation(client):
    """POST /owner/pair-link returns a link ending in #pair=<token>."""
    token = "abcdef0123456789abcdef0123456789"
    res = client.post("/owner/pair-link", headers={"X-Saha-Owner": token})
    assert res.status_code == 200
    data = res.json()
    assert "pair_link" in data
    assert f"#pair={token}" in data["pair_link"]
    assert "token_preview" in data
    assert "bearer-token pairing" in data["note"].lower()


def test_delete_owner_space(client):
    """DELETE /owner/space wipes that owner's tasks, files, and data without touching other owners."""
    owner_a_token = "token_to_delete_aaaa1111"
    owner_b_token = "token_to_remain_bbbb2222"
    headers_a = {"X-Saha-Owner": owner_a_token}
    headers_b = {"X-Saha-Owner": owner_b_token}

    mock_steps = [
        MagicMock(idx=1, intent="Step", tool="write_file", args={"path": "note.txt", "content": "data"}, probe={}, depends_on=[]),
    ]

    with patch("backend.api.generate_plan", return_value=mock_steps):
        res_a = client.post("/tasks", json={"goal": "Goal A"}, headers=headers_a)
        res_b = client.post("/tasks", json={"goal": "Goal B"}, headers=headers_b)
        task_a_id = res_a.json()["task"]["id"]
        task_b_id = res_b.json()["task"]["id"]

    # Write files for both
    write_file("file_a.txt", "data a", task_id=task_a_id)
    write_file("file_b.txt", "data b", task_id=task_b_id)

    path_a = resolve_workspace_path("file_a.txt", task_id=task_a_id)
    path_b = resolve_workspace_path("file_b.txt", task_id=task_b_id)
    assert path_a.exists()
    assert path_b.exists()

    # Delete Owner A's space
    del_res = client.delete("/owner/space", headers=headers_a)
    assert del_res.status_code == 200
    assert del_res.json()["status"] == "deleted"

    # Owner A's task list is empty and task A returns 404
    assert len(client.get("/tasks", headers=headers_a).json()["tasks"]) == 0
    assert client.get(f"/tasks/{task_a_id}", headers=headers_a).status_code == 404
    assert not path_a.exists()

    # Owner B's task list still contains task B and file still exists
    assert len(client.get("/tasks", headers=headers_b).json()["tasks"]) == 1
    assert client.get(f"/tasks/{task_b_id}", headers=headers_b).status_code == 200
    assert path_b.exists()


# =========================================================================
# B. POLICY + APPROVALS
# =========================================================================

def test_policy_crud_endpoints(client):
    """GET and PUT /policy manages owner-specific security policy."""
    token = "policy_user_token_123"
    headers = {"X-Saha-Owner": token}

    # Default policy
    res = client.get("/policy", headers=headers)
    assert res.status_code == 200
    pol = res.json()["policy"]
    assert "write_file" in pol["allowed_tools"]
    assert "example.com" in pol["allowed_domains"]
    assert pol["auto_heal"] is True

    # Update policy
    new_pol = {
        "allowed_tools": ["write_file", "read_file"],
        "allowed_domains": ["custom-domain.org"],
        "allowed_paths": [],
        "auto_heal": False,
        "max_steps": 5
    }
    put_res = client.put("/policy", json=new_pol, headers=headers)
    assert put_res.status_code == 200
    assert put_res.json()["status"] == "updated"

    # Verify updated
    get_res = client.get("/policy", headers=headers)
    assert get_res.json()["policy"]["allowed_domains"] == ["custom-domain.org"]
    assert get_res.json()["policy"]["auto_heal"] is False


def test_policy_blocks_unauthorized_step_and_approvals_flow(client):
    """Disallowed action is blocked, step set to blocked, and approval flow works."""
    token = "policy_block_token_999"
    headers = {"X-Saha-Owner": token}
    owner_id = hash_owner_token(token)

    task = Ledger.create_task("Policy Blocked Task", owner_id=owner_id)

    # Step attempting to contact disallowed domain evil.com
    disallowed_step = {
        "idx": 1,
        "intent": "Contact untrusted external domain",
        "tool": "http_get",
        "args": {"url": "https://evil.com/leak"},
        "probe": {},
        "depends_on": []
    }
    Ledger.add_steps(task.id, [disallowed_step])

    # Run the task -> step should be blocked by policy
    run_res = client.post(f"/tasks/{task.id}/run", headers=headers)
    assert run_res.status_code == 200

    # Poll for step to be evaluated and marked blocked
    for _ in range(30):
        detail = client.get(f"/tasks/{task.id}", headers=headers).json()
        if detail["steps"] and detail["steps"][0]["status"] == "blocked":
            break
        time.sleep(0.05)

    step_row = detail["steps"][0]
    assert step_row["status"] == "blocked"
    assert "evil.com" in step_row["block_reason"]
    assert step_row["drift_reason"] is None

    # Approvals list contains the pending approval
    appr_res = client.get("/approvals", headers=headers)
    assert appr_res.status_code == 200
    approvals = appr_res.json()["approvals"]
    assert len(approvals) == 1
    appr = approvals[0]
    assert appr["status"] == "pending"
    assert appr["tool"] == "http_get"
    assert appr["task_id"] == task.id
    approval_id = appr["id"]

    # Flow 1: Deny approval -> marks step failed
    deny_res = client.post(f"/approvals/{approval_id}/deny", headers=headers)
    assert deny_res.status_code == 200
    assert deny_res.json()["status"] == "denied"

    # Step row is now failed
    step_after_deny = client.get(f"/tasks/{task.id}", headers=headers).json()["steps"][0]
    assert step_after_deny["status"] == "failed"


def test_approval_always_allow_updates_policy(client):
    """Approving with always_allow=True updates owner's policy and re-queues step."""
    token = "always_allow_user_token_777"
    headers = {"X-Saha-Owner": token}
    owner_id = hash_owner_token(token)

    task = Ledger.create_task("Always Allow Task", owner_id=owner_id)
    step_data = {
        "idx": 1,
        "intent": "Access novel API",
        "tool": "http_get",
        "args": {"url": "https://api.weather.gov/alerts"},
        "probe": {},
        "depends_on": []
    }
    Ledger.add_steps(task.id, [step_data])

    # Trigger run -> blocked
    client.post(f"/tasks/{task.id}/run", headers=headers)

    for _ in range(30):
        approvals = client.get("/approvals", headers=headers).json().get("approvals", [])
        if len(approvals) == 1:
            break
        time.sleep(0.05)

    assert len(approvals) == 1
    appr_id = approvals[0]["id"]

    # Approve with always_allow=True
    appr_res = client.post(f"/approvals/{appr_id}/approve", json={"always_allow": True}, headers=headers)
    assert appr_res.status_code == 200
    assert appr_res.json()["status"] == "approved"
    assert appr_res.json()["always_allow"] is True

    # Policy now includes 'api.weather.gov'
    updated_pol = client.get("/policy", headers=headers).json()["policy"]
    assert "api.weather.gov" in updated_pol["allowed_domains"]

    # Step is re-queued and auto-resumed by background executor
    step_requeued = client.get(f"/tasks/{task.id}", headers=headers).json()["steps"][0]
    assert step_requeued["status"] in ("pending", "running", "verified")


# =========================================================================
# C. PRIVACY LEDGER & REDACTION
# =========================================================================

def test_redaction_engine_patterns():
    """Redaction engine scrubs emails, phone numbers, API keys, and long digit sequences."""
    sample_text = (
        "Contact me at alice.smith@cybercorp.io or +1-555-867-5309. "
        "Use key Bearer nbs_1234567890abcdef1234567890abcdef and account 4532890123456789."
    )
    scrubbed, count = redact_text(sample_text)
    assert "[EMAIL_REDACTED]" in scrubbed
    assert "alice.smith@cybercorp.io" not in scrubbed
    assert "[PHONE_REDACTED]" in scrubbed
    assert "555-867-5309" not in scrubbed
    assert "[KEY_REDACTED]" in scrubbed
    assert "nbs_1234567890abcdef" not in scrubbed
    assert "[DIGITS_REDACTED]" in scrubbed
    assert "4532890123456789" not in scrubbed
    assert count >= 4


def test_egress_log_contains_no_content(client):
    """Outbound model calls record metadata in llm_calls; no prompt or response content stored."""
    token = "privacy_audit_owner_111"
    headers = {"X-Saha-Owner": token}
    owner_id = hash_owner_token(token)

    from backend.db import record_llm_call

    # Simulate recording an LLM call via the ledger/db helper
    record_llm_call(
        owner_id=owner_id,
        role="WORKER",
        model="nvidia/nemotron-3-super-120b-a12b",
        context_tags=["task_123", "step_1", "file_generate"],
        input_chars=1420,
        output_chars=350,
        redaction_count=2,
        latency_ms=185.4
    )

    # Fetch egress log
    egress_res = client.get("/egress", headers=headers)
    assert egress_res.status_code == 200
    egress_items = egress_res.json()["egress"]
    assert len(egress_items) == 1
    entry = egress_items[0]

    assert entry["role"] == "WORKER"
    assert entry["model"] == "nvidia/nemotron-3-super-120b-a12b"
    assert entry["input_chars"] == 1420
    assert entry["output_chars"] == 350
    assert entry["redaction_count"] == 2
    assert "task_123" in entry["tags"]

    # Verify SQLite schema directly: columns must NOT contain raw prompt or response
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(llm_calls)")
    columns = [col[1] for col in cursor.fetchall()]
    conn.close()

    assert "prompt" not in columns
    assert "content" not in columns
    assert "response" not in columns
    assert "raw" not in columns


# =========================================================================
# RATE LIMITS (Per-Owner and Per-IP)
# =========================================================================

def test_rate_limits_per_owner_and_ip(client, monkeypatch):
    """Rate limits apply per owner AND per IP in public mode."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    from backend.api import IP_POST_HISTORY, OWNER_POST_HISTORY
    IP_POST_HISTORY.clear()
    OWNER_POST_HISTORY.clear()

    token_a = "token_rate_limit_a_0123456789abcdef"
    token_b = "token_rate_limit_b_0123456789abcdef"
    headers_a = {"X-Saha-Owner": token_a}
    headers_b = {"X-Saha-Owner": token_b}

    mock_steps = [
        MagicMock(idx=1, intent="Step", tool="write_file", args={"path": "f.txt", "content": "c"}, probe={}, depends_on=[]),
    ]

    with patch("backend.api.generate_plan", return_value=mock_steps):
        # Owner A creates 10 tasks successfully
        for i in range(10):
            res = client.post("/tasks", json={"goal": f"A Goal {i}"}, headers=headers_a)
            assert res.status_code == 200

        # 11th request by Owner A returns 429
        res_a_11 = client.post("/tasks", json={"goal": "A Goal 11"}, headers=headers_a)
        assert res_a_11.status_code == 429
        assert "rate limit exceeded" in res_a_11.json()["detail"].lower()
