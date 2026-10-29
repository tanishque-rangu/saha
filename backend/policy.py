"""Saha Policy & Approvals Engine.

Enforces per-owner security boundaries in pure deterministic code (never LLM):
- allowed_tools (list of permitted tool names)
- allowed_domains (list of permitted HTTP domains)
- allowed_paths (list of permitted paths relative to workspace/<owner>/<task>)
- auto_heal (boolean flag for automated drift remediation)
- max_steps (maximum steps allowed per task plan)

Any violation halts step execution, transitions step to `blocked`, and logs an approval request.
"""

import json
import os
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from backend.db import get_db, init_db, utc_now_iso


PUBLIC_ALLOWED_TOOLS = frozenset({"write_file", "read_file", "http_get", "http_request", "list_files"})


def is_public_mode() -> bool:
    return os.getenv("SAHA_PUBLIC_MODE", "false").lower() in ("true", "1", "yes")


class Policy(BaseModel):
    allowed_tools: List[str] = Field(
        default_factory=lambda: ["write_file", "read_file", "http_get", "list_files"]
        if is_public_mode()
        else ["write_file", "read_file", "run_shell", "http_get", "http_request", "list_files"],
        description="List of permitted tool names for execution.",
    )
    allowed_domains: List[str] = Field(
        default_factory=lambda: ["example.com", "wikipedia.org", "httpbin.org"],
        description="List of permitted external domains for http_get.",
    )
    allowed_paths: List[str] = Field(
        default_factory=lambda: ["*"],
        description="Permitted path prefixes/patterns relative to task workspace.",
    )
    auto_heal: bool = Field(
        default=True,
        description="Whether autonomous drift repair is permitted.",
    )
    max_steps: int = Field(
        default=8 if is_public_mode() else 10,
        description="Maximum number of steps permitted in a single task DAG.",
    )
    trusted_idempotency_domains: List[str] = Field(
        default_factory=list,
        description="External domains trusted by the owner to support safe idempotency key deduplication.",
    )
    idempotency_domains: Optional[List[str]] = Field(
        default=None,
        description="Optional alias for trusted_idempotency_domains.",
    )


def get_default_policy() -> Policy:
    """Return default policy with sensible public-mode defaults."""
    if is_public_mode():
        return Policy(
            allowed_tools=["write_file", "read_file", "http_get", "list_files"],
            allowed_domains=["example.com", "wikipedia.org", "httpbin.org"],
            allowed_paths=["*"],
            auto_heal=True,
            max_steps=8,
            trusted_idempotency_domains=[],
        )
    return Policy(
        allowed_tools=["write_file", "read_file", "run_shell", "http_get", "http_request", "list_files"],
        allowed_domains=["example.com", "wikipedia.org", "httpbin.org"],
        allowed_paths=["*"],
        auto_heal=True,
        max_steps=10,
        trusted_idempotency_domains=[],
    )


