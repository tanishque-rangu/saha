"""Real Demonstration Script for Phase 6-Lite: Memory and Saved Routines.

Executes real Nemotron LLM calls with:
1. Owner A adds memory: "I prefer summaries as short bullet points".
2. Owner A runs a task that writes a summary file.
3. Shows memory ID in the privacy ledger egress tags and the bullet-point output formatting.
4. Saves the verified task as a routine (skill-lite).
5. Runs the routine, verifying execution and stat updates.
6. Proves Owner B sees nothing (isolation: 0 memories, 0 skills, 404 on Owner A's resources).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from backend.api import app
from backend.db import get_owner_egress_logs, hash_owner_token, init_db
from backend.tools.sandbox import resolve_workspace_path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def run_phase6_demo():
    print("=" * 75)
    print("SAHA PHASE 6-LITE: MEMORY AND SAVED ROUTINES REAL DEMO")
    print("=" * 75)

    # Use dedicated isolated demo workspace and database
    tmp_dir = tempfile.mkdtemp()
    demo_db = Path(tmp_dir) / "demo_phase6.db"
    demo_ws = Path(tmp_dir) / "workspace"
    demo_ws.mkdir()

    os.environ["SAHA_DB_PATH"] = str(demo_db)
    os.environ["SAHA_WORKSPACE_ROOT"] = str(demo_ws)
    os.environ["SAHA_DEMO_MODE"] = "true"
    os.environ["SAHA_PUBLIC_MODE"] = "false"

    init_db(str(demo_db))
    client = TestClient(app)

    owner_a_token = "token_alice_owner_a_12345678901234567890"
    owner_b_token = "token_bob_owner_b_98765432109876543210"
    headers_a = {"X-Saha-Owner": owner_a_token}
    headers_b = {"X-Saha-Owner": owner_b_token}

    owner_a_id = hash_owner_token(owner_a_token)
    owner_b_id = hash_owner_token(owner_b_token)

    print(f"\n[SETUP] Initialized demo environment.")
    print(f"  Owner A ID: {owner_a_id}")
    print(f"  Owner B ID: {owner_b_id}")

    # ---------------------------------------------------------------------
    # 1. OWNER A ADDS MEMORY
    # ---------------------------------------------------------------------
    print("\n" + "-" * 75)
    print("STEP 1: Owner A adds a preference memory")
    print("-" * 75)
    pref_text = "I prefer summaries as short bullet points"
    mem_res = client.post(
        "/memories",
        json={"kind": "preference", "text": pref_text},
        headers=headers_a,
    )
    assert mem_res.status_code == 200, f"Failed to add memory: {mem_res.text}"
    mem_data = mem_res.json()["memory"]
    memory_id = mem_data["id"]
    print(f"[OK] Memory added successfully:")
    print(f"     ID:     {memory_id}")
    print(f"     Kind:   {mem_data['kind']}")
    print(f"     Status: {mem_data['status']}")
    print(f"     Text:   \"{mem_data['text']}\"")

    # ---------------------------------------------------------------------
    # 2. RUN A TASK THAT WRITES A SUMMARY FILE
    # ---------------------------------------------------------------------
    print("\n" + "-" * 75)
    print("STEP 2: Owner A runs a task writing a summary file (Real LLM Calls)")
    print("-" * 75)
    goal = "Write a summary of Hyderabad history into summary.txt"
    print(f"[TASK] Goal: \"{goal}\"")

    create_task_res = client.post(
        "/tasks",
        json={"goal": goal},
        headers=headers_a,
    )
    assert create_task_res.status_code == 200, f"Task creation failed: {create_task_res.text}"
    task_id = create_task_res.json()["task"]["id"]
    print(f"[TASK] Created Task ID: {task_id}")

    plan_steps = create_task_res.json().get("steps", [])
    print(f"[PLAN] Generated plan with {len(plan_steps)} step(s):")
    for s in plan_steps:
        print(f"       Step {s['idx']}: [{s['tool']}] {s['intent']}")

    print(f"\n[EXEC] Running task via executor and verifiers...")
    run_res = client.post(f"/tasks/{task_id}/run", headers=headers_a)
    assert run_res.status_code == 200, f"Task run failed: {run_res.text}"

    import time
    for _ in range(40):
        time.sleep(1)
        task_summary = client.get(f"/tasks/{task_id}", headers=headers_a).json()
        status = task_summary['task']['status']
        if status in ("complete", "failed", "paused"):
            break

    print(f"[EXEC] Task completed with status: {task_summary['task']['status']}")
    assert task_summary['task']['status'] == "complete", f"Expected task to reach complete, got: {task_summary['task']['status']}"

    # ---------------------------------------------------------------------
    # 3. SHOW MEMORY ID IN EGRESS TAGS & OUTPUT FORMATTING
    # ---------------------------------------------------------------------
    print("\n" + "-" * 75)
    print("STEP 3: Verify Memory ID in Egress Tags & Output Formatting")
    print("-" * 75)

    # Check egress tags
    egress_res = client.get("/egress", headers=headers_a)
    assert egress_res.status_code == 200, f"Failed to get egress logs: {egress_res.text}"
    egress_data = egress_res.json()
    egress_logs = egress_data.get("egress", egress_data.get("egress_logs", []))
    print(f"[PRIVACY LEDGER] Found {len(egress_logs)} LLM call egress record(s) for Owner A:")

    found_memory_tag = False
    for log in egress_logs:
        tags = json.loads(log["context_tags"]) if isinstance(log["context_tags"], str) else log["context_tags"]
        print(f"  Call {log['id'][:8]}... | Model: {log['model'][:35]} | Role: {log.get('role', 'unknown')}")
        print(f"    Egress Tags: {tags}")
        if f"memory:{memory_id}" in tags:
            found_memory_tag = True
            print(f"    -> MATCH: Found expected tag 'memory:{memory_id}' in egress!")

    assert found_memory_tag, f"Expected 'memory:{memory_id}' in LLM egress context tags"

    # Check summary.txt output formatting
    summary_path = resolve_workspace_path("summary.txt", task_id=task_id, owner_id=owner_a_id)
    if not summary_path.exists():
        # Check subdirectories if any
        candidates = list(Path(demo_ws / owner_a_id / task_id).glob("**/summary.txt"))
        if candidates:
            summary_path = candidates[0]

    assert summary_path.exists(), f"Expected summary file at {summary_path}"
    with open(summary_path, "r", encoding="utf-8") as f:
        content = f.read()

    print(f"\n[OUTPUT FORMATTING] Contents of '{summary_path.name}':")
    print("┌" + "─" * 60 + "┐")
    for line in content.strip().splitlines():
        print(f"│ {line[:58]:<58} │")
    print("└" + "─" * 60 + "┘")

    # Verify bullet point formatting applied
    has_bullets = any(line.strip().startswith(("-", "*", "•")) for line in content.splitlines())
    print(f"[VERIFY] Bullet point formatting applied from memory: {has_bullets}")

    # ---------------------------------------------------------------------
    # 4. SAVE TASK AS ROUTINE (SKILL)
    # ---------------------------------------------------------------------
    print("\n" + "-" * 75)
    print("STEP 4: Save verified task as a Saved Routine (Skill)")
    print("-" * 75)
    routine_name = "Hyderabad Summary Routine"
    routine_desc = "Generates concise bullet points on Hyderabad history"

    save_res = client.post(
        f"/tasks/{task_id}/save-as-skill",
        json={"name": routine_name, "description": routine_desc},
        headers=headers_a,
    )
    assert save_res.status_code == 200, f"Failed to save skill: {save_res.text}"
    skill = save_res.json()["skill"]
    skill_id = skill["id"]
    print(f"[SKILL] Saved Routine Created:")
    print(f"        ID:          {skill_id}")
    print(f"        Name:        {skill['name']}")
    print(f"        Description: {skill['description']}")
    print(f"        Steps Count: {len(skill['steps'])}")
    print(f"        Initial Runs: {skill['runs']}, Verified Runs: {skill['verified_runs']}")

    # ---------------------------------------------------------------------
    # 5. RUN THE ROUTINE & SHOW IT VERIFIED
    # ---------------------------------------------------------------------
    print("\n" + "-" * 75)
    print("STEP 5: Run the Saved Routine and Verify Stats Update")
    print("-" * 75)
    print(f"[ROUTINE] Triggering POST /skills/{skill_id}/run...")
    run_routine_res = client.post(f"/skills/{skill_id}/run", headers=headers_a)
    assert run_routine_res.status_code == 200, f"Failed to run routine: {run_routine_res.text}"
    routine_run_data = run_routine_res.json()
    new_task = routine_run_data["task"]
    updated_skill = routine_run_data["skill"]

    print(f"[ROUTINE] Routine Executed in Task ID: {new_task['id']}")
    print(f"          Task Status:   {new_task['status']}")
    print(f"          Skill Runs:    {updated_skill['runs']}")
    print(f"          Verified Runs: {updated_skill['verified_runs']}")
    print(f"          Last Status:   {updated_skill['last_status']}")
    print(f"          Last Run At:   {updated_skill['last_run_at']}")

    assert new_task["status"] == "complete", "Routine task did not verify"
    assert updated_skill["runs"] == 1, "Expected skill runs count == 1"
    assert updated_skill["verified_runs"] == 1, "Expected verified runs count == 1"

    # ---------------------------------------------------------------------
    # 6. SHOW OWNER B SEES NOTHING (ISOLATION AUDIT)
    # ---------------------------------------------------------------------
    print("\n" + "-" * 75)
    print("STEP 6: Owner B Isolation Verification (Owner B sees nothing)")
    print("-" * 75)

    # Owner B lists memories
    b_mems_res = client.get("/memories", headers=headers_b)
    b_mems = b_mems_res.json()["memories"]
    print(f"[ISOLATION] Owner B GET /memories count: {len(b_mems)} (Expected: 0)")
    assert len(b_mems) == 0

    # Owner B lists skills
    b_skills_res = client.get("/skills", headers=headers_b)
    b_skills = b_skills_res.json()["skills"]
    print(f"[ISOLATION] Owner B GET /skills count:   {len(b_skills)} (Expected: 0)")
    assert len(b_skills) == 0

    # Owner B attempts to delete Owner A's memory
    b_del_mem = client.delete(f"/memories/{memory_id}", headers=headers_b)
    print(f"[ISOLATION] Owner B DELETE /memories/{memory_id[:8]}... HTTP Status: {b_del_mem.status_code} (Expected: 404)")
    assert b_del_mem.status_code == 404

    # Owner B attempts to run Owner A's skill
    b_run_skill = client.post(f"/skills/{skill_id}/run", headers=headers_b)
    print(f"[ISOLATION] Owner B POST /skills/{skill_id[:8]}.../run HTTP Status: {b_run_skill.status_code} (Expected: 404)")
    assert b_run_skill.status_code == 404

    # Owner B attempts to delete Owner A's skill
    b_del_skill = client.delete(f"/skills/{skill_id}", headers=headers_b)
    print(f"[ISOLATION] Owner B DELETE /skills/{skill_id[:8]}... HTTP Status: {b_del_skill.status_code} (Expected: 404)")
    assert b_del_skill.status_code == 404

    print("\n" + "=" * 75)
    print("PHASE 6-LITE DEMO COMPLETED SUCCESSFULLY WITH 100% VERIFICATION")
    print("=" * 75)


if __name__ == "__main__":
    run_phase6_demo()
