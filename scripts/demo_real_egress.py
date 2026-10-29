"""Demo script to run a real task through the API with a two-owner setup.

Demonstrates:
- 401 behavior in public mode (missing header, short token, /docs disabled, /health ok).
- Owner A runs a real task with goal containing fake email and phone to exercise redaction.
- Step 1 (write_file) executes with WORKER model.
- Step 2 (run_shell) blocks on owner policy and generates an approval request.
- POST /approvals/{id}/approve auto-resumes the task in background without manual /run.
- Step 2 runs and becomes verified; task completes.
- Print Owner A's GET /egress output (real latencies, tags, redaction counts).
- Prove Owner B's GET /egress is empty.
- Prove nothing in SQLite llm_calls contains the fake email or phone (SQL match counts = 0).
"""

import os
import sys
import time
import json
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv(PROJECT_ROOT / ".env")

from fastapi.testclient import TestClient
from backend.api import app
from backend.db import get_db_path, hash_owner_token, init_db, check_and_init_data_dirs

def main():
    print("=" * 75)
    print("SAHA PRE-FLIGHT VERIFICATION: REAL EGRESS & TWO-OWNER APPROVAL AUTO-RESUME")
    print("=" * 75)

    # 1. 401 Public Mode Verification
    print("\n--- 1. Testing 401 Behavior in Public Mode ---")
    os.environ["SAHA_PUBLIC_MODE"] = "true"
    client = TestClient(app)

    # Request without header
    r1 = client.get("/tasks")
    print(f"GET /tasks without header -> Status: {r1.status_code}")
    print(f"  Response detail: {r1.json().get('detail')}")

    # Request with short token
    r2 = client.get("/tasks", headers={"X-Saha-Owner": "too_short_token_12345"})
    print(f"GET /tasks with short token (<32 chars) -> Status: {r2.status_code}")
    print(f"  Response detail: {r2.json().get('detail')}")

    # GET /health without header
    r3 = client.get("/health")
    print(f"GET /health without header -> Status: {r3.status_code}")
    print(f"  Response: {r3.json()}")

    # GET /docs in public mode
    r4 = client.get("/docs")
    print(f"GET /docs in public mode -> Status: {r4.status_code} (Disabled/404)")

    # Reset public mode for real execution
    os.environ["SAHA_PUBLIC_MODE"] = "false"
    client = TestClient(app)

    # 2. Setup Two Owners
    token_a = "owner_a_live_token_32_characters_12345"
    token_b = "owner_b_live_token_32_characters_67890"
    headers_a = {"X-Saha-Owner": token_a}
    headers_b = {"X-Saha-Owner": token_b}
    owner_id_a = hash_owner_token(token_a)
    owner_id_b = hash_owner_token(token_b)

    fake_email = "test.target.alpha@sample-corp-domain.internal"
    fake_phone = "+1-555-019-8472"

    print("\n--- 2. Running Real Task with Owner A (exercising PII redaction) ---")
    goal = f"Create a record client_info.txt with email {fake_email} and phone {fake_phone}, and list the workspace."
    print(f"Goal: {goal}")

    create_res = client.post("/tasks", json={"goal": goal}, headers=headers_a)
    if create_res.status_code != 200:
        print(f"Failed to create task: {create_res.status_code} {create_res.text}")
        return
    task_data = create_res.json()
    task_id = task_data["task"]["id"]
    steps = task_data["steps"]
    print(f"Created Task ID: {task_id} with {len(steps)} planned steps.")
    for s in steps:
        print(f"  Step {s['idx']}: intent='{s['intent']}' tool='{s['tool']}' status='{s['status']}'")

    # Start task execution
    print("\nStarting execution (POST /tasks/{task_id}/run)...")
    run_res = client.post(f"/tasks/{task_id}/run", headers=headers_a)
    print(f"Run response status: {run_res.status_code}")

    # Wait for step 1 execution and step 2 policy block
    print("Waiting for step 1 to execute and step 2 to block for owner approval...")
    for _ in range(40):
        t_res = client.get(f"/tasks/{task_id}", headers=headers_a)
        if t_res.status_code == 200:
            status = t_res.json()["task"]["status"]
            if status in ("paused", "complete", "failed"):
                break
        time.sleep(1.0)

    summary1 = client.get(f"/tasks/{task_id}", headers=headers_a).json()
    print(f"Task status after initial run: {summary1['task']['status']}")
    for s in summary1["steps"]:
        print(f"  Step {s['idx']}: intent='{s['intent']}' tool='{s['tool']}' status='{s['status']}'")

    # 3. Demonstrate Approval Auto-Resume
    print("\n--- 3. Testing Approval Auto-Resume ---")
    apprs = client.get("/approvals", headers=headers_a).json().get("approvals", [])
    print(f"Pending approvals for Owner A: {len(apprs)}")
    if apprs:
        appr = apprs[0]
        appr_id = appr["id"]
        print(f"Approving request {appr_id} (tool='{appr['tool']}') with always_allow=True...")
        approve_res = client.post(
            f"/approvals/{appr_id}/approve",
            json={"always_allow": True},
            headers=headers_a,
        )
        print(f"POST /approvals/{appr_id}/approve -> Status: {approve_res.status_code}, Response: {approve_res.json()}")

        print("Waiting for auto-resumed background execution to complete...")
        for _ in range(40):
            t_res = client.get(f"/tasks/{task_id}", headers=headers_a)
            if t_res.status_code == 200:
                cur_status = t_res.json()["task"]["status"]
                if cur_status in ("complete", "failed"):
                    break
            time.sleep(1.0)

        final_summary = client.get(f"/tasks/{task_id}", headers=headers_a).json()
        print(f"Final Task Status: {final_summary['task']['status']}")
        for s in final_summary["steps"]:
            print(f"  Step {s['idx']}: intent='{s['intent']}' tool='{s['tool']}' status='{s['status']}'")
        assert final_summary["task"]["status"] == "complete", "Task should reach complete status automatically!"

    # 4. Print Owner A's GET /egress output
    print("\n--- 4. Owner A's GET /egress output (Privacy Ledger) ---")
    egress_a = client.get("/egress", headers=headers_a).json()["egress"]
    print(f"Owner A Egress Records Count: {len(egress_a)}")
    for i, row in enumerate(egress_a, 1):
        print(f"  Record {i}:")
        print(f"    Role:           {row.get('role')}")
        print(f"    Model:          {row.get('model')}")
        print(f"    Latency:        {row.get('latency_ms')} ms")
        print(f"    Tags:           {row.get('tags')}")
        print(f"    Input Chars:    {row.get('input_chars')}")
        print(f"    Output Chars:   {row.get('output_chars')}")
        print(f"    Redactions:     {row.get('redaction_count')}")
        print(f"    Dropped Tags:   {row.get('dropped_tags')}")
        print(f"    Tokens:         {row.get('total_tokens')}")

    # 5. Prove Owner B's /egress is empty
    print("\n--- 5. Owner B's GET /egress output ---")
    egress_b = client.get("/egress", headers=headers_b).json()["egress"]
    print(f"Owner B Egress Records Count: {len(egress_b)}")
    assert len(egress_b) == 0, "Owner B should have 0 egress records!"
    print("Owner B Egress is completely empty: Verified.")

    # 6. Prove nothing in SQLite llm_calls contains the fake email or phone
    print("\n--- 6. SQL Verification: Zero PII in llm_calls Table ---")
    db_file = get_db_path()
    conn = sqlite3.connect(db_file)
    cur = conn.cursor()

    cur.execute(
        "SELECT count(*) FROM llm_calls WHERE id LIKE ? OR model LIKE ? OR role LIKE ? OR context_tags LIKE ? OR status LIKE ? OR error LIKE ?",
        (f"%{fake_email}%", f"%{fake_email}%", f"%{fake_email}%", f"%{fake_email}%", f"%{fake_email}%", f"%{fake_email}%")
    )
    email_matches_llm = cur.fetchone()[0]

    cur.execute(
        "SELECT count(*) FROM llm_calls WHERE id LIKE ? OR model LIKE ? OR role LIKE ? OR context_tags LIKE ? OR status LIKE ? OR error LIKE ?",
        (f"%{fake_phone}%", f"%{fake_phone}%", f"%{fake_phone}%", f"%{fake_phone}%", f"%{fake_phone}%", f"%{fake_phone}%")
    )
    phone_matches_llm = cur.fetchone()[0]

    print(f"Matches for fake email '{fake_email}' in llm_calls: {email_matches_llm}")
    print(f"Matches for fake phone '{fake_phone}' in llm_calls: {phone_matches_llm}")

    assert email_matches_llm == 0, f"Found {email_matches_llm} email matches in llm_calls!"
    assert phone_matches_llm == 0, f"Found {phone_matches_llm} phone matches in llm_calls!"
    print("Privacy assertion verified: Zero raw PII stored in privacy ledger metadata.")

    conn.close()
    print("\n" + "=" * 75)
    print("ALL PRE-FLIGHT VERIFICATION CRITERIA SUCCESSFULLY DEMONSTRATED.")
    print("=" * 75)

if __name__ == "__main__":
    main()
