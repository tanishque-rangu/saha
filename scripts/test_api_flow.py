"""Run complete end-to-end flow through the Saha HTTP API.

Flow:
1. POST /tasks: Create task with goal
2. POST /tasks/{id}/run: Execute task
3. Poll GET /tasks/{id} until verified
4. POST /tasks/{id}/sabotage: Simulate drift by deleting disk artifact
5. POST /tasks/{id}/resume: Trigger reconciliation & repair
6. Poll GET /tasks/{id} until complete
7. GET /tasks/{id}: Final state verification
"""

import json
import sys
import time
from typing import Any, Dict

import httpx

# Windows UTF-8 stdout
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def run_api_flow(base_url: str = "http://127.0.0.1:8000"):
    t0 = time.time()
    print("=" * 90)
    print(f" SAHA HTTP API END-TO-END FLOW: {base_url}")
    print("=" * 90)

    with httpx.Client(base_url=base_url, timeout=120.0) as client:
        # Check health
        health = client.get("/health").json()
        print(f"\n[1] Health Check: {health}")

        goal = (
            "Create two small data files with different facts about Hyderabad, "
            "combine them into a summary file (depends on steps 1 and 2), and check that https://example.com is reachable."
        )

        # 1. Create task
        print(f"\n[2] POST /tasks -> Creating task...")
        t_create = time.time()
        resp_create = client.post("/tasks", json={"goal": goal})
        if resp_create.status_code != 200:
            print(f"FAILED: {resp_create.text}")
            return
        data_create = resp_create.json()
        task_id = data_create["task"]["id"]
        print(f"    Created Task ID: {task_id} (elapsed: {time.time() - t_create:.2f}s)")
        print(f"    Planned steps count: {len(data_create['steps'])}")

        # 2. Run task
        print(f"\n[3] POST /tasks/{task_id}/run -> Starting execution...")
        resp_run = client.post(f"/tasks/{task_id}/run")
        print(f"    Run initiated: {resp_run.json()}")

        # Poll until complete
        print("    Polling GET /tasks/{id} until initial run finishes...")
        time.sleep(1.5)
        while True:
            st = client.get(f"/tasks/{task_id}").json()
            task_status = st["task"]["status"]
            if task_status in ("complete", "failed", "paused"):
                print(f"    Initial execution finished: status={task_status.upper()}")
                break
            time.sleep(1.0)

        # 3. Sabotage
        print(f"\n[4] POST /tasks/{task_id}/sabotage -> Simulating drift...")
        resp_sab = client.post(f"/tasks/{task_id}/sabotage")
        sab_data = resp_sab.json()
        print(f"    Sabotage output: {sab_data}")

        # 4. Resume
        print(f"\n[5] POST /tasks/{task_id}/resume -> Reconciling & repairing...")
        t_resume = time.time()
        resp_res = client.post(f"/tasks/{task_id}/resume")
        print(f"    Resume initiated: {resp_res.json()}")

        # Poll until complete
        print("    Polling GET /tasks/{id} until reconciliation & repairs finish...")
        time.sleep(1.5)
        while True:
            st = client.get(f"/tasks/{task_id}").json()
            task_status = st["task"]["status"]
            if task_status in ("complete", "failed"):
                print(f"    Reconciliation finished: status={task_status.upper()} (elapsed: {time.time() - t_resume:.2f}s)")
                break
            time.sleep(1.0)


        # 5. Final GET /tasks/{id}
        print(f"\n[6] Final GET /tasks/{task_id} Detail:")
        final_detail = client.get(f"/tasks/{task_id}").json()
        
        print("\n" + "=" * 90)
        print(" FINAL AUDIT LEDGER STATE (via HTTP API)")
        print("=" * 90)
        print(f"{'Seq':<5} {'Idx':<4} {'Status':<14} {'Tool':<12} {'Intent':<50}")
        print("-" * 90)
        for s in final_detail["steps"]:
            status_badge = f"[{s['status'].upper():<10}]"
            print(f"{s['seq']:<5} {s['idx']:<4} {status_badge:<14} {s['tool']:<12} {s['intent'][:48]:<50}")
            if s.get("drift_reason"):
                print(f"       └─ Drift Reason: {s['drift_reason']}")
            if s.get("resolved_by"):
                print(f"       └─ Resolved By: Step ID {s['resolved_by'][:8]}...")
            if s.get("superseded_by"):
                print(f"       └─ Superseded By: Step ID {s['superseded_by'][:8]}...")

        print("=" * 90)
        print(f"\nDrift Events: {len(final_detail['drift_events'])}")
        for de in final_detail["drift_events"]:
            print(f"  • [{de['detected_at']}] Step {de['step_id'][:8]} ({de['tool']}): {de['reason']}")
            print(f"    Fast Auditor: \"{de.get('explanation', '')}\"")

        wall_clock = time.time() - t0
        print(f"\nTotal Wall-Clock Time for Full API Run: {wall_clock:.2f} seconds")
        print("=" * 90 + "\n")


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    run_api_flow(url)
