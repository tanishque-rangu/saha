"""Saha Always-On Watcher, Auto-Heal & Scheduled Routines Engine (Phase 7-Lite).

Enforces:
1. Continuous background auditing thread running every SAHA_WATCH_INTERVAL seconds (min 15s).
2. Deterministic reality probe re-runs for complete/paused tasks updated in last 7 days.
3. Drift detection, step drifting, task pausing, and non-blocking FAST explanation.
4. Auto-heal: budget enforcement (max 3/hr, 10/day per owner), policy check, LLM cap check.
5. In-flight task isolation (prevents double work on the same task).
6. Scheduled routines dispatching via run_skill, consecutive failure tracking (auto-disable at 3).
7. Aggregated owner activity feed (identifiers & short labels only, never secrets or file content).
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.db import check_daily_llm_limit, get_db, init_db, utc_now_iso
from backend.ledger import Ledger
from backend.policy import get_owner_policy
from backend.verifiers import verify_step

logger = logging.getLogger("saha.watcher")

DEFAULT_WATCH_INTERVAL = 60
MIN_WATCH_INTERVAL = 15
MAX_TASKS_PER_CYCLE = 20
HTTP_PROBE_INTERVAL_SECONDS = 600  # 10 minutes
MAX_AUTO_HEALS_PER_HOUR = 3
MAX_AUTO_HEALS_PER_DAY = 10


def get_watch_interval() -> int:
    """Return configured watch interval with minimum enforcement."""
    try:
        val = int(os.getenv("SAHA_WATCH_INTERVAL", str(DEFAULT_WATCH_INTERVAL)))
        return max(MIN_WATCH_INTERVAL, val)
    except ValueError:
        return DEFAULT_WATCH_INTERVAL


def is_watcher_enabled() -> bool:
    """Return whether the watcher loop is enabled (default true unless 'false')."""
    val = os.getenv("SAHA_WATCHER_ENABLED", "true").strip().lower()
    return val not in ("false", "0", "no")


def count_owner_auto_heals(owner_id: str, db_path: Optional[str] = None) -> Tuple[int, int]:
    """Count auto-heals performed for an owner in the last 1 hour and 24 hours."""
    init_db(db_path)
    now_dt = datetime.now(timezone.utc)
    one_hour_ago = (now_dt - timedelta(hours=1)).isoformat()
    one_day_ago = (now_dt - timedelta(days=1)).isoformat()

    with get_db(db_path) as conn:
        r_hr = conn.execute(
            """
            SELECT COUNT(*) as c FROM drift_events
            WHERE owner_id = ? AND auto_healed = 1 AND detected_at >= ?
            """,
            (owner_id, one_hour_ago),
        ).fetchone()
        count_hr = r_hr["c"] if r_hr else 0

        r_day = conn.execute(
            """
            SELECT COUNT(*) as c FROM drift_events
            WHERE owner_id = ? AND auto_healed = 1 AND detected_at >= ?
            """,
            (owner_id, one_day_ago),
        ).fetchone()
        count_day = r_day["c"] if r_day else 0

    return count_hr, count_day


class Watcher:
    """Always-on background auditor thread and scheduled routine dispatcher."""

    def __init__(
        self,
        interval: Optional[int] = None,
        enabled: Optional[bool] = None,
        db_path: Optional[str] = None,
    ):
        self.db_path = db_path
        self.enabled = is_watcher_enabled() if enabled is None else enabled
        self.interval = get_watch_interval() if interval is None else max(MIN_WATCH_INTERVAL, interval)
        self.last_cycle_at: Optional[str] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Concurrency & deduplication guards
        self._in_flight_tasks: Set[str] = set()
        self._in_flight_lock = threading.Lock()
        self._auto_healing_tasks: Set[str] = set()
        self._last_http_probes: Dict[str, datetime] = {}
        self._running_routine_owners: Set[str] = set()

    def start(self) -> None:
        """Start the background watcher thread if enabled."""
        if not self.enabled:
            logger.info("Watcher is disabled (SAHA_WATCHER_ENABLED=false)")
            return

        with self._in_flight_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                daemon=True,
                name="SahaWatcherLoop",
            )
            self._thread.start()
            logger.info("Saha Watcher started (interval=%ds)", self.interval)

    def stop(self, timeout: float = 5.0) -> None:
        """Clean shutdown of background watcher thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
            logger.info("Saha Watcher stopped cleanly")

    def _run_loop(self) -> None:
        """Main loop: sleeps until next cycle or shutdown signal."""
        while not self._stop_event.is_set():
            cycle_start = time.monotonic()
            try:
                self.run_cycle()
            except Exception as e:
                # Watcher must NEVER crash the server: log sanitized message and continue
                logger.error("Watcher cycle error: %s", str(e), exc_info=False)

            # Sleep in 0.5s increments to support responsive clean shutdown
            sleep_needed = max(0.1, self.interval - (time.monotonic() - cycle_start))
            steps = int(sleep_needed / 0.5)
            for _ in range(steps):
                if self._stop_event.is_set():
                    break
                time.sleep(0.5)
            rem = sleep_needed - (steps * 0.5)
            if rem > 0 and not self._stop_event.is_set():
                time.sleep(rem)

    def run_cycle(self) -> Dict[str, Any]:
        """Execute one complete audit and routine dispatching cycle."""
        self.last_cycle_at = utc_now_iso()
        drift_results = self._audit_tasks_for_drift()
        routine_results = self._dispatch_scheduled_routines()
        return {
            "cycle_at": self.last_cycle_at,
            "drift_audit": drift_results,
            "routines_dispatched": routine_results,
        }

    def _audit_tasks_for_drift(self) -> List[Dict[str, Any]]:
        """Audit candidate tasks for physical drift and trigger auto-heal."""
        init_db(self.db_path)
        cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        # Candidates: tasks in 'complete' or 'paused' updated within the last 7 days
        with get_db(self.db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, owner_id, status, updated_at
                FROM tasks
                WHERE status IN ('complete', 'paused') AND updated_at >= ?
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (cutoff_7d, MAX_TASKS_PER_CYCLE),
            ).fetchall()
            candidates = [(r["id"], r["owner_id"]) for r in rows]

        audit_log = []
        for task_id, owner_id in candidates:
            # Check in-flight guard
            with self._in_flight_lock:
                if task_id in self._in_flight_tasks:
                    continue
                self._in_flight_tasks.add(task_id)

            try:
                task_drift = self._check_single_task_drift(task_id, owner_id)
                if task_drift:
                    audit_log.append(task_drift)
            except Exception as e:
                logger.error("Error auditing task %s for drift: %s", task_id, str(e), exc_info=False)
            finally:
                # If task was not handed off to auto-heal, remove in-flight lock
                with self._in_flight_lock:
                    if task_id not in self._auto_healing_tasks:
                        self._in_flight_tasks.discard(task_id)

        return audit_log

    def _check_single_task_drift(self, task_id: str, owner_id: str) -> Optional[Dict[str, Any]]:
        """Re-run deterministic probes for active steps of a task."""
        task = Ledger.get_task(task_id, db_path=self.db_path)
        if not task or task.status == "running":
            return None

        active_steps = Ledger.get_steps(task_id, include_superseded=False, db_path=self.db_path)
        drifted_events = []

        now_dt = datetime.now(timezone.utc)

        for step in active_steps:
            if step.status not in ("verified", "done"):
                continue

            # HTTP probes at most once per 10 minutes per step
            if step.tool in ("http_get", "http_request"):
                last_p = self._last_http_probes.get(step.id)
                if last_p and (now_dt - last_p).total_seconds() < HTTP_PROBE_INTERVAL_SECONDS:
                    continue
                self._last_http_probes[step.id] = now_dt

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

            # Pure deterministic code verifier (NO LLM)
            probe_result = verify_step(
                tool=step.tool,
                args=args,
                result=result,
                probe_spec=probe_spec,
                task_id=task_id,
            )

            if not probe_result.get("passed"):
                error_msg = probe_result.get("error", "Deterministic probe check failed")
                probe_name = probe_spec.get("probe_type") or probe_result.get("probe") or f"{step.tool}_probe"
                verified_time = step.finished_at or step.started_at or "previously"

                # 1. Mark step drifted and task paused
                Ledger.mark_step_drifted(step.id, reason=error_msg, probe=probe_result, db_path=self.db_path)
                Ledger.update_task_status(task_id, "paused", db_path=self.db_path)

                # 2. Record drift event with owner attribution
                event_id = Ledger.record_drift_event(
                    task_id=task_id,
                    step_id=step.id,
                    tool=step.tool,
                    reason=error_msg,
                    explanation=None,
                    owner_id=owner_id,
                    db_path=self.db_path,
                )

                # 3. Trigger FAST model explanation in background (non-blocking) if cap allows
                can_spend_fast = True
                try:
                    check_daily_llm_limit(owner_id, db_path=self.db_path)
                except Exception:
                    can_spend_fast = False

                if can_spend_fast:
                    from backend.resume import _generate_fast_explanation_worker
                    threading.Thread(
                        target=_generate_fast_explanation_worker,
                        args=(event_id, step.intent, verified_time, probe_name, error_msg, owner_id, self.db_path),
                        daemon=True,
                    ).start()

                drifted_events.append((event_id, step.id))

        if not drifted_events:
            return None

        # Auto-heal decision
        policy = get_owner_policy(owner_id, db_path=self.db_path)
        heals_hr, heals_day = count_owner_auto_heals(owner_id, db_path=self.db_path)
        budget_ok = (heals_hr < MAX_AUTO_HEALS_PER_HOUR and heals_day < MAX_AUTO_HEALS_PER_DAY)

        llm_cap_ok = True
        try:
            check_daily_llm_limit(owner_id, db_path=self.db_path)
        except Exception:
            llm_cap_ok = False

        if policy.auto_heal and budget_ok and llm_cap_ok:
            # Mark drift events as auto_healed=1 and launch background resume
            with get_db(self.db_path) as conn:
                for eid, _ in drifted_events:
                    conn.execute("UPDATE drift_events SET auto_healed = 1 WHERE id = ?", (eid,))

            with self._in_flight_lock:
                self._auto_healing_tasks.add(task_id)

            threading.Thread(
                target=self._auto_heal_worker,
                args=(task_id, [eid for eid, _ in drifted_events]),
                daemon=True,
            ).start()

            return {
                "task_id": task_id,
                "owner_id": owner_id,
                "drift_events_count": len(drifted_events),
                "auto_heal_started": True,
            }
        else:
            # Leave task paused and set needs_attention=True
            Ledger.update_task_needs_attention(task_id, True, db_path=self.db_path)
            return {
                "task_id": task_id,
                "owner_id": owner_id,
                "drift_events_count": len(drifted_events),
                "auto_heal_started": False,
                "reason": "policy_disabled" if not policy.auto_heal else "budget_exhausted",
            }

    def _auto_heal_worker(self, task_id: str, drift_event_ids: List[str]) -> None:
        """Background worker executing task resume/reconciliation and marking healed events."""
        from backend.resume import resume_task

        try:
            res = resume_task(task_id, db_path=self.db_path)
            # If task successfully resumed to complete (or all repairs verified)
            summary = Ledger.get_task_summary(task_id, db_path=self.db_path)
            if summary.get("is_complete"):
                now = utc_now_iso()
                for eid in drift_event_ids:
                    Ledger.mark_drift_event_healed(eid, healed_at=now, db_path=self.db_path)
                Ledger.update_task_needs_attention(task_id, False, db_path=self.db_path)
        except Exception as e:
            logger.error("Auto-heal worker error on task %s: %s", task_id, str(e), exc_info=False)
            Ledger.update_task_needs_attention(task_id, True, db_path=self.db_path)
        finally:
            with self._in_flight_lock:
                self._auto_healing_tasks.discard(task_id)
                self._in_flight_tasks.discard(task_id)

    def _dispatch_scheduled_routines(self) -> List[Dict[str, Any]]:
        """Trigger due scheduled routines through run_skill, one at a time per owner."""
        from backend.skills import get_due_skills, run_skill, update_skill_schedule_outcome

        due_skills = get_due_skills(db_path=self.db_path)
        if not due_skills:
            return []

        dispatched = []
        for skill in due_skills:
            # One routine at a time per owner
            with self._in_flight_lock:
                if skill.owner_id in self._running_routine_owners:
                    continue
                self._running_routine_owners.add(skill.owner_id)

            # Check daily LLM cap before triggering routine execution
            try:
                check_daily_llm_limit(skill.owner_id, db_path=self.db_path)
            except Exception as e:
                logger.warning("Skipping scheduled routine %s: daily LLM cap exhausted for owner %s (%s)", skill.id, skill.owner_id, e)
                update_skill_schedule_outcome(skill.id, success=False, db_path=self.db_path)
                continue

            try:
                # Run the routine via the same code path as POST /skills/{id}/run
                result = run_skill(skill.id, owner_id=skill.owner_id, db_path=self.db_path)
                task_res = result.get("task", {})
                success = (task_res.get("status") == "complete")

                # Update schedule stats and handle consecutive failures
                update_skill_schedule_outcome(skill.id, success=success, db_path=self.db_path)
                dispatched.append({
                    "skill_id": skill.id,
                    "task_id": task_res.get("id"),
                    "owner_id": skill.owner_id,
                    "success": success,
                })
            except Exception as e:
                logger.error("Error executing scheduled skill %s: %s", skill.id, str(e), exc_info=False)
                update_skill_schedule_outcome(skill.id, success=False, db_path=self.db_path)
            finally:
                with self._in_flight_lock:
                    self._running_routine_owners.discard(skill.owner_id)

        return dispatched

    def get_status(self, owner_id: str) -> Dict[str, Any]:
        """Return owner-scoped watcher status and budget counters."""
        heals_hr, heals_day = count_owner_auto_heals(owner_id, db_path=self.db_path)
        return {
            "enabled": self.enabled,
            "interval": self.interval,
            "last_cycle_at": self.last_cycle_at,
            "heals_used_hour": heals_hr,
            "heals_remaining_hour": max(0, MAX_AUTO_HEALS_PER_HOUR - heals_hr),
            "heals_used_day": heals_day,
            "heals_remaining_day": max(0, MAX_AUTO_HEALS_PER_DAY - heals_day),
            "in_flight_tasks": len(self._in_flight_tasks),
        }


