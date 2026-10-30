"""Saha Saved Routines (Skills-Lite) Module.

Provides:
- Saving verified task plans as reusable skills (max 8 steps, validated against owner policy & public mode).
- Listing and deleting owner-scoped skills.
- Running saved skills: clones steps as pending, runs through normal executor & probes,
  updates stats (runs, verified_runs, last_run_at, last_status).
- Scheduling skills: PUT /skills/{id}/schedule with interval limits, next_run_at calculation,
  consecutive failure tracking, and needs_attention signaling.
"""

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from backend.db import get_db, init_db, utc_now_iso
from backend.executor import run_task
from backend.ledger import Ledger
from backend.policy import PUBLIC_ALLOWED_TOOLS, get_owner_policy, is_public_mode

logger = logging.getLogger(__name__)

MAX_SKILL_NAME_LEN = 60
MAX_SKILL_DESC_LEN = 200
MAX_SKILL_STEPS = 8
MAX_SCHEDULED_ROUTINES_PER_OWNER = 2
MAX_SCHEDULE_MINUTES = 1440


def get_min_schedule_minutes() -> int:
    """Return minimum allowed routine schedule interval in minutes."""
    try:
        val = int(os.getenv("SAHA_MIN_SCHEDULE_MINUTES", "10"))
        return max(1, val)
    except ValueError:
        return 10


@dataclass
class Skill:
    id: str
    owner_id: str
    name: str
    description: str
    steps_json: str
    source_task_id: str
    created_at: str
    runs: int = 0
    verified_runs: int = 0
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    schedule_minutes: Optional[int] = None
    next_run_at: Optional[str] = None
    consecutive_failures: int = 0
    needs_attention: bool = False

    def to_dict(self) -> Dict[str, Any]:
        try:
            steps = json.loads(self.steps_json) if self.steps_json else []
        except Exception:
            steps = []
        return {
            "id": self.id,
            "owner_id": self.owner_id,
            "name": self.name,
            "description": self.description,
            "steps": steps,
            "source_task_id": self.source_task_id,
            "created_at": self.created_at,
            "runs": self.runs,
            "verified_runs": self.verified_runs,
            "last_run_at": self.last_run_at,
            "last_status": self.last_status,
            "schedule_minutes": self.schedule_minutes,
            "next_run_at": self.next_run_at,
            "consecutive_failures": self.consecutive_failures,
            "needs_attention": self.needs_attention,
        }


