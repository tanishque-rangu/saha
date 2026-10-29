"""Unit tests for append-only Saha Ledger state machine."""

import tempfile
from pathlib import Path
import pytest

from backend.db import get_db, init_db
from backend.ledger import Ledger


@pytest.fixture
def temp_db():
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_file = Path(tmp_dir) / "test_ledger.db"
        init_db(str(db_file))
        yield str(db_file)


def test_task_lifecycle(temp_db):
    task = Ledger.create_task("Test Task Lifecycle", db_path=temp_db)
    assert task.status == "planning"
    assert task.goal == "Test Task Lifecycle"

    Ledger.update_task_status(task.id, "running", db_path=temp_db)
    loaded = Ledger.get_task(task.id, db_path=temp_db)
    assert loaded.status == "running"

    with pytest.raises(ValueError):
        Ledger.update_task_status(task.id, "invalid_status", db_path=temp_db)


def test_step_transitions_and_crash_visibility(temp_db):
    task = Ledger.create_task("Test Steps", db_path=temp_db)
    steps_data = [
        {"intent": "Step 1", "tool": "write_file", "args": {"path": "a.txt", "content": "hello"}},
        {"intent": "Step 2", "tool": "run_shell", "args": {"cmd": "echo test"}},
    ]
    steps = Ledger.add_steps(task.id, steps_data, db_path=temp_db)
    assert len(steps) == 2
    assert steps[0].status == "pending"
    assert steps[0].seq == 1
    assert steps[1].seq == 2

    # Step marked running BEFORE execution
    step1 = Ledger.mark_step_running(steps[0].id, db_path=temp_db)
    assert step1.status == "running"
    assert step1.started_at is not None

    # Mid-step crash visibility check
    inspected = Ledger.get_step(steps[0].id, db_path=temp_db)
    assert inspected.status == "running"
    assert inspected.finished_at is None

    # Step finished
    step1_done = Ledger.mark_step_done(steps[0].id, {"bytes_written": 5}, db_path=temp_db)
    assert step1_done.status == "done"
    assert step1_done.finished_at is not None

    # Step verified
    step1_verified = Ledger.mark_step_verified(steps[0].id, {"sha256": "abc"}, db_path=temp_db)
    assert step1_verified.status == "verified"


def test_drift_and_drift_events(temp_db):
    task = Ledger.create_task("Test Drift", db_path=temp_db)
    steps = Ledger.add_steps(task.id, [{"intent": "Write", "tool": "write_file", "args": {}}], db_path=temp_db)

    Ledger.mark_step_running(steps[0].id, db_path=temp_db)
    Ledger.mark_step_done(steps[0].id, {}, db_path=temp_db)
    Ledger.mark_step_drifted(steps[0].id, reason="File deleted by user", probe={"passed": False}, db_path=temp_db)

    event_id = Ledger.record_drift_event(
        task_id=task.id,
        step_id=steps[0].id,
        tool="write_file",
        reason="File deleted by user",
        explanation="The file was unlinked from workspace disk.",
        db_path=temp_db,
    )
    assert event_id is not None

    summary = Ledger.get_task_summary(task.id, db_path=temp_db)
    assert summary["has_drift"] is True
    assert summary["counts"]["drifted"] == 1
    assert len(summary["drift_events"]) == 1
    assert summary["drift_events"][0]["explanation"] == "The file was unlinked from workspace disk."


def test_append_only_preserves_history(temp_db):
    task = Ledger.create_task("Append Only Test", db_path=temp_db)
    initial_steps = [
        {"intent": "Step 1", "tool": "write_file", "args": {"path": "1.txt"}, "depends_on": []},
        {"intent": "Step 2", "tool": "write_file", "args": {"path": "2.txt"}, "depends_on": [1]},
    ]
    steps = Ledger.add_steps(task.id, initial_steps, db_path=temp_db)
    assert len(steps) == 2
    assert steps[1].to_dict()["depends_on"] == [1]

    # Step 1 verified, then reality drifts
    Ledger.mark_step_running(steps[0].id, db_path=temp_db)
    Ledger.mark_step_done(steps[0].id, {}, db_path=temp_db)
    Ledger.mark_step_drifted(steps[0].id, "file missing", {"passed": False}, db_path=temp_db)

    # Append new repair steps
    new_steps = [
        {"intent": "Repair Step 1", "tool": "write_file", "args": {"path": "1.txt"}, "depends_on": []},
        {"intent": "Recreate Step 2", "tool": "write_file", "args": {"path": "2.txt"}, "depends_on": [3]},
    ]
    added = Ledger.add_steps(task.id, new_steps, db_path=temp_db)
    repair_step_1 = added[-2]
    repair_step_2 = added[-1]

    # Drifted step stays drifted (never superseded); resolved_by points to repair step
    Ledger.supersede_steps([steps[0].id], superseded_by_id=repair_step_1.id, db_path=temp_db)
    drifted_step = Ledger.get_step(steps[0].id, db_path=temp_db)
    assert drifted_step.status == "drifted"  # Protected from overwrite!

    Ledger.mark_step_resolved(steps[0].id, repair_step_1.id, db_path=temp_db)
    drifted_step = Ledger.get_step(steps[0].id, db_path=temp_db)
    assert drifted_step.resolved_by == repair_step_1.id

    # Dependent Step 2 transitions to superseded
    Ledger.supersede_steps([steps[1].id], superseded_by_id=repair_step_2.id, db_path=temp_db)
    step2 = Ledger.get_step(steps[1].id, db_path=temp_db)
    assert step2.status == "superseded"
    assert step2.superseded_by == repair_step_2.id

    # Verify that ALL original rows still exist (none deleted)
    all_steps = Ledger.get_steps(task.id, include_superseded=True, db_path=temp_db)
    assert len(all_steps) == 4

    # Verify task complete condition:
    # 1) Mark repairs verified
    Ledger.mark_step_running(repair_step_1.id, db_path=temp_db)
    Ledger.mark_step_done(repair_step_1.id, {}, db_path=temp_db)
    Ledger.mark_step_verified(repair_step_1.id, {"passed": True}, db_path=temp_db)

    Ledger.mark_step_running(repair_step_2.id, db_path=temp_db)
    Ledger.mark_step_done(repair_step_2.id, {}, db_path=temp_db)
    Ledger.mark_step_verified(repair_step_2.id, {"passed": True}, db_path=temp_db)

    summary = Ledger.get_task_summary(task.id, db_path=temp_db)
    assert summary["is_complete"] is True
