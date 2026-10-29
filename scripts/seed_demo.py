"""Seed script to populate a finished, fully-verified demonstration task in Saha.

Runs real PLANNER and WORKER model calls to establish initial state.
Execute once: python scripts/seed_demo.py
"""

import sys
from pathlib import Path

# UTF-8 stdout configuration for Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.db import init_db
from backend.executor import run_task
from backend.ledger import Ledger
from backend.planner import generate_plan
from cli.saha import print_step_table


def seed_demo(db_path: str = None):
    print("=" * 80)
    print(" SEEDING COMPLETED DEMO TASK FOR SAHA")
    print("=" * 80)

    init_db(db_path)

    goal = (
        "Create two small data files with different facts about Hyderabad, "
        "combine them into a summary file, and check that https://example.com is reachable."
    )
    print(f"\n[1] Goal: \"{goal}\"")

    task = Ledger.create_task(goal, db_path=db_path)
    print(f"    Task created: ID = {task.id}")

    print("\n[2] Invoking PLANNER model to generate plan with content briefs...")
    step_plans = generate_plan(goal)
    print(f"    Plan generated: {len(step_plans)} steps")

    steps_data = []
    for sp in step_plans:
        d_on = list(getattr(sp, "depends_on", []))
        if not d_on and ("combine" in sp.intent.lower() or "summary" in sp.intent.lower()):
            d_on = [1, 2]
        steps_data.append({
            "intent": sp.intent,
            "tool": sp.tool,
            "args": sp.args,
            "probe": sp.probe,
            "depends_on": d_on,
        })

    steps = Ledger.add_steps(task.id, steps_data, db_path=db_path)
    print(f"    Steps added to ledger: {len(steps)}")

    print("\n[3] Executing steps with WORKER model and deterministic verification probes...")
    completed_task = run_task(task.id, db_path=db_path)
    summary = Ledger.get_task_summary(task.id, db_path=db_path)

    print_step_table(summary["steps"], title=f"Seeded Task Summary (Status: {completed_task.status.upper()})")
    print(f"✔ Successfully seeded completed task {task.id} into database!\n")
    return task.id


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else None
    seed_demo(db)
