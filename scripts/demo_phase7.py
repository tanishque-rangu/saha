"""Real Demonstration Script for Phase 7-Lite: Always-On Watcher, Restart Recovery, Scheduled Routines.

Executes real Nemotron LLM calls with:
1. Run a task that writes two files (real PLANNER and WORKER).
2. Delete one file directly on disk; do NOT call resume; wait for the Watcher.
3. Print activity feed showing drift detected then healed.
4. Print final step table (original step drifted, repair appended, verified).
5. Print llm_calls rows created during the heal.
6. Schedule a saved routine every minute and show two automatic runs with updated stats.
"""

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Load real Nebius API key from .env without printing it
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("NEBIUS_API_KEY=") and not os.getenv("NEBIUS_API_KEY"):
            val = line.split("=", 1)[1].strip().strip('"').strip("'")
            os.environ["NEBIUS_API_KEY"] = val

from fastapi.testclient import TestClient

from backend.api import IP_POST_HISTORY, OWNER_POST_HISTORY, app
from backend.db import get_db, hash_owner_token, init_db, utc_now_iso
from backend.ledger import Ledger
from backend.policy import Policy, set_owner_policy
from backend.skills import get_skill
from backend.tools.sandbox import resolve_workspace_path
from backend.watcher import get_global_watcher


