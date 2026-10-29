"""Saha Resume & Reconciliation Engine (Append-Only with FAST & PLANNER models).

On resume:
1. Re-runs deterministic reality probes for every completed step.
2. Any step whose reality diverged is marked `drifted`.
3. FAST model (NVIDIA-Nemotron-3-Nano-30B) generates a plain-English explanation for each drift_events row.
4. PLANNER model generates repair steps for drifted and dependent remaining work.
5. Replaced steps transition to `superseded` (with `superseded_by` pointer).
6. Repair steps are APPENDED to the ledger (NEVER DELETED).
7. Execution resumes until all active steps in the current plan are verified.
"""

import json
from typing import Any, Dict, List, Optional

from backend.executor import run_task
from backend.ledger import Ledger, Step, Task
from backend.llm import FAST_MODEL, call_llm
from backend.planner import replan_drift
from backend.verifiers import verify_step


def reconcile_task_reality(task_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Audit all active verified/done steps against physical reality.
    
    Marks failing steps as `drifted` and records a drift_events row with FAST model explanation.
    """
    task = Ledger.get_task(task_id, db_path=db_path)
    if not task:
        raise ValueError(f"Task {task_id} not found")

    active_steps = Ledger.get_steps(task_id, include_superseded=False, db_path=db_path)
    drifted_steps: List[Dict[str, Any]] = []

    for step in active_steps:
        if step.status in ("verified", "done"):
            try:
                args = json.loads(step.args_json) if step.args_json else {}
            except Exception:
                args = {}
            try:
                result = json.loads(step.result_json) if step.result_json else {}
            except Exception:
                result = {}
            try:
                probe_spec = json.loads(step.probe_json) if step.probe_json else {}
            except Exception:
                probe_spec = {}

            # Deterministic reality probe (100% pure code)
            probe_result = verify_step(
                tool=step.tool,
                args=args,
                result=result,
                probe_spec=probe_spec,
                task_id=task_id,
            )

            if not probe_result.get("passed"):
                error_msg = probe_result.get("error", "Probe assertion failed")

                # FAST model writes the plain-English explanation for the drift event
                fast_prompt = (
                    f"You are Saha's Fast Auditor AI. In exactly one concise sentence, explain why this step drifted:\n"
                    f"Step intent: {step.intent}\nTool: {step.tool}\nObserved Reality Error: {error_msg}"
                )
                try:
                    explanation = call_llm(
                        model=FAST_MODEL,
                        messages=[{"role": "user", "content": fast_prompt}],
                        role="fast",
                        temperature=0.0,
                        max_tokens=256,
                        db_path=db_path,
                    ).strip()
                except Exception as fast_err:
                    explanation = f"Reality mismatch detected: {error_msg} (Auditor: {fast_err})"

                # Mark step drifted in ledger
                Ledger.mark_step_drifted(step.id, reason=error_msg, probe=probe_result, db_path=db_path)

                # Record immutable drift event
                event_id = Ledger.record_drift_event(
                    task_id=task_id,
                    step_id=step.id,
                    tool=step.tool,
                    reason=error_msg,
                    explanation=explanation,
                    db_path=db_path,
                )

                drifted_steps.append({
                    "event_id": event_id,
                    "step_id": step.id,
                    "idx": step.idx,
                    "seq": step.seq,
                    "intent": step.intent,
                    "tool": step.tool,
                    "reason": error_msg,
                    "explanation": explanation,
                })

    return {
        "task_id": task_id,
        "has_drift": len(drifted_steps) > 0,
        "drift_count": len(drifted_steps),
        "drifted_steps": drifted_steps,
    }


def resume_task(task_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Reconcile reality, append repair steps, supersede replaced steps, and resume execution."""
    task = Ledger.get_task(task_id, db_path=db_path)
    if not task:
        raise ValueError(f"Task {task_id} not found")

    # Step 1: Reconcile against physical reality
    drift_report = reconcile_task_reality(task_id, db_path=db_path)

    all_steps = Ledger.get_steps(task_id, include_superseded=True, db_path=db_path)
    active_steps = [s for s in all_steps if s.status != "superseded"]
    pending_or_drifted = [s for s in active_steps if s.status in ("drifted", "pending", "failed")]

    if not pending_or_drifted and not drift_report["has_drift"]:
        Ledger.update_task_status(task_id, "complete", db_path=db_path)
        return {
            "task": Ledger.get_task(task_id, db_path=db_path).to_dict(),
            "drift_detected": False,
            "drift_report": drift_report,
            "replanned": False,
            "message": "Task is already fully verified against reality.",
        }

    # Step 2: Determine steps to supersede (drifted steps and downstream steps)
    first_drifted_seq = min(s.seq for s in pending_or_drifted)
    steps_to_supersede = [s.id for s in active_steps if s.seq >= first_drifted_seq]

    # Step 3: Replan with PLANNER model
    new_step_plans = replan_drift(
        goal=task.goal,
        ledger_steps=[s.to_dict() for s in all_steps],
        drift_report=drift_report,
    )

    # Step 4: Append new repair steps to ledger (NEVER DELETE)
    new_steps_data = [
        {
            "intent": sp.intent,
            "tool": sp.tool,
            "args": sp.args,
            "probe": sp.probe,
        }
        for sp in new_step_plans
    ]
    added_steps = Ledger.add_steps(task_id, new_steps_data, db_path=db_path)
    first_new_step_id = added_steps[-len(new_steps_data)].id if added_steps else None

    # Step 5: Mark replaced steps as superseded (with pointer to the new repair step)
    Ledger.supersede_steps(steps_to_supersede, superseded_by_id=first_new_step_id, db_path=db_path)

    # Step 6: Resume execution of the updated active plan
    final_task = run_task(task_id, db_path=db_path)
    final_summary = Ledger.get_task_summary(task_id, db_path=db_path)

    return {
        "task": final_task.to_dict(),
        "drift_detected": drift_report["has_drift"],
        "drift_report": drift_report,
        "replanned": True,
        "summary": final_summary,
    }
