"""Saha Ledger Module (Append-Only State Machine).

Core rules:
- NEVER DELETE STEPS.
- History is preserved permanently.
- Replaced steps transition to `superseded` with a pointer to their replacement.
- Drift events are permanently audited in the `drift_events` table.
- Current active plan = all non-superseded steps.
- Task is complete when every active step is `verified`.
"""

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union

from backend.db import DEFAULT_OWNER_ID, get_db, init_db


def utc_now_iso() -> str:
    """Return current UTC time in ISO 8601 format."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Task:
    id: str
    owner_id: str
    goal: str
    status: str  # planning | running | paused | complete | failed
    created_at: str
    updated_at: str
    needs_attention: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Step:
    id: str
    task_id: str
    idx: int
    seq: int
    intent: str
    tool: str
    args_json: str
    result_json: Optional[str]
    probe_json: Optional[str]
    status: str  # pending | running | done | verified | drifted | superseded | failed | blocked
    drift_reason: Optional[str] = None
    block_reason: Optional[str] = None
    superseded_by: Optional[str] = None
    resolved_by: Optional[str] = None
    depends_on_json: str = "[]"
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        try:
            d["args"] = json.loads(self.args_json) if self.args_json else {}
        except Exception:
            d["args"] = {}
        try:
            d["result"] = json.loads(self.result_json) if self.result_json else None
        except Exception:
            d["result"] = None
        try:
            d["probe"] = json.loads(self.probe_json) if self.probe_json else None
        except Exception:
            d["probe"] = None
        try:
            d["depends_on"] = json.loads(self.depends_on_json) if self.depends_on_json else []
        except Exception:
            d["depends_on"] = []
        return d


class Ledger:
    """Manages transactional, append-only state changes for Tasks and Steps."""

    @staticmethod
    def create_task(goal: str, owner_id: Optional[str] = None, task_id: Optional[str] = None, db_path: Optional[str] = None) -> Task:
        init_db(db_path)
        tid = task_id or str(uuid.uuid4())
        now = utc_now_iso()
        effective_owner = owner_id if owner_id is not None else DEFAULT_OWNER_ID 
        with get_db(db_path) as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, owner_id, goal, status, created_at, updated_at)
                VALUES (?, ?, ?, 'planning', ?, ?)
                """,
                (tid, effective_owner, goal.strip(), now, now),
            )
        task = Ledger.get_task(tid, db_path=db_path)
        if not task:
            raise RuntimeError(f"Failed to create task {tid}")
        return task

    @staticmethod
    def get_task(task_id: str, owner_id: Optional[str] = None, db_path: Optional[str] = None) -> Optional[Task]:
        init_db(db_path)
        with get_db(db_path) as conn:
            if owner_id:
                row = conn.execute("SELECT * FROM tasks WHERE id = ? AND owner_id = ?", (task_id, owner_id)).fetchone()
            else:
                row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return None
            return Task(
                id=row["id"],
                owner_id=row["owner_id"] if "owner_id" in row.keys() else "default",
                goal=row["goal"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                needs_attention=bool(row["needs_attention"]) if "needs_attention" in row.keys() else False,
            )

    @staticmethod
    def list_tasks(owner_id: Optional[str] = None, status: Optional[str] = None, limit: int = 50, db_path: Optional[str] = None) -> List[Task]:
        init_db(db_path)
        with get_db(db_path) as conn:
            query = "SELECT * FROM tasks"
            conditions = []
            params: List[Any] = []
            if owner_id:
                conditions.append("owner_id = ?")
                params.append(owner_id)
            if status:
                conditions.append("status = ?")
                params.append(status)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [
                Task(
                    id=r["id"],
                    owner_id=r["owner_id"] if "owner_id" in r.keys() else "default",
                    goal=r["goal"],
                    status=r["status"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    needs_attention=bool(r["needs_attention"]) if "needs_attention" in r.keys() else False,
                )
                for r in rows
            ]

    @staticmethod
    def update_task_status(task_id: str, status: str, db_path: Optional[str] = None) -> None:
        valid = {"planning", "running", "paused", "complete", "failed"}
        if status not in valid:
            raise ValueError(f"Invalid task status '{status}'. Must be one of {valid}")
        now = utc_now_iso()
        with get_db(db_path) as conn:
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, task_id),
            )

    @staticmethod
    def update_task_needs_attention(task_id: str, needs_attention: bool, db_path: Optional[str] = None) -> None:
        init_db(db_path)
        now = utc_now_iso()
        with get_db(db_path) as conn:
            conn.execute(
                "UPDATE tasks SET needs_attention = ?, updated_at = ? WHERE id = ?",
                (1 if needs_attention else 0, now, task_id),
            )

    @staticmethod
    def get_max_seq(task_id: str, db_path: Optional[str] = None) -> int:
        init_db(db_path)
        with get_db(db_path) as conn:
            row = conn.execute("SELECT MAX(seq) as max_seq FROM steps WHERE task_id = ?", (task_id,)).fetchone()
            return row["max_seq"] or 0

    @staticmethod
    def add_steps(task_id: str, steps_data: List[Dict[str, Any]], db_path: Optional[str] = None) -> List[Step]:
        init_db(db_path)
        now = utc_now_iso()
        with get_db(db_path) as conn:
            row = conn.execute("SELECT MAX(seq) as max_seq FROM steps WHERE task_id = ?", (task_id,)).fetchone()
            curr_seq = (row["max_seq"] or 0)

            for s in steps_data:
                curr_seq += 1
                sid = s.get("id") or str(uuid.uuid4())
                idx = int(s.get("idx", curr_seq))
                intent = str(s["intent"]).strip()
                tool = str(s["tool"]).strip()
                step_status = s.get("status", "pending")
                args = s.get("args", {})
                args_json = json.dumps(args) if not isinstance(args, str) else args
                probe = s.get("probe", {})
                probe_json = json.dumps(probe) if not isinstance(probe, str) else probe
                depends_on = s.get("depends_on", [])
                depends_on_json = json.dumps(depends_on) if not isinstance(depends_on, str) else depends_on
                step_error = s.get("error")
                block_reason = s.get("block_reason") or (s.get("error") if step_status == "blocked" else None)

                conn.execute(
                    """
                    INSERT INTO steps (id, task_id, idx, seq, intent, tool, args_json, probe_json, status, error, block_reason, resolved_by, depends_on_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (sid, task_id, idx, curr_seq, intent, tool, args_json, probe_json, step_status, step_error, block_reason, depends_on_json),
                )
            conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now, task_id))

        return Ledger.get_steps(task_id, db_path=db_path)

    @staticmethod
    def mark_step_blocked(step_id: str, reason: str, db_path: Optional[str] = None) -> Step:
        init_db(db_path)
        now = utc_now_iso()
        with get_db(db_path) as conn:
            conn.execute(
                """
                UPDATE steps
                SET status = 'blocked', block_reason = ?, error = ?, finished_at = ?
                WHERE id = ?
                """,
                (str(reason), str(reason), now, step_id),
            )
        step = Ledger.get_step(step_id, db_path)
        if not step:
            raise RuntimeError(f"Step {step_id} not found")
        return step

    @staticmethod
    def get_steps(task_id: str, include_superseded: bool = True, db_path: Optional[str] = None) -> List[Step]:
        """Return steps for a task ordered by insertion sequence (seq)."""
        init_db(db_path)
        with get_db(db_path) as conn:
            if include_superseded:
                rows = conn.execute(
                    "SELECT * FROM steps WHERE task_id = ? ORDER BY seq ASC",
                    (task_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM steps WHERE task_id = ? AND status != 'superseded' ORDER BY seq ASC",
                    (task_id,),
                ).fetchall()

            return [
                Step(
                    id=r["id"],
                    task_id=r["task_id"],
                    idx=r["idx"],
                    seq=r["seq"],
                    intent=r["intent"],
                    tool=r["tool"],
                    args_json=r["args_json"],
                    result_json=r["result_json"],
                    probe_json=r["probe_json"],
                    status=r["status"],
                    drift_reason=r["drift_reason"],
                    block_reason=r["block_reason"] if "block_reason" in r.keys() else None,
                    superseded_by=r["superseded_by"],
                    resolved_by=r["resolved_by"] if "resolved_by" in r.keys() else None,
                    depends_on_json=r["depends_on_json"] if "depends_on_json" in r.keys() else "[]",
                    error=r["error"],
                    started_at=r["started_at"],
                    finished_at=r["finished_at"],
                )
                for r in rows
            ]

    @staticmethod
    def get_step(step_id: str, db_path: Optional[str] = None) -> Optional[Step]:
        init_db(db_path)
        with get_db(db_path) as conn:
            r = conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
            if not r:
                return None
            return Step(
                id=r["id"],
                task_id=r["task_id"],
                idx=r["idx"],
                seq=r["seq"],
                intent=r["intent"],
                tool=r["tool"],
                args_json=r["args_json"],
                result_json=r["result_json"],
                probe_json=r["probe_json"],
                status=r["status"],
                drift_reason=r["drift_reason"],
                block_reason=r["block_reason"] if "block_reason" in r.keys() else None,
                superseded_by=r["superseded_by"],
                resolved_by=r["resolved_by"] if "resolved_by" in r.keys() else None,
                depends_on_json=r["depends_on_json"] if "depends_on_json" in r.keys() else "[]",
                error=r["error"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
            )

    @staticmethod
    def mark_step_running(step_id: str, db_path: Optional[str] = None) -> Step:
        init_db(db_path)
        now = utc_now_iso()
        with get_db(db_path) as conn:
            conn.execute(
                """
                UPDATE steps
                SET status = 'running', started_at = ?, error = NULL
                WHERE id = ?
                """,
                (now, step_id),
            )
            conn.execute(
                """
                UPDATE tasks
                SET updated_at = ?
                WHERE id = (SELECT task_id FROM steps WHERE id = ?)
                """,
                (now, step_id),
            )
        step = Ledger.get_step(step_id, db_path)
        if not step:
            raise RuntimeError(f"Step {step_id} not found")
        return step

    @staticmethod
    def mark_step_done(
        step_id: str,
        result: Union[Dict[str, Any], str],
        db_path: Optional[str] = None,
    ) -> Step:
        init_db(db_path)
        now = utc_now_iso()
        result_json = json.dumps(result) if not isinstance(result, str) else result
        with get_db(db_path) as conn:
            conn.execute(
                """
                UPDATE steps
                SET status = 'done', result_json = ?, finished_at = ?
                WHERE id = ?
                """,
                (result_json, now, step_id),
            )
        step = Ledger.get_step(step_id, db_path)
        if not step:
            raise RuntimeError(f"Step {step_id} not found")
        return step

    @staticmethod
    def mark_step_verified(
        step_id: str,
        probe: Union[Dict[str, Any], str],
        db_path: Optional[str] = None,
    ) -> Step:
        init_db(db_path)
        probe_json = json.dumps(probe) if not isinstance(probe, str) else probe
        with get_db(db_path) as conn:
            conn.execute(
                """
                UPDATE steps
                SET status = 'verified', probe_json = ?
                WHERE id = ?
                """,
                (probe_json, step_id),
            )
        step = Ledger.get_step(step_id, db_path)
        if not step:
            raise RuntimeError(f"Step {step_id} not found")
        return step

    @staticmethod
    def mark_step_drifted(
        step_id: str,
        reason: str,
        probe: Union[Dict[str, Any], str],
        db_path: Optional[str] = None,
    ) -> Step:
        init_db(db_path)
        probe_json = json.dumps(probe) if not isinstance(probe, str) else probe
        with get_db(db_path) as conn:
            conn.execute(
                """
                UPDATE steps
                SET status = 'drifted', drift_reason = ?, probe_json = ?
                WHERE id = ?
                """,
                (str(reason), probe_json, step_id),
            )
        step = Ledger.get_step(step_id, db_path)
        if not step:
            raise RuntimeError(f"Step {step_id} not found")
        return step

    @staticmethod
    def record_drift_event(
        task_id: str,
        step_id: str,
        tool: str,
        reason: str,
        explanation: Optional[str] = None,
        owner_id: Optional[str] = None,
        auto_healed: bool = False,
        healed_at: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> str:
        """Record an immutable drift event in the drift_events table."""
        from backend.db import get_task_owner_id
        init_db(db_path)
        event_id = str(uuid.uuid4())
        now = utc_now_iso()
        effective_owner = owner_id or get_task_owner_id(task_id, db_path=db_path) or "default"
        with get_db(db_path) as conn:
            conn.execute(
                """
                INSERT INTO drift_events (id, owner_id, task_id, step_id, tool, reason, explanation, detected_at, auto_healed, healed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, effective_owner, task_id, step_id, tool, reason, explanation, now, 1 if auto_healed else 0, healed_at),
            )
        return event_id

    @staticmethod
    def mark_drift_event_healed(
        event_id: str,
        healed_at: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> None:
        """Mark a drift event as auto_healed with a timestamp."""
        init_db(db_path)
        now = healed_at or utc_now_iso()
        with get_db(db_path) as conn:
            conn.execute(
                "UPDATE drift_events SET auto_healed = 1, healed_at = ? WHERE id = ?",
                (now, event_id),
            )

    @staticmethod
    def update_drift_explanation(
        event_id: str,
        explanation: str,
        db_path: Optional[str] = None,
    ) -> None:
        """Update the plain-English explanation for an existing drift event."""
        init_db(db_path)
        with get_db(db_path) as conn:
            conn.execute(
                "UPDATE drift_events SET explanation = ? WHERE id = ?",
                (explanation, event_id),
            )

    @staticmethod
    def supersede_steps(
        step_ids: List[str],
        superseded_by_id: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> None:
        """Transition steps to 'superseded' without deleting them. Drifted steps are never overwritten."""
        if not step_ids:
            return
        init_db(db_path)
        with get_db(db_path) as conn:
            for sid in step_ids:
                conn.execute(
                    """
                    UPDATE steps
                    SET status = 'superseded', superseded_by = ?
                    WHERE id = ? AND status != 'drifted'
                    """,
                    (superseded_by_id, sid),
                )

    @staticmethod
    def mark_step_resolved(
        drifted_step_id: str,
        repair_step_id: str,
        db_path: Optional[str] = None,
    ) -> Step:
        """Link a drifted step to the repair step that resolves it."""
        init_db(db_path)
        with get_db(db_path) as conn:
            conn.execute(
                """
                UPDATE steps
                SET resolved_by = ?
                WHERE id = ?
                """,
                (repair_step_id, drifted_step_id),
            )
        step = Ledger.get_step(drifted_step_id, db_path)
        if not step:
            raise RuntimeError(f"Step {drifted_step_id} not found")
        return step

    @staticmethod
    def mark_step_failed(
        step_id: str,
        error: str,
        result: Optional[Union[Dict[str, Any], str]] = None,
        probe: Optional[Union[Dict[str, Any], str]] = None,
        db_path: Optional[str] = None,
    ) -> Step:
        init_db(db_path)
        now = utc_now_iso()
        res_json = json.dumps(result) if result and not isinstance(result, str) else result
        prb_json = json.dumps(probe) if probe and not isinstance(probe, str) else probe
        with get_db(db_path) as conn:
            conn.execute(
                """
                UPDATE steps
                SET status = 'failed', error = ?, result_json = COALESCE(?, result_json),
                    probe_json = COALESCE(?, probe_json), finished_at = ?
                WHERE id = ?
                """,
                (str(error), res_json, prb_json, now, step_id),
            )
        step = Ledger.get_step(step_id, db_path)
        if not step:
            raise RuntimeError(f"Step {step_id} not found")
        return step

    @staticmethod
    def get_task_summary(task_id: str, owner_id: Optional[str] = None, db_path: Optional[str] = None) -> Dict[str, Any]:
        task = Ledger.get_task(task_id, owner_id=owner_id, db_path=db_path)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        all_steps = Ledger.get_steps(task_id, include_superseded=True, db_path=db_path)
        active_steps = [s for s in all_steps if s.status != "superseded"]

        counts = {
            "pending": 0,
            "running": 0,
            "done": 0,
            "verified": 0,
            "drifted": 0,
            "superseded": 0,
            "failed": 0,
            "blocked": 0,
        }
        drifted_steps = []
        for s in all_steps:
            counts[s.status] = counts.get(s.status, 0) + 1
            if s.status == "drifted":
                drifted_steps.append(s.to_dict())

        # Retrieve drift events
        with get_db(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM drift_events WHERE task_id = ? ORDER BY detected_at DESC",
                (task_id,),
            ).fetchall()
            drift_events = [dict(r) for r in rows]

        # Task complete = every non-superseded step is verified, and every drifted step has a verified resolved_by step
        steps_by_id = {s.id: s for s in all_steps}
        non_superseded = [s for s in all_steps if s.status != "superseded"]
        drifted = [s for s in all_steps if s.status == "drifted"]

        has_incomplete_active = any(
            s.status in ("pending", "running", "failed", "blocked") for s in non_superseded
        )
        non_superseded_non_drifted_verified = (
            len(non_superseded) > 0 and
            all(s.status == "verified" for s in non_superseded if s.status != "drifted")
        )
        drifted_resolved = all(
            s.resolved_by is not None and
            s.resolved_by in steps_by_id and
            steps_by_id[s.resolved_by].status == "verified"
            for s in drifted
        )
        is_complete = (
            not has_incomplete_active and
            non_superseded_non_drifted_verified and
            drifted_resolved
        )

        return {
            "task": task.to_dict(),
            "total_steps_count": len(all_steps),
            "active_steps_count": len(active_steps),
            "counts": counts,
            "has_drift": counts["drifted"] > 0,
            "drifted_steps": drifted_steps,
            "drift_events": drift_events,
            "is_complete": is_complete,
            "steps": [s.to_dict() for s in all_steps],
            "active_steps": [s.to_dict() for s in active_steps],
        }

    @staticmethod
    def update_step_args(
        step_id: str,
        args: Union[Dict[str, Any], str],
        db_path: Optional[str] = None,
    ) -> Step:
        """Update step args_json in SQLite ledger."""
        init_db(db_path)
        args_json = json.dumps(args) if not isinstance(args, str) else args
        with get_db(db_path) as conn:
            conn.execute(
                "UPDATE steps SET args_json = ? WHERE id = ?",
                (args_json, step_id),
            )
        step = Ledger.get_step(step_id, db_path)
        if not step:
            raise RuntimeError(f"Step {step_id} not found")
        return step

    @staticmethod
    def reset_step_to_pending(
        step_id: str,
        db_path: Optional[str] = None,
    ) -> Step:
        """Reset an interrupted or unverified step back to pending for re-execution."""
        init_db(db_path)
        with get_db(db_path) as conn:
            conn.execute(
                """
                UPDATE steps
                SET status = 'pending', started_at = NULL, finished_at = NULL, error = NULL
                WHERE id = ?
                """,
                (step_id,),
            )
        step = Ledger.get_step(step_id, db_path)
        if not step:
            raise RuntimeError(f"Step {step_id} not found")
        return step

    @staticmethod
    def recover_interrupted_step(
        step: Step,
        task_id: str,
        db_path: Optional[str] = None,
    ) -> Step:
        """Conservatively evaluate an interrupted step in 'running' or 'done' state.

        If the step was in 'done' state with a recorded result, runs deterministic
        verification. If the probe passes, transitions to 'verified'.
        If the step is an interrupted external HTTP mutation:
          - If the target supports idempotency and a stable key is available, resets
            to 'pending' for safe deduplicated retry.
          - If the outcome cannot be verified and deduplication is unavailable, does
            NOT automatically retry. Transitions to 'blocked' (needs reconciliation)
            and creates an approval request requiring authorized decision.
        For other unverified or interrupted steps (e.g. local idempotent operations or
        safe read-only HTTP GET), resets to 'pending'.
        """
        from backend.verifiers import verify_step
        from backend.tools.http import is_http_mutation_step, target_supports_idempotency

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

        # 1. If step is in 'running' or 'done', re-run deterministic probe first
        if step.status in ("running", "done"):
            probe_result = verify_step(
                tool=step.tool,
                args=args,
                result=result,
                probe_spec=probe_spec,
                task_id=task_id,
            )
            if probe_result.get("passed"):
                return Ledger.mark_step_verified(step.id, probe=probe_result, db_path=db_path)

        # 2. Check if this is an external HTTP mutation
        if is_http_mutation_step(step.tool, args):
            from backend.db import get_task_owner_id
            from backend.policy import create_approval, get_owner_policy
            from backend.tools.http import target_supports_idempotency

            owner_id = get_task_owner_id(task_id, db_path=db_path) or "default"
            policy = get_owner_policy(owner_id, db_path=db_path)

            # Check if deduplication / stable idempotency key is TRUSTED by owner configuration
            trusted = target_supports_idempotency(
                tool=step.tool,
                args=args,
                policy=policy,
                owner_id=owner_id,
                db_path=db_path,
            )

            if trusted:
                # Deduplication is verified by owner-controlled trusted configuration!
                # Ensure stable key is persisted for retry
                if not args.get("idempotency_key"):
                    args["idempotency_key"] = f"idem_{step.id}"
                    Ledger.update_step_args(step.id, args, db_path=db_path)
                return Ledger.reset_step_to_pending(step.id, db_path=db_path)
            else:
                # Ambiguous mutation without trusted deduplication:
                # The remote service may already have applied the mutation.
                # Planner output alone can NEVER authorize automatic retry!
                # Record explicit ambiguous state and require authorized owner decision.
                method = str(args.get("method", "POST" if ("body" in args or "json" in args) else "GET")).upper()
                url = args.get("url", "")
                reason = (
                    f"Ambiguous external HTTP mutation ({method} {url}): "
                    "Execution was interrupted before a response was recorded, and idempotency deduplication "
                    "is not confirmed by trusted owner policy. The remote outcome may already have taken effect or may be unknown. "
                    "Approving this request will re-dispatch (retry) the mutation to the remote service; "
                    "denying it will mark the step failed without re-sending the request."
                )
                create_approval(
                    owner_id=owner_id,
                    task_id=task_id,
                    step_id=step.id,
                    tool=step.tool,
                    args=args,
                    reason=reason,
                    db_path=db_path,
                )
                return Ledger.mark_step_blocked(step.id, reason=reason, db_path=db_path)

        # 3. Conservative fallback for safe/idempotent steps: reset to pending
        return Ledger.reset_step_to_pending(step.id, db_path=db_path)

    @staticmethod
    def recover_task_interrupted_steps(
        task_id: str,
        db_path: Optional[str] = None,
    ) -> List[Step]:
        """Recover any active steps in 'running' or 'done' status for a task."""
        init_db(db_path)
        all_steps = Ledger.get_steps(task_id, include_superseded=False, db_path=db_path)
        recovered = []
        for s in all_steps:
            if s.status in ("running", "done"):
                rec_step = Ledger.recover_interrupted_step(s, task_id=task_id, db_path=db_path)
                recovered.append(rec_step)
        return recovered

    @staticmethod
    def recover_stale_running_tasks(db_path: Optional[str] = None) -> List[str]:
        """Find any tasks left in 'running' state after a process crash and recover them."""
        from backend.db import get_task_owner_id
        from backend.policy import get_owner_policy
        import threading

        init_db(db_path)
        with get_db(db_path) as conn:
            rows = conn.execute("SELECT id, owner_id FROM tasks WHERE status = 'running'").fetchall()
            tasks = [(r["id"], r["owner_id"] if "owner_id" in r.keys() else "default") for r in rows]

        recovered_ids = []
        for tid, owner_id in tasks:
            recovered_ids.append(tid)
            Ledger.recover_task_interrupted_steps(tid, db_path=db_path)

            active_steps = Ledger.get_steps(tid, include_superseded=False, db_path=db_path)
            all_verified = bool(active_steps) and all(s.status == "verified" for s in active_steps)
            has_pending = any(s.status == "pending" for s in active_steps)

            if all_verified:
                Ledger.update_task_status(tid, "complete", db_path=db_path)
                continue

            policy = get_owner_policy(owner_id, db_path=db_path)
            if policy.auto_heal and has_pending:
                from backend.executor import run_task
                threading.Thread(target=run_task, args=(tid, db_path), daemon=True).start()
            else:
                Ledger.update_task_status(tid, "paused", db_path=db_path)
                Ledger.update_task_needs_attention(tid, True, db_path=db_path)

        return recovered_ids