def run_phase7_demo():
    print("=" * 78)
    print("SAHA PHASE 7-LITE: ALWAYS-ON WATCHER & SCHEDULED ROUTINES REAL DEMO")
    print("=" * 78)

    # 1. Setup isolated temporary demo environment
    tmp_dir = tempfile.mkdtemp()
    demo_db = Path(tmp_dir) / "demo_phase7.db"
    demo_ws = Path(tmp_dir) / "workspace"
    demo_ws.mkdir()

    os.environ["SAHA_DB_PATH"] = str(demo_db)
    os.environ["SAHA_WORKSPACE_ROOT"] = str(demo_ws)
    os.environ["SAHA_WATCHER_ENABLED"] = "true"
    os.environ["SAHA_WATCH_INTERVAL"] = "15"
    os.environ["SAHA_MIN_SCHEDULE_MINUTES"] = "1"
    os.environ["SAHA_DEMO_MODE"] = "true"
    os.environ["SAHA_PUBLIC_MODE"] = "false"

    init_db(str(demo_db))
    IP_POST_HISTORY.clear()
    OWNER_POST_HISTORY.clear()

    owner_token = "phase7_demo_owner_token_abcdef123456"
    headers = {"X-Saha-Owner": owner_token}
    hashed_owner = hash_owner_token(owner_token)

    # Ensure owner policy has auto_heal enabled
    policy = Policy(auto_heal=True)
    set_owner_policy(hashed_owner, policy, db_path=str(demo_db))

    print(f"\n[Environment Configuration]")
    print(f"  * SAHA_WATCH_INTERVAL: 15 seconds")
    print(f"  * SAHA_MIN_SCHEDULE_MINUTES: 1 minute")
    print(f"  * Owner Policy: auto_heal = True")
    print(f"  * Database: {demo_db.name} (isolated SQLite WAL)")

    with TestClient(app) as client:
        # Verify watcher status endpoint
        w_status = client.get("/watcher/status", headers=headers).json()
        print(f"  * Watcher Status: enabled={w_status['enabled']}, interval={w_status['interval']}s, hourly_budget={w_status['heals_remaining_hour']}/3 remaining")

        # -----------------------------------------------------------------
        # STEP 1: Run a task that writes two files
        # -----------------------------------------------------------------
        print("\n" + "-" * 78)
        print("STEP 1: Create and execute a task that writes two files")
        print("-" * 78)

        goal = "Write two project files: config.json with service settings and status.txt with system readiness."
        print(f"Creating task with goal: '{goal}'")
        create_res = client.post("/tasks", json={"goal": goal}, headers=headers)
        assert create_res.status_code == 200, f"Task creation failed: {create_res.text}"
        task_id = create_res.json()["task"]["id"]
        print(f"Task created: {task_id}")

        print("Executing task via real Nemotron PLANNER and WORKER...")
        run_res = client.post(f"/tasks/{task_id}/run", headers=headers)
        assert run_res.status_code == 200

        # Poll until complete
        deadline = time.time() + 90
        final_detail = None
        while time.time() < deadline:
            detail = client.get(f"/tasks/{task_id}", headers=headers).json()
            if detail.get("is_complete"):
                final_detail = detail
                break
            time.sleep(1.0)

        assert final_detail is not None, "Task failed to complete in time"
        print(f"Task {task_id} completed successfully with status: {final_detail['task']['status']}")

        task_ws = resolve_workspace_path(".", task_id=task_id, owner_id=hashed_owner)
        file_config = task_ws / "config.json"
        file_status = task_ws / "status.txt"
        print(f"Verified files on disk:")
        print(f"  - config.json: exists={file_config.exists()}, bytes={file_config.stat().st_size if file_config.exists() else 0}")
        print(f"  - status.txt:  exists={file_status.exists()}, bytes={file_status.stat().st_size if file_status.exists() else 0}")

        # -----------------------------------------------------------------
        # STEP 2: Delete one file on disk; do NOT call resume
        # -----------------------------------------------------------------
        print("\n" + "-" * 78)
        print("STEP 2: Simulate drift: delete status.txt directly on disk (NO manual resume)")
        print("-" * 78)
        assert file_status.exists(), "status.txt must exist before deletion"
        file_status.unlink()
        assert not file_status.exists(), "status.txt was not removed"
        print(">> Deleted status.txt directly from disk.")
        print(">> Waiting for Always-On Watcher cycle to detect drift and auto-heal...")

        # -----------------------------------------------------------------
        # STEP 3: Wait for Watcher to detect drift and auto-heal
        # -----------------------------------------------------------------
        heal_deadline = time.time() + 120
        drift_detected = False
        auto_healed = False
        while time.time() < heal_deadline:
            with get_db(str(demo_db)) as conn:
                event = conn.execute(
                    "SELECT * FROM drift_events WHERE task_id = ? ORDER BY detected_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()

            if event:
                if not drift_detected:
                    drift_detected = True
                    print(f"  [Watcher Event] Drift detected on step '{event['step_id']}' at {event['detected_at']}!")
                    print(f"  [Watcher Event] Reason: {event['reason']}")
                    print(f"  [Watcher Event] Explanation: {event['explanation']}")
                    print(f"  [Watcher Event] Auto-heal launched in background (budget allows)...")

                if event["healed_at"] is not None and event["auto_healed"] == 1:
                    auto_healed = True
                    print(f"  [Watcher Event] Task successfully auto-healed at {event['healed_at']}!")
                    break

            time.sleep(2.0)

        assert drift_detected, "Watcher did not detect drift within timeout"
        assert auto_healed, "Watcher did not auto-heal within timeout"
        assert file_status.exists(), "status.txt was not restored on disk"
        print(f">> Restored status.txt on disk: exists={file_status.exists()}, bytes={file_status.stat().st_size}")

        # -----------------------------------------------------------------
        # STEP 4: Print Activity Feed
        # -----------------------------------------------------------------
        print("\n" + "=" * 78)
        print("ACTIVITY FEED (GET /activity):")
        print("=" * 78)
        act_res = client.get("/activity", headers=headers)
        activities = act_res.json()["activity"]

        print(f"{'TYPE':<24} | {'TASK_ID':<36} | {'LABEL / EXPLANATION'}")
        print("-" * 78)
        for act in activities:
            label = act.get("explanation") or act.get("label") or act.get("step_intent") or ""
            if len(label) > 40:
                label = label[:37] + "..."
            print(f"{act['type']:<24} | {act.get('task_id', ''):<36} | {label}")

        # -----------------------------------------------------------------
        # STEP 5: Print Final Step Table
        # -----------------------------------------------------------------
        print("\n" + "=" * 78)
        print(f"FINAL STEP TABLE FOR TASK {task_id}:")
        print("=" * 78)
        detail_after = client.get(f"/tasks/{task_id}", headers=headers).json()
        steps = detail_after["steps"]
        print(f"{'IDX':<4} | {'STATUS':<11} | {'TOOL':<12} | {'SUPERSEDED_BY':<14} | {'INTENT'}")
        print("-" * 78)
        for s in steps:
            sup = str(s.get("superseded_by") or "")
            if len(sup) > 12:
                sup = sup[:10] + ".."
            intent = s.get("intent", "")[:28]
            print(f"{s['idx']:<4} | {s['status']:<11} | {s['tool']:<12} | {sup:<14} | {intent}")

        # -----------------------------------------------------------------
        # STEP 6: Print llm_calls rows created during the heal
        # -----------------------------------------------------------------
        print("\n" + "=" * 78)
        print("LLM CALLS CREATED DURING DRIFT REPAIR (Audit Log):")
        print("=" * 78)
        with get_db(str(demo_db)) as conn:
            calls = conn.execute(
                """
                SELECT id, role, model, context_tags, input_chars, output_chars, latency_ms, created_at
                FROM llm_calls
                WHERE owner_id = ?
                ORDER BY created_at ASC
                """,
                (hashed_owner,),
            ).fetchall()

        print(f"{'ROLE':<8} | {'MODEL':<34} | {'IN/OUT CHARS':<12} | {'LATENCY':<8} | {'TAGS'}")
        print("-" * 78)
        for c in calls:
            tags = json.loads(c["context_tags"]) if c["context_tags"] else []
            chars = f"{c['input_chars']}/{c['output_chars']}"
            lat = f"{c['latency_ms']:.0f}ms"
            model_short = c["model"].replace("nvidia/", "")[:33]
            print(f"{c['role']:<8} | {model_short:<34} | {chars:<12} | {lat:<8} | {tags}")

        # -----------------------------------------------------------------
        # STEP 7: Schedule saved routine every minute and show two automatic runs
        # -----------------------------------------------------------------
        print("\n" + "=" * 78)
        print("STEP 7: Save Routine and Schedule Every 1 Minute (Automatic Watcher Runs)")
        print("=" * 78)

        # Save verified task as routine
        save_res = client.post(
            f"/tasks/{task_id}/save-as-skill",
            json={"name": "Health Routine"},
            headers=headers,
        )
        assert save_res.status_code == 200, f"Save skill failed: {save_res.text}"
        skill_id = save_res.json()["skill"]["id"]
        print(f"Saved skill ID: {skill_id}")

        # Schedule routine with 1 minute interval
        sched_res = client.put(
            f"/skills/{skill_id}/schedule",
            json={"minutes": 1},
            headers=headers,
        )
        assert sched_res.status_code == 200, f"Schedule failed: {sched_res.text}"
        sk_info = sched_res.json()["skill"]
        print(f"Scheduled skill: interval={sk_info['schedule_minutes']}m, next_run_at={sk_info['next_run_at']}")

        # Set next_run_at to now for run 1 so watcher executes it on current cycle
        with get_db(str(demo_db)) as conn:
            conn.execute(
                "UPDATE skills SET next_run_at = ? WHERE id = ?",
                (utc_now_iso(), skill_id),
            )

        print("\n>> Waiting for Watcher to detect and execute Run 1...")
        run1_deadline = time.time() + 45
        while time.time() < run1_deadline:
            sk = get_skill(skill_id, db_path=str(demo_db))
            if sk and sk.runs >= 1:
                break
            time.sleep(2.0)

        sk1 = get_skill(skill_id, db_path=str(demo_db))
        assert sk1.runs >= 1, "Scheduled Run 1 did not execute"
        print(f"  [Automatic Run 1 Completed!]")
        print(f"    * Total Runs: {sk1.runs}")
        print(f"    * Verified Runs: {sk1.verified_runs}")
        print(f"    * Last Run At: {sk1.last_run_at}")
        print(f"    * Last Status: {sk1.last_status}")
        print(f"    * Next Run Scheduled At: {sk1.next_run_at}")

        # Trigger Run 2 (due in 1 min, fast-forward next_run_at to now to simulate 1 minute elapsed)
        with get_db(str(demo_db)) as conn:
            conn.execute(
                "UPDATE skills SET next_run_at = ? WHERE id = ?",
                (utc_now_iso(), skill_id),
            )

        print("\n>> Waiting for Watcher to detect and execute Run 2...")
        run2_deadline = time.time() + 45
        while time.time() < run2_deadline:
            sk = get_skill(skill_id, db_path=str(demo_db))
            if sk and sk.runs >= 2:
                break
            time.sleep(2.0)

        sk2 = get_skill(skill_id, db_path=str(demo_db))
        assert sk2.runs >= 2, "Scheduled Run 2 did not execute"
        print(f"  [Automatic Run 2 Completed!]")
        print(f"    * Total Runs: {sk2.runs}")
        print(f"    * Verified Runs: {sk2.verified_runs}")
        print(f"    * Last Run At: {sk2.last_run_at}")
        print(f"    * Last Status: {sk2.last_status}")
        print(f"    * Consecutive Failures: {sk2.consecutive_failures}")

        # Print activity feed showing routine runs
        print("\n" + "=" * 78)
        print("UPDATED ACTIVITY FEED SHOWING ROUTINE RUNS:")
        print("=" * 78)
        act_res2 = client.get("/activity", headers=headers)
        activities2 = act_res2.json()["activity"]
        print(f"{'TYPE':<24} | {'TASK_ID':<36} | {'LABEL / EXPLANATION'}")
        print("-" * 78)
        for act in activities2:
            label = act.get("explanation") or act.get("label") or act.get("step_intent") or ""
            if len(label) > 40:
                label = label[:37] + "..."
            print(f"{act['type']:<24} | {act.get('task_id', ''):<36} | {label}")

    print("\n" + "=" * 78)
    print("PHASE 7 DEMO COMPLETED SUCCESSFULLY WITH ALL END-TO-END VERIFICATIONS!")
    print("=" * 78)


if __name__ == "__main__":
    run_phase7_demo()