def get_owner_policy(owner_id: str, db_path: Optional[str] = None) -> Policy:
    """Retrieve owner policy from database, or initialize with defaults."""
    init_db(db_path)
    with get_db(db_path) as conn:
        row = conn.execute("SELECT policy_json FROM policies WHERE owner_id = ?", (owner_id,)).fetchone()
        if row and row["policy_json"]:
            try:
                data = json.loads(row["policy_json"])
                return Policy(**data)
            except Exception:
                pass

        default_policy = get_default_policy()
        conn.execute(
            """
            INSERT OR REPLACE INTO policies (owner_id, policy_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (owner_id, default_policy.model_dump_json(), utc_now_iso()),
        )
        return default_policy


def set_owner_policy(owner_id: str, policy: Policy, db_path: Optional[str] = None) -> Policy:
    """Save or update an owner's policy in SQLite."""
    if is_public_mode():
        unpermitted = [t for t in policy.allowed_tools if t not in PUBLIC_ALLOWED_TOOLS]
        if unpermitted:
            raise ValueError(
                f"In public mode, tools outside {sorted(PUBLIC_ALLOWED_TOOLS)} cannot be permitted: {unpermitted}"
            )
    init_db(db_path)
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO policies (owner_id, policy_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (owner_id, policy.model_dump_json(), utc_now_iso()),
        )
    return policy


def evaluate_plan_size(step_count: int, policy: Policy) -> Tuple[bool, Optional[str]]:
    """Evaluate whether a newly proposed plan or replan size conforms to policy max_steps."""
    if step_count > policy.max_steps:
        return False, f"Proposed plan size ({step_count} steps) exceeds policy max_steps ({policy.max_steps})."
    return True, None


def evaluate_step_policy(
    tool: str,
    args: Dict[str, Any],
    policy: Policy,
    current_step_index: int = 1,
) -> Tuple[bool, Optional[str]]:
    """Evaluate a step candidate against policy.
    
    Returns (True, None) if allowed, or (False, reason) if blocked.
    Deterministic code enforcement only.
    """
    # 0. Public mode hard limit: only PUBLIC_ALLOWED_TOOLS reachable
    if is_public_mode() and tool not in PUBLIC_ALLOWED_TOOLS:
        return False, f"Tool '{tool}' is strictly forbidden in public mode. Allowed tools: {sorted(PUBLIC_ALLOWED_TOOLS)}."

    # 1. Tool permission check
    if tool not in policy.allowed_tools:
        return False, f"Tool '{tool}' is not permitted by owner policy. Allowed tools: {policy.allowed_tools}."

    # 3. HTTP domain permission check
    if tool in ("http_get", "http_request", "http_post", "http_mutation"):
        url = args.get("url", "")
        if not url:
            return False, f"Missing 'url' argument for {tool}."
        try:
            parsed = urllib.parse.urlparse(url)
            host = (parsed.hostname or "").lower()
            if not host:
                return False, f"Invalid URL hostname: {url}."

            domain_allowed = False
            for allowed in policy.allowed_domains:
                allowed_clean = allowed.lower().strip()
                if host == allowed_clean or host.endswith("." + allowed_clean):
                    domain_allowed = True
                    break

            if not domain_allowed:
                return False, f"Domain '{host}' is not in policy allowed_domains: {policy.allowed_domains}."
        except Exception as e:
            return False, f"Failed to parse URL for policy check: {e}."

    # 4. File path permission check
    if tool in ("write_file", "read_file"):
        path = args.get("path", "")
        if not path:
            return False, f"Missing 'path' argument for {tool}."
        # If wildcard not present, check explicit allowed paths
        if "*" not in policy.allowed_paths:
            path_clean = path.replace("\\", "/").strip("/")
            if not any(path_clean == p.strip("/") or path_clean.startswith(p.strip("/") + "/") for p in policy.allowed_paths):
                return False, f"Path '{path}' is not in policy allowed_paths: {policy.allowed_paths}."

    return True, None


def create_approval(
    owner_id: str,
    task_id: str,
    step_id: str,
    tool: str,
    args: Dict[str, Any],
    reason: str,
    db_path: Optional[str] = None,
) -> str:
    """Create a pending approval row in SQLite."""
    init_db(db_path)
    approval_id = str(uuid.uuid4())
    now = utc_now_iso()
    args_json = json.dumps(args)
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO approvals (id, owner_id, task_id, step_id, tool, args_json, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (approval_id, owner_id, task_id, step_id, tool, args_json, reason, now),
        )
    return approval_id


def get_owner_approvals(owner_id: str, status: Optional[str] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return approvals for an owner, optionally filtered by status."""
    init_db(db_path)
    with get_db(db_path) as conn:
        query = "SELECT * FROM approvals WHERE owner_id = ?"
        params: List[Any] = [owner_id]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": r["id"],
                "owner_id": r["owner_id"],
                "task_id": r["task_id"],
                "step_id": r["step_id"],
                "tool": r["tool"],
                "args": json.loads(r["args_json"]) if r["args_json"] else {},
                "reason": r["reason"],
                "status": r["status"],
                "created_at": r["created_at"],
                "resolved_at": r["resolved_at"],
            }
            for r in rows
        ]


def approve_request(
    approval_id: str,
    owner_id: str,
    always_allow: bool = False,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Approve a blocked step request, re-queue the step to 'pending', and optionally update policy."""
    init_db(db_path)
    now = utc_now_iso()
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM approvals WHERE id = ? AND owner_id = ?",
            (approval_id, owner_id),
        ).fetchone()
        if not row:
            raise ValueError("Approval not found")

        step_id = row["step_id"]
        tool = row["tool"]
        if is_public_mode() and tool not in PUBLIC_ALLOWED_TOOLS:
            raise ValueError(f"Cannot approve tool '{tool}': strictly forbidden in public mode.")
        args = json.loads(row["args_json"]) if row["args_json"] else {}

        # 1. Update approval status
        conn.execute(
            "UPDATE approvals SET status = 'approved', resolved_at = ? WHERE id = ?",
            (now, approval_id),
        )

        # 2. Re-queue step to pending and clear block reason
        conn.execute(
            "UPDATE steps SET status = 'pending', block_reason = NULL, error = NULL WHERE id = ?",
            (step_id,),
        )

    # 3. Always Allow: update owner's policy
    if always_allow:
        policy = get_owner_policy(owner_id, db_path=db_path)
        if tool not in policy.allowed_tools:
            policy.allowed_tools.append(tool)
        if tool in ("http_get", "http_request", "http_post", "http_mutation") and args.get("url"):
            try:
                host = urllib.parse.urlparse(args["url"]).hostname
                if host and host not in policy.allowed_domains:
                    policy.allowed_domains.append(host)
            except Exception:
                pass
        set_owner_policy(owner_id, policy, db_path=db_path)

    return {
        "status": "approved",
        "approval_id": approval_id,
        "step_id": step_id,
        "task_id": row["task_id"],
        "always_allow": always_allow,
    }


def deny_request(
    approval_id: str,
    owner_id: str,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Deny a blocked step request and mark the step failed."""
    init_db(db_path)
    now = utc_now_iso()
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM approvals WHERE id = ? AND owner_id = ?",
            (approval_id, owner_id),
        ).fetchone()
        if not row:
            raise ValueError("Approval not found")

        step_id = row["step_id"]

        # 1. Update approval status
        conn.execute(
            "UPDATE approvals SET status = 'denied', resolved_at = ? WHERE id = ?",
            (now, approval_id),
        )

        # 2. Mark step and task failed
        conn.execute(
            "UPDATE steps SET status = 'failed', error = 'Denied by owner policy approval' WHERE id = ?",
            (step_id,),
        )
        conn.execute(
            "UPDATE tasks SET status = 'failed', updated_at = ? WHERE id = ?",
            (now, row["task_id"]),
        )

    return {
        "status": "denied",
        "approval_id": approval_id,
        "step_id": step_id,
        "task_id": row["task_id"],
    }
