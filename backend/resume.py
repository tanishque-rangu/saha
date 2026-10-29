"""Saha Resume & Reconciliation Engine (Append-Only with FAST & PLANNER models).

On resume:
1. Re-runs deterministic reality probes for every completed step.
2. Any step whose reality diverged is marked `drifted` and stays `drifted` (never superseded).
3. FAST model (NVIDIA-Nemotron-3-Nano-30B) receives structured facts only and generates a 1-sentence explanation without guessing causes.
4. Only steps that depend on a drifted step (directly or transitively) transition to `superseded`.
5. Unaffected verified steps remain `verified`.
6. PLANNER model generates repair/replacement steps.
7. Repair steps are APPENDED to the ledger; drifted steps link to repairs via `resolved_by`; superseded steps link via `superseded_by`.
8. Execution resumes until all active steps are verified and all drifted steps are resolved.
"""

import json
import re
from typing import Any, Dict, List, Optional

from backend.executor import run_task
from backend.ledger import Ledger, Step, Task
from backend.llm import FAST_MODEL, call_llm
from backend.planner import replan_drift
from backend.verifiers import verify_step


def clean_fast_explanation(raw: str) -> str:
    """Strip any chain-of-thought tags, reasoning blocks, and prompt echoes."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", cleaned) if s.strip()]
    filtered = [
        s for s in sentences
        if not re.search(r"\b(make sure|exactly one sentence|do not guess|system prompt|instruction|prompt)\b", s, re.IGNORECASE)
    ]
    if filtered:
        for s in filtered:
            if any(k in s.lower() for k in ("verif", "probe", "exist", "miss", "error", "file", "now")):
                return s
        return filtered[0]
    return sentences[0] if sentences else cleaned


def _generate_fast_explanation_worker(
    event_id: str,
    intent: str,
    verified_time: str,
    probe_name: str,
    error_msg: str,
    owner_id: Optional[str] = None,
    db_path: Optional[str] = None,
) -> None:
    """Worker function executed in background thread to generate FAST explanation."""
    fast_prompt = (
        f"You are Saha's Fast Auditor AI. Given these structured facts:\n"
        f"- Step Intent: {intent}\n"
        f"- Last Verified At: {verified_time}\n"
        f"- Probe Name: {probe_name}\n"
        f"- Probe Failure Reason: {error_msg}\n\n"
        f"In exactly one sentence, state what was verified before and what the probe sees now. "
        f"Do NOT guess a cause."
    )
    try:
        safe_probe = re.sub(r"[^A-Za-z0-9._-]", "_", str(probe_name))[:64]
        raw_expl = call_llm(
            model=FAST_MODEL,
            messages=[{"role": "user", "content": fast_prompt}],
            role="fast",
            temperature=0.0,
            max_tokens=150,
            timeout=15.0,
            owner_id=owner_id,
            context_tags=["role:fast", f"probe:{safe_probe}"],
            db_path=db_path,
        ).strip()
        explanation = clean_fast_explanation(raw_expl)
        Ledger.update_drift_explanation(event_id, explanation, db_path=db_path)
    except Exception:
        fallback = f"Step was verified at {verified_time}, but probe '{probe_name}' currently reports: {error_msg}."
        Ledger.update_drift_explanation(event_id, fallback, db_path=db_path)


def reconcile_task_reality(task_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Audit all active verified/done steps against physical reality.
    
    Marks failing steps as `drifted` and records a drift_events row immediately.
    Spawns background thread for FAST explanation.
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
                probe_name = probe_spec.get("probe_type") or probe_result.get("probe") or f"{step.tool}_probe"
                verified_time = step.finished_at or step.started_at or "previously"

                # Mark step drifted in ledger immediately (stays drifted, never superseded)
                Ledger.mark_step_drifted(step.id, reason=error_msg, probe=probe_result, db_path=db_path)

                # Record immutable drift event immediately with explanation=None
                event_id = Ledger.record_drift_event(
                    task_id=task_id,
                    step_id=step.id,
                    tool=step.tool,
                    reason=error_msg,
                    explanation=None,
                    db_path=db_path,
                )

                # Spawn non-blocking background thread for FAST explanation
                import threading
                threading.Thread(
                    target=_generate_fast_explanation_worker,
                    args=(event_id, step.intent, verified_time, probe_name, error_msg, task.owner_id, db_path),
                    daemon=True,
                ).start()

                drifted_steps.append({
                    "event_id": event_id,
                    "step_id": step.id,
                    "idx": step.idx,
                    "seq": step.seq,
                    "intent": step.intent,
                    "tool": step.tool,
                    "reason": error_msg,
                    "explanation": None,
                })

    return {
        "task_id": task_id,
        "has_drift": len(drifted_steps) > 0,
        "drift_count": len(drifted_steps),
        "drifted_steps": drifted_steps,
    }


def resume_task(task_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Reconcile reality, calculate dependencies, append repair steps, and resume execution."""
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

    # Step 2: Determine dependent steps based on depends_on
    drifted_steps = [s for s in active_steps if s.status == "drifted"]
    drifted_indices = {s.idx for s in drifted_steps}

    # Transitive dependency resolution: find all steps that depend on drifted steps
    dependent_indices = set()
    changed = True
    while changed:
        changed = False
        for s in active_steps:
            if s.idx not in drifted_indices and s.idx not in dependent_indices:
                deps = set(s.to_dict().get("depends_on", []))
                if deps.intersection(drifted_indices.union(dependent_indices)):
                    dependent_indices.add(s.idx)
                    changed = True

    # Drifted steps STAY drifted. Only dependent steps are marked superseded.
    steps_to_supersede = [s for s in active_steps if s.idx in dependent_indices]

    # Step 3: Replan with PLANNER model
    new_step_plans = replan_drift(
        goal=task.goal,
        ledger_steps=[s.to_dict() for s in all_steps],
        drift_report=drift_report,
        owner_id=task.owner_id,
    )

    # Step 4: Append new repair steps to ledger (NEVER DELETE)
    new_steps_data = [
        {
            "intent": sp.intent,
            "tool": sp.tool,
            "args": sp.args,
            "probe": sp.probe,
            "depends_on": sp.depends_on,
        }
        for sp in new_step_plans
    ]
    added_steps = Ledger.add_steps(task_id, new_steps_data, db_path=db_path)
    new_repair_steps = added_steps[-len(new_steps_data):]

    # Step 5: Link resolved_by on drifted steps and superseded_by on superseded steps
    # Link drifted steps to repair steps
    for d in drifted_steps:
        d_args = json.loads(d.args_json) if d.args_json else {}
        d_path = d_args.get("path")
        # Match by path/tool if possible, otherwise first repair step
        matched_repair = None
        for r in new_repair_steps:
            r_args = json.loads(r.args_json) if r.args_json else {}
            if d_path and r_args.get("path") == d_path:
                matched_repair = r
                break
        if not matched_repair and new_repair_steps:
            matched_repair = new_repair_steps[0]

        if matched_repair:
            Ledger.mark_step_resolved(d.id, matched_repair.id, db_path=db_path)

    # Link superseded steps to replacement steps
    for s in steps_to_supersede:
        s_args = json.loads(s.args_json) if s.args_json else {}
        s_path = s_args.get("path")
        matched_rep = None
        for r in new_repair_steps:
            r_args = json.loads(r.args_json) if r.args_json else {}
            if s_path and r_args.get("path") == s_path:
                matched_rep = r
                break
        if not matched_rep and len(new_repair_steps) > 1:
            matched_rep = new_repair_steps[-1]
        elif not matched_rep and new_repair_steps:
            matched_rep = new_repair_steps[0]

        rep_id = matched_rep.id if matched_rep else None
        Ledger.supersede_steps([s.id], superseded_by_id=rep_id, db_path=db_path)

    # Step 6: Resume execution of pending steps
    final_task = run_task(task_id, db_path=db_path)
    final_summary = Ledger.get_task_summary(task_id, db_path=db_path)

    return {
        "task": final_task.to_dict(),
        "drift_detected": drift_report["has_drift"],
        "drift_report": drift_report,
        "replanned": True,
        "summary": final_summary,
    }