def get_skills(owner_id: str, db_path: Optional[str] = None) -> List[Skill]:
    """List all skills owned by an owner ordered by creation time."""
    init_db(db_path)
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM skills
            WHERE owner_id = ?
            ORDER BY created_at DESC
            """,
            (owner_id,),
        ).fetchall()
        return [
            Skill(
                id=r["id"],
                owner_id=r["owner_id"],
                name=r["name"],
                description=r["description"],
                steps_json=r["steps_json"],
                source_task_id=r["source_task_id"],
                created_at=r["created_at"],
                runs=r["runs"],
                verified_runs=r["verified_runs"],
                last_run_at=r["last_run_at"],
                last_status=r["last_status"],
                schedule_minutes=r["schedule_minutes"] if "schedule_minutes" in r.keys() else None,
                next_run_at=r["next_run_at"] if "next_run_at" in r.keys() else None,
                consecutive_failures=r["consecutive_failures"] if "consecutive_failures" in r.keys() else 0,
                needs_attention=bool(r["needs_attention"]) if "needs_attention" in r.keys() else False,
            )
            for r in rows
        ]


def get_skill(skill_id: str, owner_id: Optional[str] = None, db_path: Optional[str] = None) -> Optional[Skill]:
    """Get a single skill by ID, optionally verifying owner."""
    init_db(db_path)
    with get_db(db_path) as conn:
        if owner_id:
            row = conn.execute(
                "SELECT * FROM skills WHERE id = ? AND owner_id = ?",
                (skill_id, owner_id),
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM skills WHERE id = ?", (skill_id,)).fetchone()

        if not row:
            return None
        return Skill(
            id=row["id"],
            owner_id=row["owner_id"],
            name=row["name"],
            description=row["description"],
            steps_json=row["steps_json"],
            source_task_id=row["source_task_id"],
            created_at=row["created_at"],
            runs=row["runs"],
            verified_runs=row["verified_runs"],
            last_run_at=row["last_run_at"],
            last_status=row["last_status"],
            schedule_minutes=row["schedule_minutes"] if "schedule_minutes" in row.keys() else None,
            next_run_at=row["next_run_at"] if "next_run_at" in row.keys() else None,
            consecutive_failures=row["consecutive_failures"] if "consecutive_failures" in row.keys() else 0,
            needs_attention=bool(row["needs_attention"]) if "needs_attention" in row.keys() else False,
        )


def delete_skill(skill_id: str, owner_id: str, db_path: Optional[str] = None) -> bool:
    """Delete a skill by ID with owner scoping."""
    init_db(db_path)
    with get_db(db_path) as conn:
        cursor = conn.execute(
            "DELETE FROM skills WHERE id = ? AND owner_id = ?",
            (skill_id, owner_id),
        )
        return cursor.rowcount > 0


def create_skill_from_task(
    task_id: str,
    owner_id: str,
    name: str,
    description: str = "",
    db_path: Optional[str] = None,
) -> Skill:
    """Save a task's verified plan as a skill with validation."""
    init_db(db_path)
    task = Ledger.get_task(task_id, owner_id=owner_id, db_path=db_path)
    if not task:
        raise KeyError(f"Task {task_id} not found for this owner")

    clean_name = str(name).strip()
    if not clean_name:
        raise ValueError("Skill name cannot be empty.")
    if len(clean_name) > MAX_SKILL_NAME_LEN:
        raise ValueError(f"Skill name exceeds maximum of {MAX_SKILL_NAME_LEN} characters.")

    clean_desc = str(description or "").strip()
    if len(clean_desc) > MAX_SKILL_DESC_LEN:
        raise ValueError(f"Skill description exceeds maximum of {MAX_SKILL_DESC_LEN} characters.")

    # Verify task plan is fully verified
    active_steps = Ledger.get_steps(task_id, include_superseded=False, db_path=db_path)
    effective_steps = [
        s for s in active_steps
        if s.status != "superseded" and not (s.status == "drifted" and s.resolved_by)
    ]
    if not effective_steps:
        raise ValueError("Task has no steps to save.")

    if not all(s.status == "verified" for s in effective_steps):
        raise ValueError("Only fully verified task plans can be saved as a skill.")

    if len(effective_steps) > MAX_SKILL_STEPS:
        raise ValueError(f"Task plan has {len(effective_steps)} steps, exceeding maximum allowed of {MAX_SKILL_STEPS}.")

    # Validate tools against owner policy and public mode limits
    owner_policy = get_owner_policy(owner_id, db_path=db_path)
    public_mode = is_public_mode()

    steps_to_save: List[Dict[str, Any]] = []
    for s in effective_steps:
        if s.tool not in owner_policy.allowed_tools:
            raise ValueError(f"Tool '{s.tool}' is not permitted by owner policy.")
        if public_mode and s.tool not in PUBLIC_ALLOWED_TOOLS:
            raise ValueError(f"Tool '{s.tool}' is not permitted in public mode.")

        try:
            args = json.loads(s.args_json) if s.args_json else {}
        except Exception:
            args = {}
        try:
            probe = json.loads(s.probe_json) if s.probe_json else {}
        except Exception:
            probe = {}
        try:
            depends_on = json.loads(s.depends_on_json) if s.depends_on_json else []
        except Exception:
            depends_on = []

        step_record = {
            "idx": s.idx,
            "intent": s.intent,
            "tool": s.tool,
            "args": args,
            "content_brief": args.get("content_brief"),
            "probe": probe,
            "depends_on": depends_on,
        }
        steps_to_save.append(step_record)

    skill_id = str(uuid.uuid4())
    now = utc_now_iso()
    steps_json = json.dumps(steps_to_save)

    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO skills (
                id, owner_id, name, description, steps_json, source_task_id,
                created_at, runs, verified_runs, last_run_at, last_status,
                schedule_minutes, next_run_at, consecutive_failures, needs_attention
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, NULL, NULL, NULL, NULL, 0, 0)
            """,
            (skill_id, owner_id, clean_name, clean_desc, steps_json, task_id, now),
        )

    return Skill(
        id=skill_id,
        owner_id=owner_id,
        name=clean_name,
        description=clean_desc,
        steps_json=steps_json,
        source_task_id=task_id,
        created_at=now,
        runs=0,
        verified_runs=0,
        last_run_at=None,
        last_status=None,
        schedule_minutes=None,
        next_run_at=None,
        consecutive_failures=0,
        needs_attention=False,
    )


def schedule_skill(
    skill_id: str,
    owner_id: str,
    minutes: Optional[int],
    db_path: Optional[str] = None,
) -> Skill:
    """Configure or cancel recurring schedule for a skill."""
    init_db(db_path)
    skill = get_skill(skill_id, owner_id=owner_id, db_path=db_path)
    if not skill:
        raise KeyError(f"Skill {skill_id} not found for this owner")

    # If minutes is None or 0, disable schedule
    if minutes is None or minutes <= 0:
        with get_db(db_path) as conn:
            conn.execute(
                """
                UPDATE skills
                SET schedule_minutes = NULL, next_run_at = NULL, consecutive_failures = 0, needs_attention = 0
                WHERE id = ? AND owner_id = ?
                """,
                (skill_id, owner_id),
            )
        updated = get_skill(skill_id, owner_id=owner_id, db_path=db_path)
        if not updated:
            raise KeyError(f"Skill {skill_id} not found")
        return updated

    # Validate interval limits
    min_interval = get_min_schedule_minutes()
    if minutes < min_interval:
        raise ValueError(f"Schedule interval must be at least {min_interval} minutes.")
    if minutes > MAX_SCHEDULE_MINUTES:
        raise ValueError(f"Schedule interval cannot exceed {MAX_SCHEDULE_MINUTES} minutes.")

    # Validate max scheduled routines per owner (excluding current skill)
    with get_db(db_path) as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) as c FROM skills
            WHERE owner_id = ? AND schedule_minutes IS NOT NULL AND id != ?
            """,
            (owner_id, skill_id),
        ).fetchone()
        current_scheduled = row["c"] if row else 0
        if current_scheduled >= MAX_SCHEDULED_ROUTINES_PER_OWNER:
            raise ValueError(f"Maximum {MAX_SCHEDULED_ROUTINES_PER_OWNER} scheduled routines allowed per owner.")

        next_run = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
        conn.execute(
            """
            UPDATE skills
            SET schedule_minutes = ?, next_run_at = ?, consecutive_failures = 0, needs_attention = 0
            WHERE id = ? AND owner_id = ?
            """,
            (minutes, next_run, skill_id, owner_id),
        )

    updated = get_skill(skill_id, owner_id=owner_id, db_path=db_path)
    if not updated:
        raise KeyError(f"Skill {skill_id} not found")
    return updated