# Global singleton watcher instance
_GLOBAL_WATCHER: Optional[Watcher] = None


def get_global_watcher(db_path: Optional[str] = None) -> Watcher:
    """Return or initialize singleton Watcher instance."""
    global _GLOBAL_WATCHER
    if _GLOBAL_WATCHER is None:
        _GLOBAL_WATCHER = Watcher(db_path=db_path)
    return _GLOBAL_WATCHER


# -----------------------------------------------------------------------------
# Activity Feed Generator
# -----------------------------------------------------------------------------

def get_owner_activity(
    owner_id: str,
    limit: int = 50,
    db_path: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return aggregated activity feed for an owner, newest first (max 50).
    
    Item types:
    - drift_detected (with auto_healed, explanation, step intent)
    - healed (with task_id, step_id, label)
    - routine_ran (with status, task_id, label)
    - approval_pending (with task_id, step_id, tool, reason)
    - memory_proposal_pending (with memory_id, kind, label)

    Privacy guarantee: Identifiers and short labels only, never file contents or secrets.
    """
    init_db(db_path)
    items: List[Dict[str, Any]] = []

    with get_db(db_path) as conn:
        # 1. Drift events
        drift_rows = conn.execute(
            """
            SELECT d.id, d.task_id, d.step_id, d.tool, d.reason, d.explanation,
                   d.detected_at, d.auto_healed, d.healed_at, s.intent as step_intent
            FROM drift_events d
            LEFT JOIN steps s ON d.step_id = s.id
            WHERE d.owner_id = ?
            ORDER BY d.detected_at DESC
            LIMIT ?
            """,
            (owner_id, limit),
        ).fetchall()

        for r in drift_rows:
            # 1a. drift_detected item
            items.append({
                "id": r["id"],
                "type": "drift_detected",
                "task_id": r["task_id"],
                "step_id": r["step_id"],
                "step_intent": r["step_intent"] or f"Step {r['step_id'][:8]}",
                "explanation": r["explanation"] or r["reason"],
                "auto_healed": bool(r["auto_healed"]),
                "created_at": r["detected_at"],
            })

            # 1b. healed item (if event has been auto_healed and has healed_at)
            if r["auto_healed"] and r["healed_at"]:
                items.append({
                    "id": f"healed_{r['id']}",
                    "type": "healed",
                    "task_id": r["task_id"],
                    "step_id": r["step_id"],
                    "label": f"Step healed successfully: {r['step_intent'] or r['tool']}",
                    "created_at": r["healed_at"],
                })

        # 2. Routine runs (tasks with goal starting with 'Routine: ')
        routine_rows = conn.execute(
            """
            SELECT id, goal, status, created_at, updated_at
            FROM tasks
            WHERE owner_id = ? AND goal LIKE 'Routine: %'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (owner_id, limit),
        ).fetchall()

        for r in routine_rows:
            items.append({
                "id": f"routine_{r['id']}",
                "type": "routine_ran",
                "task_id": r["id"],
                "status": r["status"],
                "label": r["goal"],
                "created_at": r["updated_at"] or r["created_at"],
            })

        # 3. Pending approvals
        approval_rows = conn.execute(
            """
            SELECT id, task_id, step_id, tool, reason, created_at
            FROM approvals
            WHERE owner_id = ? AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (owner_id, limit),
        ).fetchall()

        for r in approval_rows:
            items.append({
                "id": f"approval_{r['id']}",
                "type": "approval_pending",
                "task_id": r["task_id"],
                "step_id": r["step_id"],
                "tool": r["tool"],
                "reason": r["reason"][:120],
                "created_at": r["created_at"],
            })

        # 4. Pending memory proposals
        mem_rows = conn.execute(
            """
            SELECT id, kind, text, created_at
            FROM memories
            WHERE owner_id = ? AND status = 'proposed'
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (owner_id, limit),
        ).fetchall()

        for r in mem_rows:
            items.append({
                "id": f"memory_{r['id']}",
                "type": "memory_proposal_pending",
                "task_id": None,
                "memory_id": r["id"],
                "kind": r["kind"],
                "label": f"{r['kind'].capitalize()} proposal: {r['text'][:60]}",
                "created_at": r["created_at"],
            })

    # Sort all items by created_at descending and take top 'limit'
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return items[:limit]
