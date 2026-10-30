"""Focused Tests for Phase 7-Lite: Always-On Watcher, Restart Recovery, Scheduled Routines, and Activity Feed."""

import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.api import IP_POST_HISTORY, OWNER_POST_HISTORY, app
from backend.db import get_db, hash_owner_token, init_db, utc_now_iso
from backend.ledger import Ledger
from backend.policy import Policy, get_owner_policy, set_owner_policy
from backend.skills import (
    create_skill_from_task,
    get_due_skills,
    get_skill,
    schedule_skill,
    update_skill_schedule_outcome,
)
from backend.tools.sandbox import resolve_workspace_path
from backend.verifiers import verify_step
from backend.watcher import (
    Watcher,
    count_owner_auto_heals,
    get_global_watcher,
    get_owner_activity,
)


@pytest.fixture
def client(monkeypatch):
    tmp_dir = tempfile.mkdtemp()
    test_db = Path(tmp_dir) / "test_phase7.db"
    test_ws = Path(tmp_dir) / "workspace"
    test_ws.mkdir()

    monkeypatch.setenv("SAHA_DB_PATH", str(test_db))
    monkeypatch.setenv("SAHA_WORKSPACE_ROOT", str(test_ws))
    monkeypatch.setenv("SAHA_DEMO_MODE", "true")
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "false")
    monkeypatch.setenv("SAHA_WATCHER_ENABLED", "false")
    monkeypatch.setenv("SAHA_MIN_SCHEDULE_MINUTES", "10")
    monkeypatch.setenv("NEBIUS_API_KEY", "")

    init_db(str(test_db))
    IP_POST_HISTORY.clear()
    OWNER_POST_HISTORY.clear()
    return TestClient(app)


# =========================================================================
# 1. WATCHER & DRIFT DETECTION
# =========================================================================

def test_drift_detected_after_file_deletion_and_task_paused(tmp_path):
    """Deleting a file on disk causes the watcher to detect drift, log event, and pause task."""
    db_file = str(tmp_path / "test_drift.db")
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    os.environ["SAHA_DB_PATH"] = db_file
    os.environ["SAHA_WORKSPACE_ROOT"] = str(ws_dir)
    init_db(db_file)

    owner_id = "test_owner_drift_1"
    task = Ledger.create_task("Write a file", owner_id=owner_id, db_path=db_file)
    task_ws = resolve_workspace_path(".", task_id=task.id, owner_id=owner_id)
    task_ws.mkdir(parents=True, exist_ok=True)

    file_path = task_ws / "test.txt"
    file_path.write_text("initial content", encoding="utf-8")

    # Add verified step with file probe
    step_data = [{
        "idx": 1,
        "intent": "Write test file",
        "tool": "write_file",
        "args": {"path": "test.txt", "content": "initial content"},
        "status": "pending",
        "probe": {"expected_file": "test.txt"},
        "depends_on": [],
    }]
    Ledger.add_steps(task.id, step_data, db_path=db_file)
    steps = Ledger.get_steps(task.id, db_path=db_file)
    step = steps[0]
    Ledger.mark_step_running(step.id, db_path=db_file)
    Ledger.mark_step_done(step.id, result={"bytes_written": 15}, db_path=db_file)
    Ledger.mark_step_verified(step.id, probe={"expected_file": "test.txt"}, db_path=db_file)
    Ledger.update_task_status(task.id, "complete", db_path=db_file)

    # Now delete the file directly on disk
    file_path.unlink()
    assert not file_path.exists()

    watcher = Watcher(interval=15, enabled=False, db_path=db_file)
    with patch("backend.resume._generate_fast_explanation_worker"):
        with patch.object(watcher, "_auto_heal_worker"):
            watcher.run_cycle()

    # Verify drift event recorded
    with get_db(db_file) as conn:
        events = conn.execute("SELECT * FROM drift_events WHERE task_id = ?", (task.id,)).fetchall()
        assert len(events) == 1
        assert events[0]["step_id"] == step.id

    # Verify step is drifted and task is paused
    step_after = Ledger.get_step(step.id, db_path=db_file)
    assert step_after.status == "drifted"
    task_after = Ledger.get_task(task.id, db_path=db_file)
    assert task_after.status == "paused"


# =========================================================================
# 2. AUTO-HEAL: Budget & Policy Enforcement
# =========================================================================

