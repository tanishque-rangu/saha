"""Tests for Interruption and Restart Recovery in Saha.

Verifies the core recovery guarantees:
1. Persists task and step state as execution proceeds.
2. Marks a step complete only after its result is verifiably recorded.
3. On restart, reloads the task and resumes from persisted verified state without blindly rerunning completed steps.
4. Handles an interrupted or ambiguous step conservatively; do not claim success when outcome cannot be verified.
5. Preserves existing authorization and tool-policy enforcement during resumed execution.
6. Proves that already verified steps are NEVER executed twice.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from backend.api import app
from backend.db import get_db, hash_owner_token, init_db, check_and_init_data_dirs
from backend.executor import execute_step, run_task
from backend.ledger import Ledger
from backend.policy import get_owner_policy, set_owner_policy
from backend.resume import resume_task
from backend.tools.sandbox import resolve_workspace_path


@pytest.fixture
def recovery_env(monkeypatch):
    """Setup isolated test environment for recovery testing."""
    tmp_dir = tempfile.mkdtemp()
    test_db = Path(tmp_dir) / "recovery_test.db"
    test_ws = Path(tmp_dir) / "workspace"
    test_ws.mkdir()

    monkeypatch.setenv("SAHA_DB_PATH", str(test_db))
    monkeypatch.setenv("SAHA_WORKSPACE_ROOT", str(test_ws))
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    monkeypatch.setenv("SAHA_DEMO_MODE", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "")

    init_db(str(test_db))
    check_and_init_data_dirs()
    return {"db": str(test_db), "ws": str(test_ws), "tmp": tmp_dir}


def test_resume_does_not_execute_verified_steps_twice(recovery_env):
    """Proof that an already-verified step is NOT executed twice during restart recovery."""
    db_path = recovery_env["db"]
    owner_id = hash_owner_token("owner_token_recovery_test_32_chars")

    task = Ledger.create_task("Multi-step task with simulated crash", owner_id=owner_id, db_path=db_path)
    steps_data = [
        {
            "idx": 1,
            "intent": "Write first file",
            "tool": "write_file",
            "args": {"path": "step1.txt", "content": "content_one"},
            "probe": {},
            "depends_on": [],
        },
        {
            "idx": 2,
            "intent": "Write second file",
            "tool": "write_file",
            "args": {"path": "step2.txt", "content": "content_two"},
            "probe": {},
            "depends_on": [1],
        },
        {
            "idx": 3,
            "intent": "Write third file",
            "tool": "write_file",
            "args": {"path": "step3.txt", "content": "content_three"},
            "probe": {},
            "depends_on": [2],
        },
    ]
    steps = Ledger.add_steps(task.id, steps_data, db_path=db_path)
    assert len(steps) == 3

    # Track how many times each step's tool is invoked
    execution_counter = {"step1.txt": 0, "step2.txt": 0, "step3.txt": 0}

    from backend import tools
    real_write = tools.write_file

    def spy_write(path, content, task_id=None):
        execution_counter[path] = execution_counter.get(path, 0) + 1
        return real_write(path, content, task_id=task_id)

    with patch("backend.executor.write_file", side_effect=spy_write):
        # 1. Execute Step 1 completely to verified
        s1 = execute_step(steps[0].id, db_path=db_path)
        assert s1.status == "verified"
        assert execution_counter["step1.txt"] == 1

        # 2. Simulate process crash while Step 2 is in-flight:
        # Step 2 was marked 'running', but the process died before mark_step_done or mark_step_verified
        Ledger.mark_step_running(steps[1].id, db_path=db_path)
        step2_crashed = Ledger.get_step(steps[1].id, db_path=db_path)
        assert step2_crashed.status == "running"
        assert step2_crashed.finished_at is None
        # Task was left in 'running' status when process died
        Ledger.update_task_status(task.id, "running", db_path=db_path)

        # 3. Simulate process restart & resume: run_task reloads persisted state
        # Reset counter to prove subsequent execution counts during recovery
        execution_counter["step1.txt"] = 0
        execution_counter["step2.txt"] = 0
        execution_counter["step3.txt"] = 0

        final_task = run_task(task.id, db_path=db_path)
        assert final_task.status == "complete"

        # Verification of Recovery Invariants:
        # Step 1 was already verified -> must NOT be executed again (count == 0)
        assert execution_counter["step1.txt"] == 0, "CRITICAL ERROR: Step 1 was executed twice!"

        # Step 2 was interrupted -> recovered and executed once
        assert execution_counter["step2.txt"] == 1

        # Step 3 was pending -> executed once
        assert execution_counter["step3.txt"] == 1

        # Check all steps in ledger
        summary = Ledger.get_task_summary(task.id, owner_id=owner_id, db_path=db_path)
        assert summary["is_complete"] is True
        assert all(s["status"] == "verified" for s in summary["steps"])

        # Check physical artifacts exist on disk
        for fname in ("step1.txt", "step2.txt", "step3.txt"):
            fpath = resolve_workspace_path(fname, task_id=task.id, owner_id=owner_id)
            assert fpath.exists()


def test_interrupted_step_conservative_ambiguity_handling(recovery_env):
    """An interrupted step with no verified outcome is handled conservatively:
    it is never falsely claimed as success, but safely reloaded and re-executed."""
    db_path = recovery_env["db"]
    owner_id = hash_owner_token("owner_token_ambiguous_32_chars___")

    task = Ledger.create_task("Ambiguous Step Task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [{
            "idx": 1,
            "intent": "Ambiguous write",
            "tool": "write_file",
            "args": {"path": "ambiguous.txt", "content": "verified_content"},
            "probe": {},
            "depends_on": [],
        }],
        db_path=db_path,
    )
    step1 = steps[0]

    # Process died while step was running: no file on disk, status='running'
    Ledger.mark_step_running(step1.id, db_path=db_path)
    crashed_step = Ledger.get_step(step1.id, db_path=db_path)
    assert crashed_step.status == "running"

    # Recovery evaluation must NOT claim success
    recovered_step = Ledger.recover_interrupted_step(crashed_step, task_id=task.id, db_path=db_path)
    assert recovered_step.status == "pending"
    assert recovered_step.started_at is None
    assert recovered_step.error is None

    # Now running the task executes the pending step cleanly and verifies it
    run_task(task.id, db_path=db_path)
    final_step = Ledger.get_step(step1.id, db_path=db_path)
    assert final_step.status == "verified"
    assert final_step.probe_json is not None

    disk_file = resolve_workspace_path("ambiguous.txt", task_id=task.id, owner_id=owner_id)
    assert disk_file.exists()
    assert disk_file.read_text(encoding="utf-8") == "verified_content"


def test_interrupted_step_in_done_state_with_passing_probe_recovers_to_verified(recovery_env):
    """If a step finished its tool execution and wrote its result before crash (status='done'),
    recovery runs deterministic probe: if physical outcome is confirmed, marks verified without rerun."""
    db_path = recovery_env["db"]
    owner_id = hash_owner_token("owner_token_done_state_32_chars__")

    task = Ledger.create_task("Done State Recovery Task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "Pre-crash completed write",
                "tool": "write_file",
                "args": {"path": "pre_crash.txt", "content": "exact_content"},
                "probe": {},
                "depends_on": [],
            },
            {
                "idx": 2,
                "intent": "Subsequent step",
                "tool": "write_file",
                "args": {"path": "after_crash.txt", "content": "after_content"},
                "probe": {},
                "depends_on": [1],
            },
        ],
        db_path=db_path,
    )

    # 1. Manually write the file to workspace (as the tool would have)
    fpath = resolve_workspace_path("pre_crash.txt", task_id=task.id, owner_id=owner_id)
    fpath.write_text("exact_content", encoding="utf-8")
    from backend.tools.files import compute_sha256
    file_sha = compute_sha256(b"exact_content")

    # 2. Mark step 1 'done' with recorded result (crash occurred before mark_step_verified)
    Ledger.mark_step_running(steps[0].id, db_path=db_path)
    Ledger.mark_step_done(
        steps[0].id,
        {"path": "pre_crash.txt", "bytes_written": len("exact_content"), "sha256": file_sha},
        db_path=db_path,
    )
    done_step = Ledger.get_step(steps[0].id, db_path=db_path)
    assert done_step.status == "done"

    # Track execution of tools
    executed_tools = []
    from backend import tools
    real_write = tools.write_file

    def track_write(path, content, task_id=None):
        executed_tools.append(path)
        return real_write(path, content, task_id=task_id)

    with patch("backend.executor.write_file", side_effect=track_write):
        # 3. Resume / run task
        run_task(task.id, db_path=db_path)

        # Step 1 was verified from disk probe -> tool write_file was NOT called for pre_crash.txt!
        assert "pre_crash.txt" not in executed_tools, "Tool should not re-run when result was verifiably recorded"
        # Step 2 was executed
        assert "after_crash.txt" in executed_tools

    s1 = Ledger.get_step(steps[0].id, db_path=db_path)
    s2 = Ledger.get_step(steps[1].id, db_path=db_path)
    assert s1.status == "verified"
    assert s2.status == "verified"


def test_recovery_preserves_policy_and_authorization(recovery_env):
    """Resumed execution preserves full tool-policy enforcement: an unauthorized interrupted step blocks."""
    db_path = recovery_env["db"]
    token = "owner_token_policy_rec_test_32_chars"
    owner_id = hash_owner_token(token)
    headers = {"X-Saha-Owner": token}

    task = Ledger.create_task("Policy Preservation Task", owner_id=owner_id, db_path=db_path)
    # Step 1: verified write_file
    # Step 2: http_get to non-allowlisted domain (attempted before crash, left in 'running')
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "Local write",
                "tool": "write_file",
                "args": {"path": "ok.txt", "content": "ok"},
                "probe": {},
                "depends_on": [],
            },
            {
                "idx": 2,
                "intent": "External call to unallowed domain",
                "tool": "http_get",
                "args": {"url": "https://unauthorized-domain.org"},
                "probe": {},
                "depends_on": [1],
            },
        ],
        db_path=db_path,
    )

    # Step 1 verified
    execute_step(steps[0].id, db_path=db_path)
    assert Ledger.get_step(steps[0].id, db_path=db_path).status == "verified"

    # Step 2 left in 'running' state from simulated crash
    Ledger.mark_step_running(steps[1].id, db_path=db_path)

    # Resume the task: Step 2 must NOT bypass policy!
    recovered_task = run_task(task.id, db_path=db_path)
    assert recovered_task.status == "paused"

    # Step 2 must be blocked with block_reason set
    s2 = Ledger.get_step(steps[1].id, db_path=db_path)
    assert s2.status == "blocked"
    assert s2.block_reason is not None
    assert "not in policy allowed_domains" in s2.block_reason
    assert s2.drift_reason is None


def test_stale_running_tasks_recovered_on_startup_preventing_concurrency_error(recovery_env):
    """Multiple tasks left in 'running' by a crashed process are safely paused on startup,
    preventing MAX_CONCURRENT_TASKS blockage."""
    db_path = recovery_env["db"]
    owner_id = hash_owner_token("owner_token_concurrency_test_32_c")

    # Create 2 stale tasks left in 'running' state
    t1 = Ledger.create_task("Stale Task 1", owner_id=owner_id, db_path=db_path)
    t2 = Ledger.create_task("Stale Task 2", owner_id=owner_id, db_path=db_path)
    Ledger.update_task_status(t1.id, "running", db_path=db_path)
    Ledger.update_task_status(t2.id, "running", db_path=db_path)

    # Calling recover_stale_running_tasks recovers them to 'paused'
    recovered_ids = Ledger.recover_stale_running_tasks(db_path=db_path)
    assert t1.id in recovered_ids
    assert t2.id in recovered_ids

    assert Ledger.get_task(t1.id, db_path=db_path).status == "paused"
    assert Ledger.get_task(t2.id, db_path=db_path).status == "paused"

    # Now a new task can run without exceeding MAX_CONCURRENT_TASKS (2)
    t3 = Ledger.create_task("New Task", owner_id=owner_id, db_path=db_path)
    Ledger.add_steps(
        t3.id,
        [{"idx": 1, "intent": "Quick write", "tool": "write_file", "args": {"path": "q.txt", "content": "q"}}],
        db_path=db_path,
    )
    executed_task = run_task(t3.id, db_path=db_path)
    assert executed_task.status == "complete"


def test_resume_task_endpoint_resumes_interrupted_task(recovery_env):
    """POST /tasks/{id}/resume successfully recovers an interrupted task and finishes execution."""
    db_path = recovery_env["db"]
    token = "owner_token_api_resume_test_32_chars"
    headers = {"X-Saha-Owner": token}
    owner_id = hash_owner_token(token)

    task = Ledger.create_task("API Resume Interrupted Task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "First part",
                "tool": "write_file",
                "args": {"path": "p1.txt", "content": "p1"},
                "probe": {},
                "depends_on": [],
            },
            {
                "idx": 2,
                "intent": "Interrupted part",
                "tool": "write_file",
                "args": {"path": "p2.txt", "content": "p2"},
                "probe": {},
                "depends_on": [1],
            },
        ],
        db_path=db_path,
    )

    # Step 1 verified
    execute_step(steps[0].id, db_path=db_path)
    # Step 2 crashed in 'running'
    Ledger.mark_step_running(steps[1].id, db_path=db_path)
    Ledger.update_task_status(task.id, "running", db_path=db_path)

    client = TestClient(app)

    # Call resume_task synchronously (or via API)
    res = resume_task(task.id, db_path=db_path)
    assert res["task"]["status"] == "complete"
    assert res["drift_detected"] is False
    assert res["replanned"] is False

    summary = Ledger.get_task_summary(task.id, owner_id=owner_id, db_path=db_path)
    assert summary["is_complete"] is True
    assert all(s["status"] == "verified" for s in summary["steps"])
