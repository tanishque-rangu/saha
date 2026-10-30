"""Focused end-to-end multi-process termination and recovery test.

Verifies true operating system process interruption and restart recovery:
1. Process 1 (separate OS process via subprocess.Popen) starts against real SQLite WAL and workspace.
2. Executes and verifies Step 1 (write_file with SHA-256 probe).
3. Process 1 is interrupted (SIGKILL / proc.kill()) while Step 2 (external HTTP mutation) is running in-flight.
4. Process 2 (fresh separate OS process) starts against the exact same SQLite database and workspace.
5. Runs actual startup recovery (recover_stale_running_tasks) and resume path (resume_task).
6. Confirms:
   - Step 1 (already verified) is NOT repeated.
   - Interrupted Step 2 reuses the identical stable idempotency key persisted in SQLite.
   - Step 3 executes to completion.
   - Task reaches expected final state ('complete').
   - For untrusted targets, Process 2 refuses blind retry, transitions to 'blocked', and pauses.

REAL VS MOCKED ACCOUNTING:
- REAL:
  * Operating system process creation and termination (subprocess.Popen, proc.kill()).
  * SQLite WAL transactional database on disk (real concurrency and crash recovery).
  * Real sandboxed filesystem writes and SHA-256 verifier probes.
  * Real policy checking and approval audit log.
- MOCKED:
  * External HTTP endpoints: Mocked in subprocess runner to simulate mid-flight socket drops
    and record exact headers (Idempotency-Key) without third-party internet dependencies.
  * LLM calls: Mocked to return deterministic strings.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from backend.db import get_db, hash_owner_token, init_db, check_and_init_data_dirs
from backend.ledger import Ledger
from backend.policy import Policy, get_owner_policy, set_owner_policy, get_owner_approvals
from backend.tools.sandbox import resolve_workspace_path


def wait_for_marker(marker_file: Path, timeout: float = 12.0) -> bool:
    """Poll until marker file is written by subprocess or timeout expires."""
    start = time.time()
    while time.time() - start < timeout:
        if marker_file.exists():
            return True
        time.sleep(0.05)
    return False


def test_real_subprocess_interruption_and_recovery_with_trusted_idempotency():
    """Test real OS process termination (proc.kill()) during an external HTTP mutation
    and verify startup recovery in a fresh OS process reuses the stable idempotency key."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "subprocess_recovery.db"
        ws_path = tmp_path / "workspace"
        marker_dir = tmp_path / "markers"
        ws_path.mkdir(parents=True, exist_ok=True)
        marker_dir.mkdir(parents=True, exist_ok=True)

        init_db(str(db_path))

        owner_token = "real_subprocess_test_token_32_c!"
        owner_id = hash_owner_token(owner_token)

        # Configure owner policy: httpbin.org is allowed and TRUSTED for idempotency
        policy = Policy(
            allowed_tools=["write_file", "read_file", "http_request", "list_files"],
            allowed_domains=["httpbin.org"],
            trusted_idempotency_domains=["httpbin.org"],
            allowed_paths=["*"],
            auto_heal=False,
            max_steps=10,
        )
        set_owner_policy(owner_id, policy, db_path=str(db_path))

        # Create multi-step task
        task = Ledger.create_task("Subprocess recovery test task", owner_id=owner_id, db_path=str(db_path))
        steps = Ledger.add_steps(
            task.id,
            [
                {
                    "idx": 1,
                    "intent": "Write initial dataset",
                    "tool": "write_file",
                    "args": {"path": "step1.txt", "content": "step 1 initial data"},
                    "probe": {},
                    "depends_on": [],
                },
                {
                    "idx": 2,
                    "intent": "Dispatch external payment mutation",
                    "tool": "http_request",
                    "args": {
                        "url": "https://httpbin.org/post",
                        "method": "POST",
                        "json": {"charge_id": "ch_999", "amount": 250},
                    },
                    "probe": {"expected_status": [200, 201]},
                    "depends_on": [1],
                },
                {
                    "idx": 3,
                    "intent": "Synthesize final report",
                    "tool": "write_file",
                    "args": {"path": "step3.txt", "content": "step 3 final summary"},
                    "probe": {},
                    "depends_on": [2],
                },
            ],
            db_path=str(db_path),
        )
        s1_id = steps[0].id
        s2_id = steps[1].id
        s3_id = steps[2].id

        runner_script = PROJECT_ROOT / "scripts" / "subprocess_recovery_runner.py"

        sub_env = os.environ.copy()
        sub_env["SAHA_DB_PATH"] = str(db_path)
        sub_env["SAHA_WORKSPACE_ROOT"] = str(ws_path)
        sub_env["PYTHONPATH"] = str(PROJECT_ROOT)
        sub_env["NEBIUS_API_KEY"] = ""

        # ---------------------------------------------------------------------
        # 1. Spawn Process 1: Executes Step 1 and pauses inside Step 2
        # ---------------------------------------------------------------------
        cmd_p1 = [
            sys.executable,
            str(runner_script),
            "run_and_pause_on_step2",
            task.id,
            str(db_path),
            str(ws_path),
            str(marker_dir),
        ]
        p1 = subprocess.Popen(cmd_p1, env=sub_env)

        # Wait until Step 2 is actively in-flight
        ready_marker = marker_dir / "step2_ready_to_kill.marker"
        assert wait_for_marker(ready_marker, timeout=12.0), "Process 1 failed to reach Step 2 execution"

        # ---------------------------------------------------------------------
        # 2. Terminate Process 1 mid-flight (True OS Process Termination)
        # ---------------------------------------------------------------------
        p1.kill()
        p1.wait(timeout=5)

        # ---------------------------------------------------------------------
        # 3. Audit on-disk SQLite state immediately after crash
        # ---------------------------------------------------------------------
        s1_post_crash = Ledger.get_step(s1_id, db_path=str(db_path))
        s2_post_crash = Ledger.get_step(s2_id, db_path=str(db_path))
        s3_post_crash = Ledger.get_step(s3_id, db_path=str(db_path))
        task_post_crash = Ledger.get_task(task.id, db_path=str(db_path))

        # Invariant checks:
        assert task_post_crash.status == "running", "Task lock was prematurely released"
        assert s1_post_crash.status == "verified", "Step 1 was not persisted as verified"
        assert s2_post_crash.status == "running", "Step 2 was not left in running status"
        assert s2_post_crash.result_json is None, "Step 2 should have no recorded result"
        assert s3_post_crash.status == "pending", "Step 3 should still be pending"

        # Verify stable idempotency key was persisted to SQLite by Process 1
        s2_args = json.loads(s2_post_crash.args_json)
        initial_key = s2_args.get("idempotency_key")
        assert initial_key is not None, "Idempotency key was not persisted to SQLite"
        assert initial_key.startswith("idem_")

        # Verify real on-disk artifact from Step 1 exists
        f1_path = resolve_workspace_path("step1.txt", task_id=task.id, owner_id=owner_id)
        # Using ws_path directly to verify
        disk_f1 = ws_path / owner_id / task.id / "step1.txt"
        assert disk_f1.exists()
        assert disk_f1.read_text(encoding="utf-8") == "step 1 initial data"

        # ---------------------------------------------------------------------
        # 4. Spawn Process 2: Fresh separate process runs startup recovery & resume
        # ---------------------------------------------------------------------
        cmd_p2 = [
            sys.executable,
            str(runner_script),
            "recover_and_resume",
            task.id,
            str(db_path),
            str(ws_path),
            str(marker_dir),
        ]
        p2 = subprocess.Popen(cmd_p2, env=sub_env)
        p2.wait(timeout=15)
        assert p2.returncode == 0, f"Process 2 failed with exit code {p2.returncode}"

        # ---------------------------------------------------------------------
        # 5. Audit Final Recovery Invariants
        # ---------------------------------------------------------------------
        # A. Step 1 was NOT executed twice
        p2_written_file = marker_dir / "recover_and_resume_written_files.json"
        assert p2_written_file.exists()
        p2_written = json.loads(p2_written_file.read_text(encoding="utf-8"))
        assert "step1.txt" not in p2_written, "CRITICAL: Verified Step 1 was executed twice!"

        # B. Interrupted Step 2 reused the EXACT SAME idempotency key
        p2_http_file = marker_dir / "proc2_http_called.json"
        assert p2_http_file.exists()
        p2_http_calls = json.loads(p2_http_file.read_text(encoding="utf-8"))
        assert len(p2_http_calls) == 1
        retry_key = p2_http_calls[0]["idempotency_key"]
        assert retry_key == initial_key, f"Idempotency key changed! Expected {initial_key}, got {retry_key}"

        # C. Step 3 was executed to completion
        assert "step3.txt" in p2_written
        disk_f3 = ws_path / owner_id / task.id / "step3.txt"
        assert disk_f3.exists()

        # D. All steps verified and task reached final 'complete' state
        summary = Ledger.get_task_summary(task.id, owner_id=owner_id, db_path=str(db_path))
        assert summary["is_complete"] is True
        for step_dict in summary["steps"]:
            assert step_dict["status"] == "verified"
        final_task = Ledger.get_task(task.id, owner_id=owner_id, db_path=str(db_path))
        assert final_task.status == "complete"