def test_auto_heal_starts_resume_and_respects_budget_and_policy(tmp_path):
    """Auto-heal triggers resume when within budget (<=3/hr, <=10/day), else leaves needs_attention."""
    db_file = str(tmp_path / "test_budget.db")
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    os.environ["SAHA_DB_PATH"] = db_file
    os.environ["SAHA_WORKSPACE_ROOT"] = str(ws_dir)
    init_db(db_file)

    owner_id = "test_owner_budget"
    policy = get_owner_policy(owner_id, db_path=db_file)
    assert policy.auto_heal is True

    # 1. Simulate 3 auto-heals already consumed in the last hour
    t_dummy = Ledger.create_task("Dummy task", owner_id=owner_id, db_path=db_file)
    Ledger.add_steps(t_dummy.id, [{"idx": 1, "intent": "dummy", "tool": "write_file", "args": {}, "status": "pending"}], db_path=db_file)
    s_dummy = Ledger.get_steps(t_dummy.id, db_path=db_file)[0]
    now = utc_now_iso()
    with get_db(db_file) as conn:
        for i in range(3):
            conn.execute(
                """
                INSERT INTO drift_events (id, owner_id, task_id, step_id, tool, reason, explanation, detected_at, auto_healed, healed_at)
                VALUES (?, ?, ?, ?, 'write_file', 'reason', 'expl', ?, 1, ?)
                """,
                (f"past_event_{i}", owner_id, t_dummy.id, s_dummy.id, now, now),
            )

    used_hr, used_day = count_owner_auto_heals(owner_id, db_path=db_file)
    assert used_hr == 3
    assert used_day == 3

    # 2. Create task with drift
    task = Ledger.create_task("Budget test task", owner_id=owner_id, db_path=db_file)
    step_data = [{
        "idx": 1,
        "intent": "Write missing file",
        "tool": "write_file",
        "args": {"path": "missing.txt", "content": "xyz"},
        "status": "pending",
        "probe": {"expected_file": "missing.txt"},
    }]
    Ledger.add_steps(task.id, step_data, db_path=db_file)
    step = Ledger.get_steps(task.id, db_path=db_file)[0]
    Ledger.mark_step_running(step.id, db_path=db_file)
    Ledger.mark_step_done(step.id, result={"bytes_written": 3}, db_path=db_file)
    Ledger.mark_step_verified(step.id, probe={"expected_file": "missing.txt"}, db_path=db_file)
    Ledger.update_task_status(task.id, "complete", db_path=db_file)

    watcher = Watcher(interval=15, enabled=False, db_path=db_file)
    with patch("backend.resume._generate_fast_explanation_worker"):
        with patch.object(watcher, "_auto_heal_worker") as mock_heal:
            watcher.run_cycle()
            # Since hourly budget is exhausted (3/3), auto_heal worker should NOT be started
            mock_heal.assert_not_called()

    # Task must be paused and needs_attention must be True
    task_after = Ledger.get_task(task.id, db_path=db_file)
    assert task_after.status == "paused"
    assert task_after.needs_attention is True


def test_auto_heal_off_leaves_needs_attention(tmp_path):
    """When auto_heal=False in owner policy, drift is detected but no heal is initiated."""
    db_file = str(tmp_path / "test_no_heal.db")
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    os.environ["SAHA_DB_PATH"] = db_file
    os.environ["SAHA_WORKSPACE_ROOT"] = str(ws_dir)
    init_db(db_file)

    owner_id = "test_owner_no_heal"
    policy = Policy(auto_heal=False)
    set_owner_policy(owner_id, policy, db_path=db_file)

    task = Ledger.create_task("No heal task", owner_id=owner_id, db_path=db_file)
    step_data = [{
        "idx": 1,
        "intent": "Write missing file",
        "tool": "write_file",
        "args": {"path": "missing2.txt", "content": "xyz"},
        "status": "pending",
        "probe": {"expected_file": "missing2.txt"},
    }]
    Ledger.add_steps(task.id, step_data, db_path=db_file)
    step = Ledger.get_steps(task.id, db_path=db_file)[0]
    Ledger.mark_step_running(step.id, db_path=db_file)
    Ledger.mark_step_done(step.id, result={"bytes_written": 3}, db_path=db_file)
    Ledger.mark_step_verified(step.id, probe={"expected_file": "missing2.txt"}, db_path=db_file)
    Ledger.update_task_status(task.id, "complete", db_path=db_file)

    watcher = Watcher(interval=15, enabled=False, db_path=db_file)
    with patch("backend.resume._generate_fast_explanation_worker"):
        with patch.object(watcher, "_auto_heal_worker") as mock_heal:
            watcher.run_cycle()
            mock_heal.assert_not_called()

    task_after = Ledger.get_task(task.id, db_path=db_file)
    assert task_after.status == "paused"
    assert task_after.needs_attention is True


