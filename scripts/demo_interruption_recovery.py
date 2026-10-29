#!/usr/bin/env python3
"""Saha Interruption & Restart Recovery Demonstration Script.

REAL VS MOCKED ACCOUNTING:
- REAL:
  * SQLite WAL transactional database (persisted to disk at data/demo_recovery.db).
  * State transitions, sequence numbering, and append-only ledger history.
  * Real sandboxed filesystem operations and SHA-256 verifier probes.
  * Real policy checking and approval request creation/resolution.
  * Real idempotency key generation, persistence to SQLite, and reuse across retries.
- MOCKED:
  * External HTTP endpoints: Mocked in-process via simulated responses to deterministically
    demonstrate in-flight network interruption (crashes mid-request) without third-party flakiness.
  * LLM calls (call_llm): Mocked for offline deterministic execution without consuming API quota.

Demonstrates end-to-end task recovery when a process is interrupted mid-execution:
Part A: Sandboxed Filesystem Task Recovery
1. Real SQLite WAL ledger persists Step 1 ('verified') to disk.
2. Step 2 starts execution ('running'), simulating a crash / SIGKILL / power loss.
3. Process restarts: loads task state from SQLite.
4. Proves Step 1 (already verified) is NOT executed twice.
5. Re-executes Step 2 cleanly and executes Step 3 to completion.

Part B: External HTTP Mutation Recovery
1. Interrupted mutation with idempotency key: persists stable key, reuses identical
   key on retry, ensuring remote service deduplication.
2. Interrupted mutation without idempotency: detects ambiguous unverified outcome,
   refuses blind retry, transitions to 'blocked', and requires owner authorization.
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

from backend.db import get_db, get_db_path, hash_owner_token, init_db, check_and_init_data_dirs
from backend.executor import execute_step, run_task
from backend.ledger import Ledger
from backend.policy import approve_request, get_owner_approvals, get_owner_policy, set_owner_policy
from backend.tools.sandbox import resolve_workspace_path


def main():
    print("=" * 80)
    print("SAHA INTERRUPTION & RESTART RECOVERY DEMO")
    print("[REAL SQLITE WAL, REAL DISK PROBES, REAL IDEMPOTENCY / MOCKED EXTERNAL APIS]")
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
    os.environ["SAHA_PUBLIC_MODE"] = "false"
    os.environ["SAHA_DEMO_MODE"] = "true"

    init_db(str(demo_db))
    check_and_init_data_dirs()

    owner_token = "demo_recovery_owner_token_32chars!"
    owner_id = hash_owner_token(owner_token)

    print(f"\n[1] Initialized Dedicated Owner Space & SQLite Database")
    print(f"    Database: {demo_db}")
    print(f"    Owner ID: {owner_id}")

    # =========================================================================
    # PART A: Sandboxed Filesystem Interruption Recovery
    # =========================================================================
    print(f"\n" + "-" * 80)
    print("PART A: SANDBOXED FILESYSTEM TASK RECOVERY")
    print("-" * 80)

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
            "args": {"path": "h1_summary.txt", "content": "H1 Total Revenue:  across 6 months.\n"},
            "probe": {},
            "depends_on": [2],
        },
    ]
    steps = Ledger.add_steps(task.id, steps_data, db_path=str(demo_db))
    print(f"[2] Task Created: '{task.goal}' (ID: {task.id})")
    print(f"    Persisted {len(steps)} steps in SQLite with status='pending'.")

    execution_calls = {"q1_revenue.csv": 0, "q2_revenue.csv": 0, "h1_summary.txt": 0}
    from backend import tools
    real_write = tools.write_file

    def instrumented_write(path, content, task_id=None):
        execution_calls[path] = execution_calls.get(path, 0) + 1
        return real_write(path, content, task_id=task_id)

    with patch("backend.executor.write_file", side_effect=instrumented_write):
        print(f"\n[3] Executing Step 1 (q1_revenue.csv)...")
        s1 = execute_step(steps[0].id, db_path=str(demo_db))
        print(f"    Step 1 Status: {s1.status.upper()}")
        assert s1.status == "verified"
        assert execution_calls["q1_revenue.csv"] == 1

        print(f"\n[4] Simulating Process Interruption (SIGKILL / Crash during Step 2)...")
        Ledger.mark_step_running(steps[1].id, db_path=str(demo_db))
        Ledger.update_task_status(task.id, "running", db_path=str(demo_db))

        s1_db = Ledger.get_step(steps[0].id, db_path=str(demo_db))
        s2_db = Ledger.get_step(steps[1].id, db_path=str(demo_db))
        task_db = Ledger.get_task(task.id, db_path=str(demo_db))

        print(f"    Persisted State in SQLite after Crash:")
        print(f"    - Task Status:   {task_db.status} (Stale lock)")
        print(f"    - Step 1 Status: {s1_db.status} (Finished before crash)")
        print(f"    - Step 2 Status: {s2_db.status} (Interrupted in-flight)")

        print(f"\n[5] Process Restarting (Simulating New Process Startup)...")
        execution_calls["q1_revenue.csv"] = 0
        execution_calls["q2_revenue.csv"] = 0
        execution_calls["h1_summary.txt"] = 0

        t_start = time.time()
        final_task = run_task(task.id, db_path=str(demo_db))
        elapsed = (time.time() - t_start) * 1000

        print(f"    Recovery Complete in {elapsed:.1f} ms! Final Task Status: {final_task.status.upper()}")
        assert execution_calls["q1_revenue.csv"] == 0, "Step 1 was executed twice!"
        assert execution_calls["q2_revenue.csv"] == 1, "Step 2 was not re-executed"
        assert execution_calls["h1_summary.txt"] == 1, "Step 3 was not executed"

    # =========================================================================
    # PART B: External HTTP Mutation Interruption Recovery
    # =========================================================================
    print(f"\n" + "-" * 80)
    print("PART B: EXTERNAL HTTP MUTATION RECOVERY (IDEMPOTENCY & AMBIGUITY HANDLING)")
    print("-" * 80)

    # Scenario 1: Idempotency Key Reuse with Owner-Trusted Policy
    print(f"\n[6] Scenario 1: External Mutation on Trusted Target (Owner-Controlled Idempotency)")
    # Owner configures trusted_idempotency_domains in policy
    pol = get_owner_policy(owner_id, db_path=str(demo_db))
    pol.trusted_idempotency_domains = ["httpbin.org"]
    set_owner_policy(owner_id, pol, db_path=str(demo_db))
    print(f"    Owner Configured Trusted Idempotency Domains: {pol.trusted_idempotency_domains}")

    http_task1 = Ledger.create_task("Payment invoice mutation", owner_id=owner_id, db_path=str(demo_db))
    http_step1 = Ledger.add_steps(
        http_task1.id,
        [
            {
                "idx": 1,
                "intent": "Create invoice via external HTTP POST mutation",
                "tool": "http_request",
                "args": {
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "json": {"amount": 1500, "currency": "USD"},
                    "supports_idempotency": True,
                },
                "probe": {"expected_status": [200, 201]},
                "depends_on": [],
            }
        ],
        db_path=str(demo_db),
    )[0]

    recorded_keys = []

    def mock_idem_post(url, method="GET", headers=None, data=None, json_data=None,
                       timeout=5.0, allowed_domains=None, idempotency_key=None,
                       idempotency_header="Idempotency-Key"):
        recorded_keys.append(idempotency_key)
        return {
            "url": url,
            "method": method,
            "status_code": 201,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"invoice_id": "inv_999", "status": "paid"}),
            "body_length": 45,
            "idempotency_key": idempotency_key,
        }

    # Simulate start of Step 1: generating and persisting key
    with patch("backend.executor.call_llm", return_value="Created invoice."), \
         patch("backend.executor.http_request", side_effect=mock_idem_post):
        # Step starts, generates stable key, persists to SQLite args_json
        execute_step(http_step1.id, db_path=str(demo_db))

    initial_key = recorded_keys[0]
    print(f"    Generated & Persisted Idempotency Key: {initial_key}")

    # Simulate crash before response was recorded
    Ledger.mark_step_running(http_step1.id, db_path=str(demo_db))
    Ledger.update_task_status(http_task1.id, "running", db_path=str(demo_db))
    with get_db(str(demo_db)) as conn:
        conn.execute("UPDATE steps SET result_json = NULL, probe_json = NULL WHERE id = ?", (http_step1.id,))

    recorded_keys.clear()

    # Recover task
    with patch("backend.executor.call_llm", return_value="Created invoice."), \
         patch("backend.executor.http_request", side_effect=mock_idem_post):
        recovered_http_task = run_task(http_task1.id, db_path=str(demo_db))

    retry_key = recorded_keys[0]
    print(f"    Key sent on restart retry:            {retry_key}")
    print(f"    Key Reused Identically across Crash:  {initial_key == retry_key}")
    assert initial_key == retry_key, "Idempotency key changed across restart!"
    assert recovered_http_task.status == "complete"

    # Scenario 2: Ambiguous Mutation without Idempotency (Conservative Halt)
    print(f"\n[7] Scenario 2: External Mutation without Idempotency (Ambiguous Outcome)")
    # Reset trusted domains: legacy bank target is NOT trusted by owner for idempotency
    pol = get_owner_policy(owner_id, db_path=str(demo_db))
    pol.trusted_idempotency_domains = []
    set_owner_policy(owner_id, pol, db_path=str(demo_db))
    print(f"    Owner Configured Trusted Idempotency Domains: {pol.trusted_idempotency_domains}")

    http_task2 = Ledger.create_task("Legacy wire transfer without deduplication", owner_id=owner_id, db_path=str(demo_db))
    http_step2 = Ledger.add_steps(
        http_task2.id,
        [
            {
                "idx": 1,
                "intent": "Execute non-idempotent wire transfer",
                "tool": "http_request",
                "args": {
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "data": "wire_transfer_50000_usd",
                    "supports_idempotency": False,
                },
                "probe": {"expected_status": 200},
                "depends_on": [],
            }
        ],
        db_path=str(demo_db),
    )[0]

    # Simulate crash in-flight before response recorded
    Ledger.mark_step_running(http_step2.id, db_path=str(demo_db))
    Ledger.update_task_status(http_task2.id, "running", db_path=str(demo_db))

    http_calls_during_recovery = []
    with patch("backend.executor.http_request", side_effect=lambda *a, **kw: http_calls_during_recovery.append(kw)):
        ambig_task = run_task(http_task2.id, db_path=str(demo_db))

    s_ambig = Ledger.get_step(http_step2.id, db_path=str(demo_db))
    print(f"    Calls dispatched during recovery:     {len(http_calls_during_recovery)} (MUST BE 0)")
    print(f"    Ambiguous Step Status:               {s_ambig.status.upper()} (Safe reconciliation state)")
    print(f"    Block Reason Recorded:               {s_ambig.block_reason}")
    print(f"    Task Paused Awaiting Owner Decision:  {ambig_task.status.upper()}")

    assert len(http_calls_during_recovery) == 0, "Non-idempotent mutation was blindly retried!"
    assert s_ambig.status == "blocked"
    assert ambig_task.status == "paused"

    # Resolve approval
    pending_approvals = get_owner_approvals(owner_id, status="pending", db_path=str(demo_db))
    print(f"    Pending Owner Approvals Logged:       {len(pending_approvals)}")
    appr = pending_approvals[0]
    print(f"    Authorizing Resume Decision (approve_request)...\n")
    with patch("backend.executor.call_llm", return_value="Transfer executed."), \
         patch("backend.executor.http_request", return_value={"status_code": 200, "body": "ok", "url": "https://httpbin.org/post", "body_length": 2}):
        approve_request(appr["id"], owner_id=owner_id, db_path=str(demo_db))
        final_authorized_task = run_task(http_task2.id, db_path=str(demo_db))

    print(f"    Final Task Status after Authorization: {final_authorized_task.status.upper()}")
    assert final_authorized_task.status == "complete"

    print("\n" + "=" * 80)
    print("DEMO VERDICT: SUCCESS (ALL RECOVERY INVARIANTS REAL & VERIFIED)")
    print("=" * 80)


if __name__ == "__main__":
    main()
