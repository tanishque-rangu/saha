"""Saha CLI Client.

Supports:
  python -m cli.saha run "<goal>"
  python -m cli.saha status <id>
  python -m cli.saha resume <id>
  python -m cli.saha metrics
"""

import argparse
import json
import sys
from typing import Any, Dict, List

from backend.db import get_llm_metrics
from backend.executor import run_task
from backend.ledger import Ledger
from backend.planner import generate_plan
from backend.resume import resume_task

# UTF-8 stdout configuration for Windows terminals
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def print_step_table(steps: List[Dict[str, Any]], title: str = "Execution Timeline"):
    """Format and print a terminal table of ledger steps."""
    print("\n" + "=" * 90)
    print(f" {title} ")
    print("=" * 90)
    print(f"{'Seq':<5} {'Idx':<4} {'Status':<14} {'Tool':<12} {'Intent':<50}")
    print("-" * 90)

    status_badges = {
        "verified":   "[VERIFIED  ]",
        "running":    "[RUNNING   ]",
        "pending":    "[PENDING   ]",
        "drifted":    "[DRIFTED   ]",
        "superseded": "[SUPERSEDED]",
        "failed":     "[FAILED    ]",
        "done":       "[DONE      ]",
    }

    for s in steps:
        seq = s.get("seq", 0)
        idx = s.get("idx", 0)
        status = s.get("status", "unknown")
        badge = status_badges.get(status, f"[{status}]")
        tool = s.get("tool", "")[:11]
        intent = s.get("intent", "")[:48]
        print(f"{seq:<5} {idx:<4} {badge:<14} {tool:<12} {intent:<50}")

        if s.get("drift_reason"):
            print(f"       └─ Drift Reason: {s['drift_reason']}")
        if s.get("superseded_by"):
            print(f"       └─ Superseded By: Step ID {s['superseded_by'][:8]}...")
        if s.get("error"):
            print(f"       └─ Error: {s['error']}")

    print("=" * 90 + "\n")


def cmd_run(goal: str, db_path: str = None):
    print(f"\n[Saha] Initializing Task...")
    print(f"Goal: {goal}")

    task = Ledger.create_task(goal, db_path=db_path)
    print(f"Task ID: {task.id}")

    print(f"[Saha] Generating plan using NVIDIA Nemotron PLANNER...")
    step_plans = generate_plan(goal)
    print(f"[Saha] Plan generated: {len(step_plans)} steps")

    steps_data = [
        {
            "intent": sp.intent,
            "tool": sp.tool,
            "args": sp.args,
            "probe": sp.probe,
        }
        for sp in step_plans
    ]
    Ledger.add_steps(task.id, steps_data, db_path=db_path)

    summary = Ledger.get_task_summary(task.id, db_path=db_path)
    print_step_table(summary["steps"], title=f"Planned Steps (Task: {task.id[:8]}...)")

    print(f"[Saha] Executing steps with WORKER model and deterministic probes...")
    final_task = run_task(task.id, db_path=db_path)

    post_summary = Ledger.get_task_summary(task.id, db_path=db_path)
    print_step_table(post_summary["steps"], title=f"Execution Results (Status: {final_task.status.upper()})")

    metrics = get_llm_metrics(db_path=db_path)
    print("Telemetry Metrics (llm_calls):")
    for m in metrics["models"]:
        print(f"  • {m['model']} ({m['role']}): {m['call_count']} calls, avg {m['avg_latency_ms']} ms")

    return final_task


def cmd_status(task_id: str, db_path: str = None):
    summary = Ledger.get_task_summary(task_id, db_path=db_path)
    task = summary["task"]
    print(f"\nTask ID: {task['id']}")
    print(f"Goal:    {task['goal']}")
    print(f"Status:  {task['status'].upper()}")
    print(f"Drift:   {'YES (Action Required)' if summary['has_drift'] else 'None'}")

    if summary.get("drift_events"):
        print("\nDrift Events Log:")
        for de in summary["drift_events"]:
            print(f"  • [{de['detected_at']}] Step {de['step_id'][:8]} ({de['tool']}): {de['reason']}")
            if de.get("explanation"):
                print(f"    Auditor explanation: {de['explanation']}")

    print_step_table(summary["steps"], title=f"Full Audit Ledger for {task_id[:8]}...")


def cmd_resume(task_id: str, db_path: str = None):
    print(f"\n[Saha] Reconciling Task {task_id} with reality...")
    result = resume_task(task_id, db_path=db_path)

    drift_report = result["drift_report"]
    if drift_report["has_drift"]:
        print(f"\n[DRIFT DETECTED] Reality diverged on {drift_report['drift_count']} step(s)!")
        for ds in drift_report["drifted_steps"]:
            print(f"  • Step {ds['idx']} (Seq {ds['seq']}, {ds['tool']}): {ds['reason']}")
            if ds.get("explanation"):
                print(f"    Fast Auditor: {ds['explanation']}")
    else:
        print("[Saha] Reality check: No drift detected.")

    if result.get("replanned"):
        print("[Saha] PLANNER regenerated repair steps and appended them to ledger.")

    summary = Ledger.get_task_summary(task_id, db_path=db_path)
    print_step_table(summary["steps"], title=f"Reconciled Ledger (Status: {result['task']['status'].upper()})")


def cmd_metrics(db_path: str = None):
    metrics = get_llm_metrics(db_path=db_path)
    print("\n" + "=" * 60)
    print("Saha LLM Performance Metrics (llm_calls)")
    print("=" * 60)
    print(f"Total calls: {metrics['total_calls']}")
    for m in metrics["models"]:
        print(f"  • Model: {m['model']}")
        print(f"    Role:        {m['role']}")
        print(f"    Calls:       {m['call_count']}")
        print(f"    Avg Latency: {m['avg_latency_ms']} ms")
        print(f"    Tokens:      {m['total_tokens']} (prompt: {m['total_prompt_tokens']}, comp: {m['total_completion_tokens']})")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(prog="saha", description="Saha: Persistent Personal AI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a new goal end-to-end")
    run_parser.add_argument("goal", type=str, help="The goal to plan and execute")

    status_parser = subparsers.add_parser("status", help="Get status of a task")
    status_parser.add_argument("task_id", type=str, help="Task ID")

    resume_parser = subparsers.add_parser("resume", help="Reconcile and resume an interrupted or drifted task")
    resume_parser.add_argument("task_id", type=str, help="Task ID")

    subparsers.add_parser("metrics", help="Show LLM telemetry and latency metrics")

    args = parser.parse_args()

    if args.command == "run":
        cmd_run(args.goal)
    elif args.command == "status":
        cmd_status(args.task_id)
    elif args.command == "resume":
        cmd_resume(args.task_id)
    elif args.command == "metrics":
        cmd_metrics()


if __name__ == "__main__":
    main()