# =========================================================================
# 3. EXCEPTION RESILIENCE IN WATCHER LOOP
# =========================================================================

def test_exception_in_one_task_does_not_stop_watcher_loop(tmp_path):
    """An unexpected exception during probe of Task 1 does not halt processing of Task 2."""
    db_file = str(tmp_path / "test_resilience.db")
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    os.environ["SAHA_DB_PATH"] = db_file
    os.environ["SAHA_WORKSPACE_ROOT"] = str(ws_dir)
    init_db(db_file)

    owner_id = "resilience_owner"
    t1 = Ledger.create_task("Task 1 error", owner_id=owner_id, db_path=db_file)
    t2 = Ledger.create_task("Task 2 normal", owner_id=owner_id, db_path=db_file)

    for t in (t1, t2):
        Ledger.add_steps(t.id, [{
            "idx": 1,
            "intent": "Step",
            "tool": "write_file",
            "args": {"path": "f.txt", "content": "1"},
            "status": "pending",
            "probe": {"expected_file": "f.txt"},
        }], db_path=db_file)
        s = Ledger.get_steps(t.id, db_path=db_file)[0]
        Ledger.mark_step_running(s.id, db_path=db_file)
        Ledger.mark_step_done(s.id, result={"bytes_written": 1}, db_path=db_file)
        Ledger.mark_step_verified(s.id, probe={"expected_file": "f.txt"}, db_path=db_file)
        Ledger.update_task_status(t.id, "complete", db_path=db_file)

    watcher = Watcher(interval=15, enabled=False, db_path=db_file)

    original_verify = verify_step
    call_count = {"val": 0}

    def flaky_verify(*args, **kwargs):
        call_count["val"] += 1
        if kwargs.get("task_id") == t1.id:
            raise RuntimeError("Simulated transient OS error on Task 1")
        return original_verify(*args, **kwargs)

    with patch("backend.watcher.verify_step", side_effect=flaky_verify):
        # Should not raise exception
        watcher.run_cycle()

    assert call_count["val"] >= 2, "Watcher did not continue to Task 2"


# =========================================================================
# 4. RESTART RECOVERY CASES
# =========================================================================

def test_restart_recovery_probe_passes_marks_verified(tmp_path):
    """Step stuck in running whose probe passes is marked verified on restart."""
    db_file = str(tmp_path / "test_rec_pass.db")
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    os.environ["SAHA_DB_PATH"] = db_file
    os.environ["SAHA_WORKSPACE_ROOT"] = str(ws_dir)
    init_db(db_file)

    owner_id = "rec_owner"
    task = Ledger.create_task("Interrupted task", owner_id=owner_id, db_path=db_file)
    task_ws = resolve_workspace_path(".", task_id=task.id, owner_id=owner_id)
    task_ws.mkdir(parents=True, exist_ok=True)
    # File actually exists on disk
    (task_ws / "done_file.txt").write_text("hello", encoding="utf-8")

    Ledger.add_steps(task.id, [{
        "idx": 1,
        "intent": "Write done file",
        "tool": "write_file",
        "args": {"path": "done_file.txt", "content": "hello"},
        "status": "pending",
        "probe": {"expected_file": "done_file.txt"},
    }], db_path=db_file)
    step = Ledger.get_steps(task.id, db_path=db_file)[0]
    Ledger.mark_step_running(step.id, db_path=db_file)
    Ledger.update_task_status(task.id, "running", db_path=db_file)

    # Disable auto_heal so task pauses after recovery
    set_owner_policy(owner_id, Policy(auto_heal=False), db_path=db_file)

    recovered = Ledger.recover_stale_running_tasks(db_path=db_file)
    assert task.id in recovered

    step_after = Ledger.get_step(step.id, db_path=db_file)
    assert step_after.status == "verified"


def test_restart_recovery_probe_fails_requeues_pending(tmp_path):
    """Step stuck in running whose probe fails is reset to pending on restart."""
    db_file = str(tmp_path / "test_rec_fail.db")
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    os.environ["SAHA_DB_PATH"] = db_file
    os.environ["SAHA_WORKSPACE_ROOT"] = str(ws_dir)
    init_db(db_file)

    owner_id = "rec_owner_fail"
    task = Ledger.create_task("Interrupted task", owner_id=owner_id, db_path=db_file)

    Ledger.add_steps(task.id, [{
        "idx": 1,
        "intent": "Write file never created",
        "tool": "write_file",
        "args": {"path": "never_created.txt", "content": "xyz"},
        "status": "pending",
        "probe": {"expected_file": "never_created.txt"},
    }], db_path=db_file)
    step = Ledger.get_steps(task.id, db_path=db_file)[0]
    Ledger.mark_step_running(step.id, db_path=db_file)
    Ledger.update_task_status(task.id, "running", db_path=db_file)

    set_owner_policy(owner_id, Policy(auto_heal=False), db_path=db_file)

    recovered = Ledger.recover_stale_running_tasks(db_path=db_file)
    assert task.id in recovered

    step_after = Ledger.get_step(step.id, db_path=db_file)
    assert step_after.status == "pending"
    task_after = Ledger.get_task(task.id, db_path=db_file)
    assert task_after.status == "paused"
    assert task_after.needs_attention is True