def get_due_skills(db_path: Optional[str] = None) -> List[Skill]:
    """Retrieve all skills whose scheduled next_run_at is due (<= now)."""
    init_db(db_path)
    now = utc_now_iso()
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT * FROM skills
            WHERE schedule_minutes IS NOT NULL AND next_run_at IS NOT NULL AND next_run_at <= ?
            ORDER BY next_run_at ASC
            """,
            (now,),
        ).fetchall()
        return [
            Skill(
                id=r["id"],
                owner_id=r["owner_id"],
                name=r["name"],
                description=r["description"],
                steps_json=r["steps_json"],
                source_task_id=r["source_task_id"],
                created_at=r["created_at"],
                runs=r["runs"],
                verified_runs=r["verified_runs"],
                last_run_at=r["last_run_at"],
                last_status=r["last_status"],
                schedule_minutes=r["schedule_minutes"],
                next_run_at=r["next_run_at"],
                consecutive_failures=r["consecutive_failures"] if "consecutive_failures" in r.keys() else 0,
                needs_attention=bool(r["needs_attention"]) if "needs_attention" in r.keys() else False,
            )
            for r in rows
        ]


def update_skill_schedule_outcome(
    skill_id: str,
    success: bool,
    db_path: Optional[str] = None,
) -> Optional[Skill]:
    """Update next_run_at and consecutive_failures after a scheduled routine run.
    
    After 3 consecutive failures, disables schedule and sets needs_attention.
    """
    init_db(db_path)
    skill = get_skill(skill_id, db_path=db_path)
    if not skill or not skill.schedule_minutes:
        return skill

    now_dt = datetime.now(timezone.utc)
    if success:
        next_run = (now_dt + timedelta(minutes=skill.schedule_minutes)).isoformat()
        with get_db(db_path) as conn:
            conn.execute(
                """
                UPDATE skills
                SET consecutive_failures = 0, next_run_at = ?, needs_attention = 0
                WHERE id = ?
                """,
                (next_run, skill_id),
            )
    else:
        new_failures = skill.consecutive_failures + 1
        if new_failures >= 3:
            # Disable schedule and signal needs_attention
            with get_db(db_path) as conn:
                conn.execute(
                    """
                    UPDATE skills
                    SET schedule_minutes = NULL, next_run_at = NULL,
                        consecutive_failures = ?, needs_attention = 1
                    WHERE id = ?
                    """,
                    (new_failures, skill_id),
                )
        else:
            next_run = (now_dt + timedelta(minutes=skill.schedule_minutes)).isoformat()
            with get_db(db_path) as conn:
                conn.execute(
                    """
                    UPDATE skills
                    SET consecutive_failures = ?, next_run_at = ?
                    WHERE id = ?
                    """,
                    (new_failures, next_run, skill_id),
                )

    return get_skill(skill_id, db_path=db_path)


def run_skill(skill_id: str, owner_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Run a saved skill: clones steps as pending into a new task and runs it."""
    init_db(db_path)
    skill = get_skill(skill_id, owner_id=owner_id, db_path=db_path)
    if not skill:
        raise KeyError(f"Skill {skill_id} not found for this owner")

    try:
        steps_data = json.loads(skill.steps_json) if skill.steps_json else []
    except Exception as e:
        raise ValueError(f"Invalid steps stored in skill: {e}")

    if not steps_data:
        raise ValueError("Skill has no steps to execute.")

    # Create new task cloning the steps as pending
    task_goal = f"Routine: {skill.name}"
    new_task = Ledger.create_task(goal=task_goal, owner_id=owner_id, db_path=db_path)

    cloned_steps: List[Dict[str, Any]] = []
    for s in steps_data:
        args = dict(s.get("args", {}))
        # Ensure content_brief is preserved in args so WORKER regenerates content
        if "content_brief" in s and s["content_brief"] and "content_brief" not in args:
            args["content_brief"] = s["content_brief"]

        cloned_steps.append({
            "idx": s.get("idx"),
            "intent": s.get("intent"),
            "tool": s.get("tool"),
            "args": args,
            "probe": s.get("probe", {}),
            "depends_on": s.get("depends_on", []),
            "status": "pending",
        })

    Ledger.add_steps(new_task.id, cloned_steps, db_path=db_path)

    # Run the task through normal policy, executor, and probes
    final_task = run_task(new_task.id, db_path=db_path)

    # Check verification
    summary = Ledger.get_task_summary(new_task.id, db_path=db_path)
    is_verified = bool(summary.get("is_complete"))
    last_status = final_task.status
    now = utc_now_iso()

    # Update skill stats
    with get_db(db_path) as conn:
        conn.execute(
            """
            UPDATE skills
            SET runs = runs + 1,
                verified_runs = verified_runs + ?,
                last_run_at = ?,
                last_status = ?
            WHERE id = ? AND owner_id = ?
            """,
            (1 if is_verified else 0, now, last_status, skill_id, owner_id),
        )

    updated_skill = get_skill(skill_id, owner_id=owner_id, db_path=db_path)
    return {
        "task": final_task.to_dict(),
        "skill": updated_skill.to_dict() if updated_skill else skill.to_dict(),
    }
