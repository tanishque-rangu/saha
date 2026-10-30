import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.api import app, IP_POST_HISTORY, OWNER_POST_HISTORY
from backend.db import init_db, hash_owner_token, get_db
from backend.ledger import Ledger
from backend.policy import get_owner_policy, set_owner_policy, Policy
from backend.tools.files import write_file, read_file
from backend.tools.sandbox import resolve_workspace_path, get_workspace_dir


@pytest.fixture
def test_setup(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    test_db = Path(tmp_dir) / "test_isolation.db"
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
    client = TestClient(app)
    return {
        "client": client,
        "db": str(test_db),
        "ws": str(test_ws),
        "tmp_dir": tmp_dir,
    }


def test_two_separate_owners_cannot_list_or_read_each_others_tasks_or_file_output(test_setup):
    """Regression: Two separate anonymous/private owners cannot list or read each other's tasks or file output."""
    client = test_setup["client"]
    db_path = test_setup["db"]

    token_a = "0123456789abcdef0123456789abcdef"  # Owner A token (32 chars)
    token_b = "fedcba9876543210fedcba9876543210"  # Owner B token (32 chars)
    owner_a_id = hash_owner_token(token_a)
    owner_b_id = hash_owner_token(token_b)

    # 1. Owner A creates Task A with a step that writes and reads a private file
    task_a = Ledger.create_task(
        goal="Owner A confidential processing",
        owner_id=owner_a_id,
        db_path=db_path,
    )

    steps_a = Ledger.add_steps(
        task_a.id,
        [
            {
                "idx": 1,
                "intent": "Write secret file A",
                "tool": "write_file",
                "args": {"path": "secret_a.txt", "content": "CONFIDENTIAL_PAYROLL_DATA_A"},
            },
            {
                "idx": 2,
                "intent": "Read secret file A",
                "tool": "read_file",
                "args": {"path": "secret_a.txt"},
            },
        ],
        db_path=db_path,
    )
    Ledger.mark_step_done(
        steps_a[0].id,
        {"path": "secret_a.txt", "bytes_written": 28, "sha256": "hash_a1"},
        db_path=db_path,
    )
    Ledger.mark_step_verified(steps_a[0].id, {"probe_type": "file_probe"}, db_path=db_path)

    Ledger.mark_step_done(
        steps_a[1].id,
        {
            "path": "secret_a.txt",
            "content": "CONFIDENTIAL_PAYROLL_DATA_A",
            "bytes_read": 28,
            "sha256": "hash_a1",
        },
        db_path=db_path,
    )
    Ledger.mark_step_verified(steps_a[1].id, {"probe_type": "file_probe"}, db_path=db_path)
    Ledger.update_task_status(task_a.id, "complete", db_path=db_path)

    # Actually write the file inside Owner A's workspace
    ws_dir_a = get_workspace_dir(task_id=task_a.id, owner_id=owner_a_id)
    (ws_dir_a / "secret_a.txt").write_text("CONFIDENTIAL_PAYROLL_DATA_A", encoding="utf-8")

    # 2. Owner B lists tasks
    res_b_list = client.get("/tasks", headers={"X-Saha-Owner": token_b})
    assert res_b_list.status_code == 200
    b_tasks = res_b_list.json().get("tasks", [])
    assert not any(t["id"] == task_a.id for t in b_tasks), "Owner A's task must NOT appear in Owner B's task list"

    # 3. Owner B attempts to GET Task A detail
    res_b_get = client.get(f"/tasks/{task_a.id}", headers={"X-Saha-Owner": token_b})
    assert res_b_get.status_code == 404, "Owner B must receive 404 Not Found when attempting to access Owner A's task"
    assert "CONFIDENTIAL_PAYROLL_DATA_A" not in res_b_get.text

    # 4. Owner B attempts to read Owner A's workspace file via sandbox
    ws_dir_b = get_workspace_dir(task_id="task_b_dummy", owner_id=owner_b_id)
    target_in_b = resolve_workspace_path("secret_a.txt", task_id="task_b_dummy", owner_id=owner_b_id)
    assert not target_in_b.exists(), "Owner A's file must not exist in Owner B's workspace"
    with pytest.raises(FileNotFoundError):
        read_file("secret_a.txt", task_id="task_b_dummy")

    # 5. Owner B attempts to mutate, run, or sabotage Owner A's task
    assert client.post(f"/tasks/{task_a.id}/run", headers={"X-Saha-Owner": token_b}).status_code == 404
    assert client.post(f"/tasks/{task_a.id}/resume", headers={"X-Saha-Owner": token_b}).status_code == 404
    assert client.post(f"/tasks/{task_a.id}/sabotage", headers={"X-Saha-Owner": token_b}).status_code == 404

    # 6. Verify Owner A can still list and read their own task and content
    res_a_list = client.get("/tasks", headers={"X-Saha-Owner": token_a})
    assert res_a_list.status_code == 200
    a_tasks = res_a_list.json().get("tasks", [])
    assert any(t["id"] == task_a.id for t in a_tasks)

    res_a_get = client.get(f"/tasks/{task_a.id}", headers={"X-Saha-Owner": token_a})
    assert res_a_get.status_code == 200
    assert "CONFIDENTIAL_PAYROLL_DATA_A" in res_a_get.text


def test_public_mode_rejects_missing_or_invalid_tokens_while_valid_tokens_work(test_setup, monkeypatch):
    """Regression: Public mode rejects missing or invalid owner tokens (<32 chars), while valid paired tokens work."""
    client = test_setup["client"]
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")

    # 1. Missing X-Saha-Owner header in public mode -> 401
    res_no_header = client.get("/tasks")
    assert res_no_header.status_code == 401
    assert "Unauthorized" in res_no_header.json().get("detail", "")

    assert client.post("/tasks", json={"goal": "Test goal"}).status_code == 401
    assert client.get("/policy").status_code == 401
    assert client.get("/activity").status_code == 401
    assert client.get("/memories").status_code == 401
    assert client.get("/skills").status_code == 401

    # 2. Invalid / short tokens (< 32 characters) -> 401
    invalid_tokens = [
        "default-anonymous-owner",  # 23 chars - the legacy shared token
        "short_token_123",          # 15 chars
        "a" * 31,                   # 31 chars
        "",                         # empty
        "   ",                      # whitespace
    ]
    for bad_token in invalid_tokens:
        res = client.get("/tasks", headers={"X-Saha-Owner": bad_token})
        assert res.status_code == 401, f"Token '{bad_token}' (len={len(bad_token)}) should be rejected with 401 in public mode"

    # 3. Valid tokens (>= 32 characters, e.g. 32-char hex token or 64-char paired token) -> 200
    valid_token_32 = "e4c8f293a10b4c5d8e7f123456789abc"
    res_valid = client.get("/tasks", headers={"X-Saha-Owner": valid_token_32})
    assert res_valid.status_code == 200
    assert "tasks" in res_valid.json()

    res_policy = client.get("/policy", headers={"X-Saha-Owner": valid_token_32})
    assert res_policy.status_code == 200

    valid_token_64 = "a" * 64
    res_paired = client.get("/tasks", headers={"X-Saha-Owner": valid_token_64})
    assert res_paired.status_code == 200


def test_demo_sample_data_cannot_reveal_records_from_real_user_workspace(test_setup):
    """Regression: Demo sample data cannot reveal records from a real user's workspace."""
    client = test_setup["client"]
    db_path = test_setup["db"]

    real_user_token = "real_user_private_token_32chars1"
    real_owner_id = hash_owner_token(real_user_token)

    demo_token = "demo_synthetic_visitor_token_123"
    demo_owner_id = hash_owner_token(demo_token)

    # 1. Real user has real private task and workspace files
    real_task = Ledger.create_task(
        goal="Private corporate acquisition analysis",
        owner_id=real_owner_id,
        db_path=db_path,
    )
    steps_real = Ledger.add_steps(
        real_task.id,
        [
            {
                "idx": 1,
                "intent": "Read sensitive balance sheet",
                "tool": "read_file",
                "args": {"path": "balance_sheet.csv"},
            }
        ],
        db_path=db_path,
    )
    Ledger.mark_step_done(
        steps_real[0].id,
        {
            "path": "balance_sheet.csv",
            "content": "Company,Valuation,CashOnHand\nAcmeCorp,$500M,$45M",
            "bytes_read": 52,
        },
        db_path=db_path,
    )
    Ledger.mark_step_verified(steps_real[0].id, {"probe_type": "file_probe"}, db_path=db_path)
    Ledger.update_task_status(real_task.id, "complete", db_path=db_path)

    # Write file into real user's workspace
    real_ws = get_workspace_dir(task_id=real_task.id, owner_id=real_owner_id)
    (real_ws / "balance_sheet.csv").write_text("Company,Valuation,CashOnHand\nAcmeCorp,$500M,$45M", encoding="utf-8")

    # 2. Demo space has synthetic sample tasks (clearly labeled [Synthetic Demo])
    demo_task = Ledger.create_task(
        goal="[Synthetic Demo] Demo data summarization workflow",
        owner_id=demo_owner_id,
        db_path=db_path,
    )
    steps_demo = Ledger.add_steps(
        demo_task.id,
        [
            {
                "idx": 1,
                "intent": "Read public sample facts",
                "tool": "read_file",
                "args": {"path": "sample_facts.txt"},
            }
        ],
        db_path=db_path,
    )
    Ledger.mark_step_done(
        steps_demo[0].id,
        {
            "path": "sample_facts.txt",
            "content": "Hyderabad is known as the City of Pearls.",
            "bytes_read": 41,
        },
        db_path=db_path,
    )
    Ledger.mark_step_verified(steps_demo[0].id, {"probe_type": "file_probe"}, db_path=db_path)
    Ledger.update_task_status(demo_task.id, "complete", db_path=db_path)

    # 3. Querying with demo token
    res_demo_tasks = client.get("/tasks", headers={"X-Saha-Owner": demo_token})
    assert res_demo_tasks.status_code == 200
    demo_tasks_list = res_demo_tasks.json().get("tasks", [])
    
    # Must only contain the demo task, never the real user's task
    demo_ids = [t["id"] for t in demo_tasks_list]
    assert demo_task.id in demo_ids
    assert real_task.id not in demo_ids, "Demo token MUST NOT reveal real user's task ID"
    assert "Acquisition" not in res_demo_tasks.text
    assert "Valuation" not in res_demo_tasks.text

    # Direct task detail access with demo token returns 404
    res_direct = client.get(f"/tasks/{real_task.id}", headers={"X-Saha-Owner": demo_token})
    assert res_direct.status_code == 404
    assert "AcmeCorp" not in res_direct.text

    # 4. Deleting demo space does not affect real user's tasks or files
    del_res = client.delete("/owner/space", headers={"X-Saha-Owner": demo_token})
    assert del_res.status_code == 200

    # Real user's task still exists and is accessible by real user
    res_real = client.get(f"/tasks/{real_task.id}", headers={"X-Saha-Owner": real_user_token})
    assert res_real.status_code == 200
    assert "AcmeCorp" in res_real.text
    assert (real_ws / "balance_sheet.csv").exists()


def test_legacy_default_owner_tasks_not_exposed_to_anonymous_visitors(test_setup):
    """Regression: Legacy tasks with owner_id = 'default' are never exposed to new anonymous/private visitors."""
    client = test_setup["client"]
    db_path = test_setup["db"]

    # Insert a legacy task with owner_id = 'default'
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO tasks (id, owner_id, goal, status, created_at, updated_at)
            VALUES ('legacy-task-1', 'default', 'Legacy task created before owner isolation', 'complete', datetime('now'), datetime('now'))
            """
        )

    # A new visitor arrives with a fresh random 32-character token
    visitor_token = "fedcba0123456789fedcba0123456789"
    res_tasks = client.get("/tasks", headers={"X-Saha-Owner": visitor_token})
    assert res_tasks.status_code == 200
    tasks = res_tasks.json().get("tasks", [])
    assert len(tasks) == 0, "Visitor must see an empty space, not legacy default tasks"

    # Direct lookup with visitor token returns 404
    res_get = client.get("/tasks/legacy-task-1", headers={"X-Saha-Owner": visitor_token})
    assert res_get.status_code == 404


def test_token_rotation_revokes_old_token_and_preserves_tasks_and_files(test_setup):
    """Regression: Rotating an owner token revokes old token and preserves access to tasks and files."""
    import secrets
    client = test_setup["client"]
    db_path = test_setup["db"]

    token_old = secrets.token_hex(16)
    owner_id = hash_owner_token(token_old)

    # 1. Create task and workspace file under initial token
    task = Ledger.create_task("Task before token rotation", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {"idx": 1, "intent": "Read financial ledger", "tool": "read_file", "args": {"path": "financials.txt"}},
        ],
        db_path=db_path,
    )
    Ledger.mark_step_done(
        steps[0].id,
        {"path": "financials.txt", "content": "PRESERVED_ACCOUNT_BALANCE_$500K", "bytes_read": 32, "sha256": "h_fin"},
        db_path=db_path,
    )
    Ledger.mark_step_verified(steps[0].id, {"probe_type": "file_probe"}, db_path=db_path)
    Ledger.update_task_status(task.id, "complete", db_path=db_path)

    ws_dir = get_workspace_dir(task_id=task.id, owner_id=owner_id)
    (ws_dir / "financials.txt").write_text("PRESERVED_ACCOUNT_BALANCE_$500K", encoding="utf-8")

    # Verify task is readable with token_old
    res_before = client.get(f"/tasks/{task.id}", headers={"X-Saha-Owner": token_old})
    assert res_before.status_code == 200
    assert "PRESERVED_ACCOUNT_BALANCE_$500K" in res_before.text

    # 2. Rotate token to a new secret token
    token_new = secrets.token_hex(16)
    res_rotate = client.post("/owner/rotate", headers={"X-Saha-Owner": token_old}, json={"new_token": token_new})
    assert res_rotate.status_code == 200
    data_rotate = res_rotate.json()
    assert data_rotate["status"] == "rotated"
    assert data_rotate["owner_id"] == owner_id

    # 3. Prove OLD token is now strictly REJECTED (revoked)
    res_old_list = client.get("/tasks", headers={"X-Saha-Owner": token_old})
    assert res_old_list.status_code == 401
    assert "revoked" in res_old_list.json().get("detail", "").lower()

    res_old_get = client.get(f"/tasks/{task.id}", headers={"X-Saha-Owner": token_old})
    assert res_old_get.status_code == 401
    assert "revoked" in res_old_get.json().get("detail", "").lower()

    res_old_post = client.post("/tasks", headers={"X-Saha-Owner": token_old}, json={"goal": "New task"})
    assert res_old_post.status_code == 401

    res_old_policy = client.get("/policy", headers={"X-Saha-Owner": token_old})
    assert res_old_policy.status_code == 401

    # 4. Prove NEW token can access the preserved task and file output
    res_new_list = client.get("/tasks", headers={"X-Saha-Owner": token_new})
    assert res_new_list.status_code == 200
    tasks_new = res_new_list.json().get("tasks", [])
    assert any(t["id"] == task.id for t in tasks_new)

    res_new_get = client.get(f"/tasks/{task.id}", headers={"X-Saha-Owner": token_new})
    assert res_new_get.status_code == 200
    assert "PRESERVED_ACCOUNT_BALANCE_$500K" in res_new_get.text

    # 5. Prove cannot rotate an already revoked token
    token_extra = secrets.token_hex(16)
    res_re_rotate = client.post("/owner/rotate", headers={"X-Saha-Owner": token_old}, json={"new_token": token_extra})
    assert res_re_rotate.status_code == 401


def test_token_revocation_without_rotation_stops_authentication(test_setup):
    """Regression: Direct revocation stops authentication while keeping task data intact."""
    import secrets
    client = test_setup["client"]
    db_path = test_setup["db"]

    token = secrets.token_hex(16)
    owner_id = hash_owner_token(token)
    task = Ledger.create_task("Task to preserve after revocation", owner_id=owner_id, db_path=db_path)

    # Revoke
    res_revoke = client.post("/owner/revoke", headers={"X-Saha-Owner": token})
    assert res_revoke.status_code == 200
    assert res_revoke.json()["status"] == "revoked"

    # Token is now rejected
    assert client.get("/tasks", headers={"X-Saha-Owner": token}).status_code == 401
    assert client.get(f"/tasks/{task.id}", headers={"X-Saha-Owner": token}).status_code == 401

    # Database row was preserved, not deleted
    preserved = Ledger.get_task(task.id, db_path=db_path)
    assert preserved is not None
    assert preserved.id == task.id


def test_public_mode_rotation_enforces_minimum_token_length(test_setup, monkeypatch):
    """Regression: In public mode, token rotation rejects short tokens (<32 chars) and accepts 32+ chars."""
    import secrets
    client = test_setup["client"]
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")

    token_init = secrets.token_hex(16)
    assert len(token_init) == 32

    # Attempt to rotate to short token (< 32 chars)
    res_bad = client.post("/owner/rotate", headers={"X-Saha-Owner": token_init}, json={"new_token": "too_short_tok"})
    assert res_bad.status_code == 400
    assert "minimum 32 characters" in res_bad.json().get("detail", "")

    # Auto-generation of new token in public mode produces valid 32-char token
    res_auto = client.post("/owner/rotate", headers={"X-Saha-Owner": token_init}, json={})
    assert res_auto.status_code == 200
    assert res_auto.json()["status"] == "rotated"

    # Old token is rejected
    assert client.get("/tasks", headers={"X-Saha-Owner": token_init}).status_code == 401


def test_failed_rotation_leaves_old_identity_and_data_usable(test_setup):
    """Regression: A failed rotation transaction leaves the old identity and data usable."""
    import secrets
    client = test_setup["client"]
    db_path = test_setup["db"]

    token_old = secrets.token_hex(16)
    owner_id = hash_owner_token(token_old)

    task = Ledger.create_task("Task with intact identity", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {"idx": 1, "intent": "Read status", "tool": "read_file", "args": {"path": "status.txt"}},
        ],
        db_path=db_path,
    )
    Ledger.mark_step_done(
        steps[0].id,
        {"path": "status.txt", "content": "STATUS_VITAL_OK", "bytes_read": 15},
        db_path=db_path,
    )
    Ledger.mark_step_verified(steps[0].id, {"probe_type": "file_probe"}, db_path=db_path)
    Ledger.update_task_status(task.id, "complete", db_path=db_path)

    # 1. Attempt rotation to identical token -> 400
    res_same = client.post("/owner/rotate", headers={"X-Saha-Owner": token_old}, json={"new_token": token_old})
    assert res_same.status_code == 400

    # 2. Old token must NOT be revoked and must still access its data
    res_list = client.get("/tasks", headers={"X-Saha-Owner": token_old})
    assert res_list.status_code == 200
    assert any(t["id"] == task.id for t in res_list.json()["tasks"])

    res_detail = client.get(f"/tasks/{task.id}", headers={"X-Saha-Owner": token_old})
    assert res_detail.status_code == 200
    assert "STATUS_VITAL_OK" in res_detail.text


def test_database_init_and_restart_does_not_undo_revocation(test_setup, monkeypatch):
    """Regression: Server restart and init_db() execution do not undo token revocation."""
    import secrets
    client = test_setup["client"]
    db_path = test_setup["db"]

    token_old = secrets.token_hex(16)
    token_new = secrets.token_hex(16)
    owner_id = hash_owner_token(token_old)

    task = Ledger.create_task("Task to survive restart", owner_id=owner_id, db_path=db_path)

    # Rotate token
    res_rotate = client.post("/owner/rotate", headers={"X-Saha-Owner": token_old}, json={"new_token": token_new})
    assert res_rotate.status_code == 200

    # Simulate app restart: re-run init_db() and create a new client
    init_db(db_path)
    restarted_client = TestClient(app)

    # Old token remains strictly revoked after restart
    res_old = restarted_client.get("/tasks", headers={"X-Saha-Owner": token_old})
    assert res_old.status_code == 401
    assert "revoked" in res_old.json().get("detail", "").lower()

    # New token remains active and accesses task after restart
    res_new = restarted_client.get(f"/tasks/{task.id}", headers={"X-Saha-Owner": token_new})
    assert res_new.status_code == 200
    assert res_new.json()["task"]["id"] == task.id


def test_rotation_preserves_isolation_against_other_owners(test_setup):
    """Regression: Rotating an owner token preserves strict isolation against other owners."""
    import secrets
    client = test_setup["client"]
    db_path = test_setup["db"]

    token_a = secrets.token_hex(16)
    token_b = secrets.token_hex(16)
    owner_a = hash_owner_token(token_a)
    owner_b = hash_owner_token(token_b)

    task_a = Ledger.create_task("Owner A private work", owner_id=owner_a, db_path=db_path)
    task_b = Ledger.create_task("Owner B private work", owner_id=owner_b, db_path=db_path)

    # Owner A rotates to new token
    token_a_new = secrets.token_hex(16)
    res_rotate = client.post("/owner/rotate", headers={"X-Saha-Owner": token_a}, json={"new_token": token_a_new})
    assert res_rotate.status_code == 200

    # New Token A sees Task A, but CANNOT see Task B
    res_a_tasks = client.get("/tasks", headers={"X-Saha-Owner": token_a_new}).json().get("tasks", [])
    assert any(t["id"] == task_a.id for t in res_a_tasks)
    assert not any(t["id"] == task_b.id for t in res_a_tasks)

    assert client.get(f"/tasks/{task_b.id}", headers={"X-Saha-Owner": token_a_new}).status_code == 404

    # Owner B sees Task B, but CANNOT see Task A
    res_b_tasks = client.get("/tasks", headers={"X-Saha-Owner": token_b}).json().get("tasks", [])
    assert any(t["id"] == task_b.id for t in res_b_tasks)
    assert not any(t["id"] == task_a.id for t in res_b_tasks)

    assert client.get(f"/tasks/{task_a.id}", headers={"X-Saha-Owner": token_b}).status_code == 404