# =========================================================================
# 5. SCHEDULED ROUTINES & FAILURE HANDLING
# =========================================================================

def test_schedule_limits_and_consecutive_failures(client):
    """Test min/max schedule limits, 2 routine max, and disabling schedule after 3 consecutive failures."""
    owner = "token_alice_sched_1234567890abcdef"
    headers = {"X-Saha-Owner": owner}
    hashed_owner = hash_owner_token(owner)

    # Create 2 verified skills
    t1 = Ledger.create_task("Skill Task 1", owner_id=hashed_owner)
    Ledger.add_steps(t1.id, [{"idx": 1, "intent": "Step 1", "tool": "write_file", "args": {"path": "a.txt", "content": "1"}, "status": "pending"}])
    s1 = Ledger.get_steps(t1.id)[0]
    Ledger.mark_step_running(s1.id)
    Ledger.mark_step_done(s1.id, result={"bytes_written": 1})
    Ledger.mark_step_verified(s1.id, probe={})
    Ledger.update_task_status(t1.id, "complete")
    skill1_res = client.post(f"/tasks/{t1.id}/save-as-skill", json={"name": "Skill 1"}, headers=headers)
    skill1_id = skill1_res.json()["skill"]["id"]

    t2 = Ledger.create_task("Skill Task 2", owner_id=hashed_owner)
    Ledger.add_steps(t2.id, [{"idx": 1, "intent": "Step 2", "tool": "write_file", "args": {"path": "b.txt", "content": "2"}, "status": "pending"}])
    s2 = Ledger.get_steps(t2.id)[0]
    Ledger.mark_step_running(s2.id)
    Ledger.mark_step_done(s2.id, result={"bytes_written": 1})
    Ledger.mark_step_verified(s2.id, probe={})
    Ledger.update_task_status(t2.id, "complete")
    skill2_res = client.post(f"/tasks/{t2.id}/save-as-skill", json={"name": "Skill 2"}, headers=headers)
    skill2_id = skill2_res.json()["skill"]["id"]

    # 1. Schedule with < min interval (default min=10) -> 400
    res_too_low = client.put(f"/skills/{skill1_id}/schedule", json={"minutes": 5}, headers=headers)
    assert res_too_low.status_code == 400
    assert "at least 10 minutes" in res_too_low.json()["detail"]

    # 2. Schedule with > 1440 -> 400
    res_too_high = client.put(f"/skills/{skill1_id}/schedule", json={"minutes": 1500}, headers=headers)
    assert res_too_high.status_code == 400

    # 3. Schedule skill 1 and skill 2 successfully
    res_sched1 = client.put(f"/skills/{skill1_id}/schedule", json={"minutes": 15}, headers=headers)
    assert res_sched1.status_code == 200
    assert res_sched1.json()["skill"]["schedule_minutes"] == 15

    res_sched2 = client.put(f"/skills/{skill2_id}/schedule", json={"minutes": 30}, headers=headers)
    assert res_sched2.status_code == 200

    # 4. Attempt to schedule a 3rd skill -> 400
    t3 = Ledger.create_task("Skill Task 3", owner_id=hashed_owner)
    Ledger.add_steps(t3.id, [{"idx": 1, "intent": "Step 3", "tool": "write_file", "args": {"path": "c.txt", "content": "3"}, "status": "pending"}])
    s3 = Ledger.get_steps(t3.id)[0]
    Ledger.mark_step_running(s3.id)
    Ledger.mark_step_done(s3.id, result={"bytes_written": 1})
    Ledger.mark_step_verified(s3.id, probe={})
    Ledger.update_task_status(t3.id, "complete")
    skill3_id = client.post(f"/tasks/{t3.id}/save-as-skill", json={"name": "Skill 3"}, headers=headers).json()["skill"]["id"]

    res_sched3 = client.put(f"/skills/{skill3_id}/schedule", json={"minutes": 60}, headers=headers)
    assert res_sched3.status_code == 400
    assert "Maximum 2 scheduled routines" in res_sched3.json()["detail"]

    # 5. Consecutive failure tracking (after 3 failures, schedule is disabled and needs_attention set)
    update_skill_schedule_outcome(skill1_id, success=False)
    update_skill_schedule_outcome(skill1_id, success=False)
    skill_after_2 = get_skill(skill1_id)
    assert skill_after_2.consecutive_failures == 2
    assert skill_after_2.schedule_minutes == 15

    update_skill_schedule_outcome(skill1_id, success=False)
    skill_after_3 = get_skill(skill1_id)
    assert skill_after_3.consecutive_failures == 3
    assert skill_after_3.schedule_minutes is None
    assert skill_after_3.next_run_at is None
    assert skill_after_3.needs_attention is True


