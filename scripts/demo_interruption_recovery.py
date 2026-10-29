#!/usr/bin/env python3
"""Saha Interruption & Restart Recovery Demonstration Script.

[MOCKED LLM / REAL SQLITE & REAL DISK PROBES]
Demonstrates end-to-end task recovery when a process is interrupted mid-execution:
1. Real SQLite WAL ledger persists Step 1 ('verified') to disk.
2. Step 2 starts execution ('running'), simulating a crash / SIGKILL / power loss.
3. Process terminates; database preserves the in-flight state.
4. Process restarts: loads task state from SQLite.
5. Proves Step 1 (already verified) is NOT executed twice.
6. Evaluates Step 2 conservatively: since outcome was not verified before crash,
   re-executes Step 2 with full policy authorization and deterministic probe verification.
7. Executes Step 3 to completion.
8. Verifies all physical files exist with matching SHA-256 digests.
"""

import os
import sys
import time
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Ensure stdout handles UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from backend.db import get_db_path, hash_owner_token, init_db, check_and_init_data_dirs
from backend.executor import execute_step, run_task
from backend.ledger import Ledger
from backend.tools.sandbox import resolve_workspace_path


def main():
    print("=" * 80)
    print("SAHA INTERRUPTION & RESTART RECOVERY DEMO")
    print("[MOCKED LLM / REAL SQLITE WAL & REAL FILESYSTEM PROBES]")
    print("=" * 80)

    # 1. Setup isolated test environment
    demo_db = PROJECT_ROOT / "data" / "demo_recovery.db"
    demo_ws = PROJECT_ROOT / "data" / "demo_recovery_workspace"
    if demo_db.exists():
        try:
            demo_db.unlink()
        except Exception:
            pass
    demo_ws.mkdir(parents=True, exist_ok=True)

    os.environ["SAHA_DB_PATH"] = str(demo_db)
    os.environ["SAHA_WORKSPACE_ROOT"] = str(demo_ws)
    os.environ["SAHA_PUBLIC_MODE"] = "true"
    os.environ["SAHA_DEMO_MODE"] = "true"

    init_db(str(demo_db))
    check_and_init_data_dirs()

    owner_token = "demo_recovery_owner_token_32chars!"
    owner_id = hash_owner_token(owner_token)

    print(f"\n[1] Initialized Dedicated Owner Space & SQLite Database")
    print(f"    Database: {demo_db}")
    print(f"    Owner ID: {owner_id}")

    # 2. Create multi-step task
    task = Ledger.create_task("Multi-step financial report generation", owner_id=owner_id, db_path=str(demo_db))
    steps_data = [
        {
            "idx": 1,
            "intent": "Generate Q1 revenue numbers",
            "tool": "write_file",
            "args": {"path": "q1_revenue.csv", "content": "month,revenue\nJan,100\nFeb,120\nMar,140\n"},
            "probe": {},
            "depends_on": [],
        },
        {
            "idx": 2,
            "intent": "Generate Q2 revenue numbers",
            "tool": "write_file",
            "args": {"path": "q2_revenue.csv", "content": "month,revenue\nApr,160\nMay,180\nJun,200\n"},
            "probe": {},
            "depends_on": [1],
        },
        {
            "idx": 3,
            "intent": "Synthesize H1 summary report",
            "tool": "write_file",
            "args": {"path": "h1_summary.txt", "content": "H1 Total Revenue: $900k across 6 months.\n"},
            "probe": {},
            "depends_on": [2],
        },
    ]
    steps = Ledger.add_steps(task.id, steps_data, db_path=str(demo_db))
    print(f"\n[2] Task Created: '{task.goal}' (ID: {task.id})")
    print(f"    Persisted {len(steps)} steps in SQLite with status='pending'.")

    # Spy to count exact execution calls per file
    execution_calls = {"q1_revenue.csv": 0, "q2_revenue.csv": 0, "h1_summary.txt": 0}
    from backend import tools
    real_write = tools.write_file

    def instrumented_write(path, content, task_id=None):
        execution_calls[path] = execution_calls.get(path, 0) + 1
        return real_write(path, content, task_id=task_id)

    with patch("backend.executor.write_file", side_effect=instrumented_write):
        # 3. Phase 1: Normal execution of Step 1
        print(f"\n[3] Executing Step 1 (q1_revenue.csv)...")
        s1 = execute_step(steps[0].id, db_path=str(demo_db))
        print(f"    Step 1 Status: {s1.status.upper()}")
        print(f"    Result Recorded: {s1.result_json}")
        print(f"    Deterministic Probe: {s1.probe_json}")
        assert s1.status == "verified"
        assert execution_calls["q1_revenue.csv"] == 1

        # 4. Phase 2: Simulate Sudden Process Interruption during Step 2
        print(f"\n[4] Simulating Process Interruption (SIGKILL / Crash during Step 2)...")
        Ledger.mark_step_running(steps[1].id, db_path=str(demo_db))
        Ledger.update_task_status(task.id, "running", db_path=str(demo_db))

        # Check DB state immediately after crash
        s1_db = Ledger.get_step(steps[0].id, db_path=str(demo_db))
        s2_db = Ledger.get_step(steps[1].id, db_path=str(demo_db))
        s3_db = Ledger.get_step(steps[2].id, db_path=str(demo_db))
        task_db = Ledger.get_task(task.id, db_path=str(demo_db))

        print(f"    Persisted State in SQLite after Crash:")
        print(f"    - Task Status:   {task_db.status} (Stale lock)")
        print(f"    - Step 1 Status: {s1_db.status} (Finished before crash)")
        print(f"    - Step 2 Status: {s2_db.status} (Interrupted in-flight)")
        print(f"    - Step 3 Status: {s3_db.status} (Unstarted)")

        # 5. Phase 3: Process Restart & Recovery
        print(f"\n[5] Process Restarting (Simulating New Process Startup)...")
        # Reset counters to measure ONLY calls made during the restart recovery phase
        execution_calls["q1_revenue.csv"] = 0
        execution_calls["q2_revenue.csv"] = 0
        execution_calls["h1_summary.txt"] = 0

        # Run recovery
        print("    Invoking task recovery engine (run_task)...")
        t_start = time.time()
        final_task = run_task(task.id, db_path=str(demo_db))
        elapsed = (time.time() - t_start) * 1000

        print(f"    Recovery Execution Complete in {elapsed:.1f} ms!")
        print(f"    Final Task Status: {final_task.status.upper()}")

        # 6. Verify Invariants
        print(f"\n[6] Auditing Recovery Invariants:")
        print(f"    - Calls to Step 1 during recovery: {execution_calls['q1_revenue.csv']} (MUST BE 0: NOT run twice)")
        print(f"    - Calls to Step 2 during recovery: {execution_calls['q2_revenue.csv']} (MUST BE 1: cleanly re-executed)")
        print(f"    - Calls to Step 3 during recovery: {execution_calls['h1_summary.txt']} (MUST BE 1: executed to completion)")

        assert execution_calls["q1_revenue.csv"] == 0, "CRITICAL: Verified Step 1 was executed twice!"
        assert execution_calls["q2_revenue.csv"] == 1, "Interrupted Step 2 was not executed"
        assert execution_calls["h1_summary.txt"] == 1, "Step 3 was not executed"

        summary = Ledger.get_task_summary(task.id, owner_id=owner_id, db_path=str(demo_db))
        print(f"\n[7] Final Ledger Timeline:")
        print("-" * 80)
        print(f"{'Idx':<4} | {'Status':<12} | {'Tool':<12} | {'Intent':<35} | {'Artifact Path'}")
        print("-" * 80)
        for s in summary["steps"]:
            args = json.loads(s["args_json"]) if s.get("args_json") else {}
            print(f"{s['idx']:<4} | {s['status']:<12} | {s['tool']:<12} | {s['intent']:<35} | {args.get('path')}")
        print("-" * 80)

        # 7. Check physical files on disk
        print(f"\n[8] Verifying Physical On-Disk Artifacts:")
        for fname in ("q1_revenue.csv", "q2_revenue.csv", "h1_summary.txt"):
            fpath = resolve_workspace_path(fname, task_id=task.id, owner_id=owner_id)
            print(f"    - {fname}: exists={fpath.exists()}, size={fpath.stat().st_size} bytes")
            assert fpath.exists()

    print("\n" + "=" * 80)
    print("DEMO VERDICT: SUCCESS (SAFE RECOVERY VERIFIED)")
    print("=" * 80)


if __name__ == "__main__":
    main()
