#!/usr/bin/env python3
"""Saha Real Approval Demonstration Script.

Runs a REAL end-to-end task against live Nebius models and live HTTP endpoints:
1. Real PLANNER model generates the execution plan.
2. Step targets reachable non-allowlisted domain https://www.python.org.
3. Policy blocks the step: status becomes 'blocked', block_reason is recorded, drift_reason is null.
4. User approves the step via POST /approvals/{id}/approve with always_allow=true.
5. Approval clears block_reason (now null), keeps drift_reason null, updates owner policy.
6. Real WORKER model executes and interprets the real HTTP 200 response from https://www.python.org.
7. Step is verified and task completes.
8. Queries and prints real timestamps, models, latencies, and token counts from llm_calls.
"""

import os
import sys
import time
import json
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

# Ensure stdout handles UTF-8 on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env (NEBIUS_API_KEY)
load_dotenv(PROJECT_ROOT / ".env")

# Ensure environment variables for demo
demo_db = PROJECT_ROOT / "data" / "demo_real_approval.db"
demo_ws = PROJECT_ROOT / "data" / "demo_workspace"
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

from starlette.testclient import TestClient
from backend.api import app
from backend.db import get_db_path, hash_owner_token, init_db, check_and_init_data_dirs
from backend.ledger import Ledger
from backend.policy import get_owner_policy