def test_real_subprocess_interruption_with_untrusted_mutation_blocks_safely():
    """Test real OS process termination on an untrusted mutation:
    fresh process recovery refuses automatic retry, marks step 'blocked', and pauses."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "subprocess_untrusted.db"
        ws_path = tmp_path / "workspace"
        marker_dir = tmp_path / "markers"
        ws_path.mkdir(parents=True, exist_ok=True)
        marker_dir.mkdir(parents=True, exist_ok=True)

        init_db(str(db_path))

        owner_token = "real_proc_untrusted_token_32_ch!"
        owner_id = hash_owner_token(owner_token)

        # Policy: httpbin.org is allowed, but trusted_idempotency_domains is EMPTY!
        policy = Policy(
            allowed_tools=["write_file", "read_file", "http_request", "list_files"],
            allowed_domains=["httpbin.org"],
            trusted_idempotency_domains=[],  # Untrusted!
            allowed_paths=["*"],
            max_steps=10,
        )
        set_owner_policy(owner_id, policy, db_path=str(db_path))

        task = Ledger.create_task("Untrusted mutation subprocess task", owner_id=owner_id, db_path=str(db_path))
        steps = Ledger.add_steps(
            task.id,
            [
                {
                    "idx": 1,
                    "intent": "Initial write",
                    "tool": "write_file",
                    "args": {"path": "init.txt", "content": "data"},
                    "probe": {},
                    "depends_on": [],
                },
                {
                    "idx": 2,
                    "intent": "Untrusted mutation",
                    "tool": "http_request",
                    "args": {
                        "url": "https://httpbin.org/post",
                        "method": "POST",
                        "json": {"wire": 50000},
                        "supports_idempotency": True,  # Untrusted planner flag
                    },
                    "probe": {"expected_status": 200},
                    "depends_on": [1],
                },
            ],
            db_path=str(db_path),
        )
        s1_id = steps[0].id
        s2_id = steps[1].id

        runner_script = PROJECT_ROOT / "scripts" / "subprocess_recovery_runner.py"

        sub_env = os.environ.copy()
        sub_env["SAHA_DB_PATH"] = str(db_path)
        sub_env["SAHA_WORKSPACE_ROOT"] = str(ws_path)
        sub_env["PYTHONPATH"] = str(PROJECT_ROOT)
        sub_env["NEBIUS_API_KEY"] = ""

        # Spawn Process 1
        p1 = subprocess.Popen([
            sys.executable,
            str(runner_script),
            "run_and_pause_on_step2",
            task.id,
            str(db_path),
            str(ws_path),
            str(marker_dir),
        ], env=sub_env)

        ready_marker = marker_dir / "step2_ready_to_kill.marker"
        assert wait_for_marker(ready_marker, timeout=12.0)

        # Kill Process 1 mid-flight
        p1.kill()
        p1.wait(timeout=5)

        # Spawn Process 2
        p2 = subprocess.Popen([
            sys.executable,
            str(runner_script),
            "recover_and_resume",
            task.id,
            str(db_path),
            str(ws_path),
            str(marker_dir),
        ], env=sub_env)
        p2.wait(timeout=15)
        assert p2.returncode == 0

        # Audit:
        # A. Process 2 did NOT send any HTTP mutation request
        p2_http_file = marker_dir / "proc2_http_called.json"
        assert not p2_http_file.exists() or len(json.loads(p2_http_file.read_text(encoding="utf-8"))) == 0

        # B. Step 2 is blocked with ambiguous mutation reason
        s2_final = Ledger.get_step(s2_id, db_path=str(db_path))
        assert s2_final.status == "blocked"
        assert "not confirmed by trusted owner policy" in (s2_final.block_reason or "")

        # C. An approval is pending for the owner
        approvals = get_owner_approvals(owner_id, status="pending", db_path=str(db_path))
        assert len(approvals) == 1
        assert approvals[0]["step_id"] == s2_id

        # D. Task is paused
        final_task = Ledger.get_task(task.id, owner_id=owner_id, db_path=str(db_path))
        assert final_task.status == "paused"