# =========================================================================
# 6. OWNER ISOLATION & ACTIVITY FEED
# =========================================================================

def test_owner_isolation_activity_and_watcher_status(client):
    """Owner B cannot view Owner A's activity feed or watcher status budget."""
    owner_a = "token_alice_activity_1234567890abcdef"
    owner_b = "token_bob_activity_9876543210fedcba"
    headers_a = {"X-Saha-Owner": owner_a}
    headers_b = {"X-Saha-Owner": owner_b}
    hashed_a = hash_owner_token(owner_a)

    # Owner A logs a drift event
    t_a = Ledger.create_task("Task A", owner_id=hashed_a)
    Ledger.add_steps(t_a.id, [{"idx": 1, "intent": "intent_a", "tool": "write_file", "args": {}, "status": "pending"}])
    s_a = Ledger.get_steps(t_a.id)[0]
    now = utc_now_iso()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO drift_events (id, owner_id, task_id, step_id, tool, reason, explanation, detected_at, auto_healed, healed_at)
            VALUES ('event_a1', ?, ?, ?, 'write_file', 'missing file', 'explanation', ?, 1, ?)
            """,
            (hashed_a, t_a.id, s_a.id, now, now),
        )

    # Owner A sees activity
    act_a = client.get("/activity", headers=headers_a).json()["activity"]
    assert len(act_a) >= 2  # drift_detected and healed
    assert any(item["type"] == "drift_detected" for item in act_a)
    assert any(item["type"] == "healed" for item in act_a)

    # Owner B activity is empty
    act_b = client.get("/activity", headers=headers_b).json()["activity"]
    assert len(act_b) == 0

    # Watcher status is owner-scoped
    status_a = client.get("/watcher/status", headers=headers_a).json()
    assert status_a["heals_used_hour"] == 1
    assert status_a["heals_remaining_hour"] == 2

    status_b = client.get("/watcher/status", headers=headers_b).json()
    assert status_b["heals_used_hour"] == 0
    assert status_b["heals_remaining_hour"] == 3


def test_space_deletion_clears_activity_and_new_data(client):
    """DELETE /owner/space purges drift_events, skills, and activity records for that owner."""
    owner_a = "token_alice_del_1234567890abcdef"
    headers_a = {"X-Saha-Owner": owner_a}
    hashed_a = hash_owner_token(owner_a)

    # Create task and drift event
    t_del = Ledger.create_task("Del Task", owner_id=hashed_a)
    Ledger.add_steps(t_del.id, [{"idx": 1, "intent": "intent_del", "tool": "write_file", "args": {}, "status": "pending"}])
    s_del = Ledger.get_steps(t_del.id)[0]
    now = utc_now_iso()
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO drift_events (id, owner_id, task_id, step_id, tool, reason, explanation, detected_at, auto_healed)
            VALUES ('del_ev1', ?, ?, ?, 'write_file', 'reason', 'expl', ?, 0)
            """,
            (hashed_a, t_del.id, s_del.id, now),
        )

    assert len(client.get("/activity", headers=headers_a).json()["activity"]) > 0

    # Delete owner space
    del_res = client.delete("/owner/space", headers=headers_a)
    assert del_res.status_code == 200

    # Activity feed is now empty
    assert len(client.get("/activity", headers=headers_a).json()["activity"]) == 0


# =========================================================================
# 7. CLEAN THREAD SHUTDOWN
# =========================================================================

def test_clean_thread_shutdown(tmp_path):
    """Watcher thread starts and terminates cleanly on stop()."""
    db_file = str(tmp_path / "test_shutdown.db")
    init_db(db_file)

    watcher = Watcher(interval=15, enabled=True, db_path=db_file)
    watcher.start()
    assert watcher._thread is not None
    assert watcher._thread.is_alive()

    # Stop watcher
    watcher.stop(timeout=2.0)
    assert not watcher._thread.is_alive()