def main():
    print("=" * 80)
    print("SAHA REAL APPROVAL DEMO (LIVE LLM & LIVE HTTP)")
    print("=" * 80)

    api_key = os.getenv("NEBIUS_API_KEY", "")
    if not api_key:
        print("ERROR: NEBIUS_API_KEY is not set in environment or .env file.")
        sys.exit(1)

    # Initialize database and directories
    check_and_init_data_dirs()
    init_db()

    client = TestClient(app)
    owner_token = "demo_real_owner_token_32chars_ok!"
    headers = {"X-Saha-Owner": owner_token}
    owner_id = hash_owner_token(owner_token)

    print(f"\n[1] Initialized Owner Space")
    print(f"    Owner Token: {owner_token[:8]}... (len={len(owner_token)})")
    print(f"    Owner ID (SHA-256): {owner_id}")
    init_policy = get_owner_policy(owner_id)
    print(f"    Initial Allowed Domains: {init_policy.allowed_domains}")

    # Step 1: Create Task with a goal targeting python.org
    goal = "Fetch the python.org homepage to check current release news"
    print(f"\n[2] Submitting Task to Real PLANNER...")
    print(f"    Goal: '{goal}'")
    t0_plan = time.time()
    create_res = client.post("/tasks", json={"goal": goal}, headers=headers)
    t_plan_wall = (time.time() - t0_plan) * 1000
    if create_res.status_code != 200:
        print(f"ERROR: Failed to create task: {create_res.status_code} {create_res.text}")
        sys.exit(1)

    task_data = create_res.json()
    task_id = task_data["task"]["id"]
    print(f"    Task ID: {task_id}")
    print(f"    Plan Generation Wall Time: {t_plan_wall:.1f} ms")
    print(f"    Steps Proposed: {len(task_data.get('steps', []))}")

    # Inspect initial step status
    steps = task_data.get("steps", [])
    for s in steps:
        print(f"    - Step {s['idx']}: tool='{s['tool']}', status='{s['status']}', intent='{s['intent']}'")

    # Step 2: Check blocked step
    blocked_step = None
    for s in steps:
        if s["status"] == "blocked":
            blocked_step = s
            break

    if not blocked_step:
        # Trigger task run if not already blocked at plan creation
        print(f"\n[3] Triggering Task Execution (POST /tasks/{task_id}/run)...")
        run_res = client.post(f"/tasks/{task_id}/run", headers=headers)
        assert run_res.status_code == 200, f"Run error: {run_res.text}"

        # Poll until step is blocked
        print("    Waiting for policy engine to evaluate and block the step...")
        for _ in range(40):
            detail = client.get(f"/tasks/{task_id}", headers=headers).json()
            active_steps = detail.get("steps", [])
            if active_steps and active_steps[0]["status"] == "blocked":
                blocked_step = active_steps[0]
                break
            time.sleep(0.2)

    if not blocked_step:
        print(f"ERROR: Step was not marked blocked. Current steps: {task_data.get('steps')}")
        sys.exit(1)

    print(f"\n[4] Step Successfully Blocked by Policy Engine:")
    print(f"    Step ID: {blocked_step['id']}")
    print(f"    Status: {blocked_step['status']}")
    print(f"    block_reason: {blocked_step.get('block_reason')}")
    print(f"    drift_reason: {blocked_step.get('drift_reason')} (MUST BE None)")

    assert blocked_step["block_reason"] is not None, "block_reason must be populated"
    assert blocked_step["drift_reason"] is None, "drift_reason must be None for policy block"

    # Step 3: Check Pending Approvals
    appr_res = client.get("/approvals?status=pending", headers=headers)
    approvals = appr_res.json().get("approvals", [])
    print(f"\n[5] Pending Approvals for Owner (GET /approvals):")
    print(f"    Found {len(approvals)} pending approval(s).")
    target_appr = [a for a in approvals if a["step_id"] == blocked_step["id"]][0]
    print(f"    Approval ID: {target_appr['id']}")
    print(f"    Requested Tool: {target_appr['tool']}")
    print(f"    Requested Args: {target_appr['args']}")
    print(f"    Reason: {target_appr['reason']}")

    # Step 4: Approve with always_allow=true
    print(f"\n[6] Approving Request (POST /approvals/{target_appr['id']}/approve, always_allow=true)...")
    appr_post = client.post(
        f"/approvals/{target_appr['id']}/approve",
        headers=headers,
        json={"always_allow": True},
    )
    assert appr_post.status_code == 200, f"Approve failed: {appr_post.text}"
    print(f"    Approve Response: {appr_post.json()}")

    # Step 5: Wait for automatic background resumption and completion
    print("\n[7] Waiting for Automatic Background Resumption & Verification...")
    completed_task = None
    for i in range(50):
        detail = client.get(f"/tasks/{task_id}", headers=headers).json()
        task_info = detail.get("task", {})
        status = task_info.get("status")
        step_0 = detail.get("steps", [])[0] if detail.get("steps") else {}
        print(f"    Poll [{i+1:02d}]: task_status='{status}', step_status='{step_0.get('status')}', block_reason='{step_0.get('block_reason')}'")
        if status == "complete" and step_0.get("status") == "verified":
            completed_task = detail
            break
        time.sleep(1.0)

    if not completed_task:
        print(f"ERROR: Task did not complete successfully within timeout. Current status: {status}")
        sys.exit(1)

    final_step = completed_task["steps"][0]
    print(f"\n[8] Task Execution & Reality Verification Complete!")
    print(f"    Task Status: {completed_task['task']['status']}")
    print(f"    Step Status: {final_step['status']}")
    print(f"    block_reason: {final_step.get('block_reason')} (CLEARED / NULL)")
    print(f"    drift_reason: {final_step.get('drift_reason')} (NULL)")
    worker_interp = final_step.get("result", {}).get("interpretation", "")
    print(f"    Worker Interpretation: {worker_interp}")

    assert final_step["block_reason"] is None, "block_reason must be cleared upon approval"
    assert final_step["drift_reason"] is None, "drift_reason must remain None"

    # Step 6: Verify Policy was updated
    updated_policy = get_owner_policy(owner_id)
    print(f"\n[9] Owner Policy Updated Dynamically:")
    print(f"    Updated Allowed Domains: {updated_policy.allowed_domains}")
    assert "www.python.org" in updated_policy.allowed_domains or "python.org" in updated_policy.allowed_domains

    # Step 7: Query llm_calls table for real timestamps, models, latencies, and tokens
    print(f"\n[10] Real LLM Call Metrics Recorded in SQLite (`llm_calls`):")
    print("-" * 115)
    print(f"{'Created At (UTC)':<26} | {'Role':<8} | {'Model':<38} | {'Latency (ms)':<12} | {'Tokens':<10}")
    print("-" * 115)

    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT created_at, role, model, latency_ms, prompt_tokens, completion_tokens, total_tokens
        FROM llm_calls
        WHERE owner_id = ?
        ORDER BY created_at ASC
        """,
        (owner_id,),
    ).fetchall()

    for r in rows:
        tokens_str = f"{r['total_tokens']} ({r['prompt_tokens']}+{r['completion_tokens']})"
        print(f"{r['created_at']:<26} | {r['role']:<8} | {r['model']:<38} | {r['latency_ms']:<12.1f} | {tokens_str:<10}")

    print("-" * 115)
    print(f"Total Live LLM Calls Recorded for Owner: {len(rows)}")
    conn.close()

    print("\n" + "=" * 80)
    print("DEMO VERDICT: SUCCESS (REAL LIVE LLM & REAL LIVE HTTP)")
    print("=" * 80)


if __name__ == "__main__":
    main()
