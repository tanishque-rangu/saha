"""Phase 1 & 2 Verification Script for Saha.

1. Tests SQLite schema initialization and Ledger state machine in a temporary directory.
2. Calls each of the three NVIDIA Nemotron models through backend/llm.py:
   - PLANNER: nvidia/Nemotron-3-Ultra-550b-a55b
   - WORKER:  nvidia/nemotron-3-super-120b-a12b
   - FAST:    nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B
3. Prints outputs and latency metrics from the llm_calls table.
"""

import os
import sys
import tempfile
from pathlib import Path

# Set up utf-8 encoding for Windows console output
sys.stdout.reconfigure(encoding="utf-8")

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.db import get_llm_metrics, init_db
from backend.ledger import Ledger
from backend.llm import (
    FAST_MODEL,
    PLANNER_MODEL,
    WORKER_MODEL,
    call_llm,
    load_api_key,
)


def test_ledger_in_temp():
    print("=" * 60)
    print("Testing Ledger and SQLite State Machine (Isolated Temp Dir)...")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp_dir:
        test_db_path = Path(tmp_dir) / "test_saha.db"
        init_db(str(test_db_path))
        print(f"✔ Database initialized in temporary dir: {test_db_path}")

        # Create Task
        task = Ledger.create_task("Verify Saha ledger state transitions", db_path=str(test_db_path))
        print(f"✔ Task created: id={task.id}, status={task.status}")

        # Add Steps
        steps_data = [
            {"idx": 1, "intent": "Initialize environment", "tool": "run_shell", "args": {"cmd": "echo ready"}},
            {"idx": 2, "intent": "Write heartbeat file", "tool": "write_file", "args": {"path": "heartbeat.txt", "content": "alive"}},
        ]
        steps = Ledger.add_steps(task.id, steps_data, db_path=str(test_db_path))
        print(f"✔ Steps added: count={len(steps)}")

        # State transition: pending -> running (logged BEFORE execution)
        step1 = steps[0]
        running_step = Ledger.mark_step_running(step1.id, db_path=str(test_db_path))
        print(f"✔ Step {running_step.idx} marked 'running' before execution")

        # State transition: running -> done
        done_step = Ledger.mark_step_done(step1.id, {"stdout": "ready", "exit_code": 0}, db_path=str(test_db_path))
        print(f"✔ Step {done_step.idx} marked 'done' after execution")

        # State transition: done -> verified
        verified_step = Ledger.mark_step_verified(step1.id, {"probe": "exit_code_zero", "passed": True}, db_path=str(test_db_path))
        print(f"✔ Step {verified_step.idx} marked 'verified' by probe")

        # Drift simulation on step 2
        step2 = steps[1]
        Ledger.mark_step_running(step2.id, db_path=str(test_db_path))
        Ledger.mark_step_done(step2.id, {"path": "heartbeat.txt"}, db_path=str(test_db_path))
        drifted_step = Ledger.mark_step_drifted(step2.id, "file missing in reality", {"error": "file missing in reality", "passed": False}, db_path=str(test_db_path))
        print(f"✔ Step {drifted_step.idx} marked 'drifted' upon reality check mismatch")

        # Summary check
        summary = Ledger.get_task_summary(task.id, db_path=str(test_db_path))
        print(f"✔ Task summary counts: {summary['counts']}")
        assert summary["counts"]["verified"] == 1
        assert summary["counts"]["drifted"] == 1
        print("✔ Isolated ledger test passed successfully!\n")


def test_models():
    print("=" * 60)
    print("Testing Nebius Token Factory & NVIDIA Nemotron Models...")
    print("=" * 60)

    try:
        key = load_api_key()
        print(f"✔ NEBIUS_API_KEY detected (length: {len(key)})")
    except Exception as e:
        print(f"✖ Failed to load NEBIUS_API_KEY: {e}")
        return

    models = [
        ("PLANNER", PLANNER_MODEL),
        ("WORKER", WORKER_MODEL),
        ("FAST", FAST_MODEL),
    ]

    results = {}
    for role, model_name in models:
        print(f"\n--- Testing {role}: {model_name} ---")
        prompt = (
            "You are Saha, a persistent AI assistant. Without conversational filler "
            "or overthinking, respond with exactly: 'Saha online, state ledger active.'"
        )
        messages = [{"role": "user", "content": prompt}]
        try:
            content = call_llm(
                model=model_name,
                messages=messages,
                role=role.lower(),
                temperature=0.0,
            )
            print(f"✔ Status: SUCCESS")
            print(f"  Response: {content.strip()}")
            results[role] = {"status": "SUCCESS", "model": model_name, "response": content.strip()}
        except Exception as e:
            print(f"✖ Status: FAILED for model {model_name}")
            print(f"  Error: {e}")
            results[role] = {"status": "FAILED", "model": model_name, "error": str(e)}

    print("\n" + "=" * 60)
    print("Phase 1 & 2 Model Verification Summary:")
    print("=" * 60)
    for role, res in results.items():
        status_icon = "✔" if res["status"] == "SUCCESS" else "✖"
        print(f"{status_icon} {role} ({res['model']}): {res['status']}")
        if res["status"] == "SUCCESS":
            print(f"   Sample: \"{res['response']}\"")
        else:
            print(f"   Error: {res['error']}")

    # Display telemetry from llm_calls
    metrics = get_llm_metrics()
    print("\nLLM Calls Telemetry (Recorded in SQLite):")
    for m in metrics["models"]:
        print(f"  - {m['model']} ({m['role']}): {m['call_count']} calls, avg latency: {m['avg_latency_ms']} ms")


if __name__ == "__main__":
    test_ledger_in_temp()
    test_models()
