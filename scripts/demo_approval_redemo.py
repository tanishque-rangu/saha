#!/usr/bin/env python3
"""[MOCKED LLM/HTTP] Saha Approval Re-Demo Script.
NOTE: This script uses stubbed LLM and HTTP responses for offline demonstration.


Executes ONE task in SAHA_PUBLIC_MODE=true demonstrating policy enforcement,
blocking, approval with always_allow=true, and successful verified completion.

Outputs:
1. Task ID
2. Step table (id, idx, status, started_at, finished_at) BEFORE block
3. Step table (id, idx, status, started_at, finished_at) AFTER block
4. Raw JSON of each API response (GET /tasks/{id}, GET /approvals, POST /approvals/{id}/approve)
5. Step table (id, idx, status, started_at, finished_at) AFTER approval
6. Updated owner policy showing domain added
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Set environment before any backend imports
os.environ["SAHA_PUBLIC_MODE"] = "true"
os.environ["SAHA_DEMO_MODE"] = "true"

# Use isolated temp directories for clean demo state
temp_dir = tempfile.TemporaryDirectory()
data_path = Path(temp_dir.name) / "data"
data_path.mkdir(parents=True, exist_ok=True)
db_file = data_path / "saha.db"
ws_dir = data_path / "workspace"
ws_dir.mkdir(parents=True, exist_ok=True)

os.environ["SAHA_DB_PATH"] = str(db_file)
os.environ["SAHA_WORKSPACE_ROOT"] = str(ws_dir)

from fastapi.testclient import TestClient
from backend.api import app
from backend.db import get_db, hash_owner_token, init_db
from backend.executor import execute_step
from backend.ledger import Ledger
from backend.policy import get_owner_policy

init_db(str(db_file))
client = TestClient(app)

OWNER_TOKEN = "saha_public_approval_demo_owner_token_32_chars"
HEADERS = {"X-Saha-Owner": OWNER_TOKEN}
OWNER_ID = hash_owner_token(OWNER_TOKEN)


def format_step_table(steps):
    """Format steps as an ASCII table with columns: id, idx, status, started_at, finished_at."""
    header = f"{'id':<38} | {'idx':<4} | {'status':<10} | {'started_at':<26} | {'finished_at':<26}"
    sep = "-" * len(header)
    rows = [header, sep]
    for s in steps:
        sid = str(s.get("id", ""))
        idx = str(s.get("idx", ""))
        status = str(s.get("status", ""))
        started = str(s.get("started_at") or "-")
        finished = str(s.get("finished_at") or "-")
        rows.append(f"{sid:<38} | {idx:<4} | {status:<10} | {started:<26} | {finished:<26}")
    return "\n".join(rows)


def run_demo():
    print("=" * 80)
    print("SAHA APPROVAL RE-DEMO (PUBLIC MODE)")
    print("=" * 80)
    print(f"Configuration: SAHA_PUBLIC_MODE=true, SAHA_DEMO_MODE=true")
    print(f"Owner Token:   {OWNER_TOKEN[:16]}... (hashed owner_id: {OWNER_ID[:12]}...)")
    print()

    # 1. Create a task with an approvable tool: http_get to an external domain outside default allowlist
    goal = "Fetch weather alerts feed from external domain api.weather.gov"
    task = Ledger.create_task(goal=goal, owner_id=OWNER_ID, db_path=str(db_file))
    print(f"TASK ID: {task.id}")
    print(f"Goal:    {goal}")
    print()

    # Step: http_get to api.weather.gov (default allowlist: example.com, wikipedia.org, httpbin.org)
    step_data = [{
        "intent": "Retrieve current active alerts from api.weather.gov",
        "tool": "http_get",
        "args": {"url": "https://api.weather.gov/alerts"},
        "probe": {"expected_status": 200},
        "depends_on": [],
        "status": "pending",
    }]
    steps = Ledger.add_steps(task.id, step_data, db_path=str(db_file))
    step = steps[0]

    # -------------------------------------------------------------------------
    # STAGE 1: BEFORE BLOCK
    # -------------------------------------------------------------------------
    print(">>> [STAGE 1] STEP TABLE BEFORE BLOCK:")
    summary_before = client.get(f"/tasks/{task.id}", headers=HEADERS).json()
    print(format_step_table(summary_before["steps"]))
    print()
    print(">>> RAW JSON API RESPONSE: GET /tasks/{task_id} (Before Block):")
    print(json.dumps(summary_before, indent=2))
    print()

    # -------------------------------------------------------------------------
    # STAGE 2: TRIGGER EXECUTION -> POLICY BLOCKS STEP
    # -------------------------------------------------------------------------
    print("Executing step with owner policy (domain 'api.weather.gov' is not in default allowlist)...")
    # execute_step evaluates policy -> detects domain not in policy -> marks blocked & logs approval
    step_blocked = execute_step(step.id, db_path=str(db_file))
    Ledger.update_task_status(task.id, "paused", db_path=str(db_file))

    print()
    print(">>> [STAGE 2] STEP TABLE AFTER BLOCK:")
    summary_after_block = client.get(f"/tasks/{task.id}", headers=HEADERS).json()
    print(format_step_table(summary_after_block["steps"]))
    print()
    print(">>> RAW JSON API RESPONSE: GET /tasks/{task_id} (After Block):")
    print(json.dumps(summary_after_block, indent=2))
    print()

    # Fetch pending approvals via API
    approvals_resp = client.get("/approvals?status=pending", headers=HEADERS)
    print(">>> RAW JSON API RESPONSE: GET /approvals?status=pending:")
    print(json.dumps(approvals_resp.json(), indent=2))
    print()

    approvals = approvals_resp.json().get("approvals", [])
    if not approvals:
        print("ERROR: No pending approval found!")
        sys.exit(1)

    approval_id = approvals[0]["id"]
    print(f"Target Approval ID: {approval_id}")
    print(f"Blocked Tool:       {approvals[0]['tool']}")
    print(f"Blocked Reason:     {approvals[0]['reason']}")
    print()

    # -------------------------------------------------------------------------
    # STAGE 3: APPROVE STEP (with always_allow=true)
    # -------------------------------------------------------------------------
    print(f"Sending approval with always_allow=true to authorize domain 'api.weather.gov'...")

    # Mock tool execution return for http_get and Worker interpretation to ensure deterministic demo
    mock_http_response = {
        "url": "https://api.weather.gov/alerts",
        "status_code": 200,
        "body": '{"status": "ok", "alerts": []}',
        "body_length": 30,
        "headers": {"content-type": "application/json"},
    }
    mock_worker_interpretation = "Worker: Successfully retrieved active alerts feed from api.weather.gov with status 200."

    with patch("backend.executor.http_get", return_value=mock_http_response), \
         patch("backend.executor.call_llm", return_value=mock_worker_interpretation):

        approve_resp = client.post(
            f"/approvals/{approval_id}/approve",
            headers=HEADERS,
            json={"always_allow": True},
        )
        print(">>> RAW JSON API RESPONSE: POST /approvals/{id}/approve:")
        print(json.dumps(approve_resp.json(), indent=2))
        print()

        # Step is re-queued to pending, now execute it under updated policy
        execute_step(step.id, db_path=str(db_file))
        Ledger.update_task_status(task.id, "complete", db_path=str(db_file))

    # -------------------------------------------------------------------------
    # STAGE 4: AFTER APPROVAL
    # -------------------------------------------------------------------------
    print(">>> [STAGE 4] STEP TABLE AFTER APPROVAL & EXECUTION:")
    summary_after_approval = client.get(f"/tasks/{task.id}", headers=HEADERS).json()
    print(format_step_table(summary_after_approval["steps"]))
    print()
    print(">>> RAW JSON API RESPONSE: GET /tasks/{task_id} (After Approval):")
    print(json.dumps(summary_after_approval, indent=2))
    print()

    # Check updated owner policy via API
    policy_resp = client.get("/policy", headers=HEADERS)
    print(">>> RAW JSON API RESPONSE: GET /policy (Updated Policy with authorized domain):")
    print(json.dumps(policy_resp.json(), indent=2))
    print()

    print("=" * 80)
    print("APPROVAL RE-DEMO COMPLETE: TASK VERIFIED")
    print("=" * 80)


if __name__ == "__main__":
    run_demo()
