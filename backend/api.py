"""Saha FastAPI Backend Server.

Provides endpoints for private spaces, task creation, execution, drift reconciliation,
approvals, policies, privacy ledger egress, and sabotage demo.
"""

from contextlib import asynccontextmanager
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from backend.db import (
    check_and_init_data_dirs,
    delete_owner_space,
    get_data_dir_status,
    get_db,
    get_db_path,
    get_llm_metrics,
    get_owner_egress_logs,
    hash_owner_token,
)
from backend.executor import is_public_mode, run_task
from backend.ledger import Ledger
from backend.planner import generate_plan
from backend.policy import (
    PUBLIC_ALLOWED_TOOLS,
    Policy,
    approve_request,
    create_approval,
    deny_request,
    evaluate_step_policy,
    get_owner_approvals,
    get_owner_policy,
    set_owner_policy,
)
from backend.resume import resume_task
from backend.tools.sandbox import delete_owner_workspace, resolve_workspace_path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"

logger = logging.getLogger("saha.api")

# Verify database and workspace directory writability or fallback at initialization
check_and_init_data_dirs()

# Track startup recovery execution and health visibility
STARTUP_RECOVERY_STATUS: Dict[str, Any] = {
    "lifespan_started": False,
    "completed": False,
    "error": None,
    "recovered_tasks": [],
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Production lifespan hook: executes startup recovery exactly once on real server boot."""
    STARTUP_RECOVERY_STATUS["lifespan_started"] = True
    STARTUP_RECOVERY_STATUS["completed"] = False
    STARTUP_RECOVERY_STATUS["error"] = None

    # Verify storage directory writability or fallback
    try:
        check_and_init_data_dirs()
    except Exception as err:
        logger.error("Startup database/workspace initialization failed: %s", err, exc_info=True)
        STARTUP_RECOVERY_STATUS["error"] = f"Storage initialization failed: {err}"
        raise

    # Execute stale running task recovery exactly once
    try:
        recovered = Ledger.recover_stale_running_tasks()
        STARTUP_RECOVERY_STATUS["completed"] = True
        STARTUP_RECOVERY_STATUS["error"] = None
        STARTUP_RECOVERY_STATUS["recovered_tasks"] = recovered
        if recovered:
            logger.info("Startup recovery recovered %d stale running tasks: %s", len(recovered), recovered)
    except Exception as err:
        error_msg = f"Startup recovery failed: {err}"
        logger.error(error_msg, exc_info=True)
        STARTUP_RECOVERY_STATUS["completed"] = False
        STARTUP_RECOVERY_STATUS["error"] = str(err)
        raise RuntimeError(error_msg) from err

    yield


app = FastAPI(
    title="Saha API",
    description="Persistent, private, drift-resilient personal AI ledger server.",
    version="1.1.0",
    docs_url=None if is_public_mode() else "/docs",
    redoc_url=None if is_public_mode() else "/redoc",
    openapi_url=None if is_public_mode() else "/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
)


@app.middleware("http")
async def public_mode_security_middleware(request: Request, call_next):
    """Enforce token authentication and endpoint restrictions in public mode."""
    path = request.url.path
    method = request.method.upper()

    if is_public_mode():
        # 1. Disable /docs, /redoc, /openapi.json in public mode
        if path in ("/docs", "/redoc", "/openapi.json"):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})

        # 2. Allow GET /health
        if method == "GET" and path == "/health":
            return await call_next(request)

        # 3. Allow static UI files and pairing page load
        if method == "GET" and (
            path in ("/", "/index.html", "/favicon.ico")
            or path.startswith("/assets/")
            or path.startswith("/static/")
        ):
            return await call_next(request)

        # 4. Allow CORS preflight OPTIONS requests
        if method == "OPTIONS":
            return await call_next(request)

        # 5. Require valid X-Saha-Owner header (minimum 32 characters)
        token = request.headers.get("X-Saha-Owner", "").strip()
        if not token or len(token) < 32:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "Unauthorized: Valid X-Saha-Owner header (minimum 32 characters) required in public mode."
                },
            )

    return await call_next(request)



# -------------------------------------------------------------------------
# MEMORY & SAVED ROUTINES (Phase 6-Lite)
# -------------------------------------------------------------------------


class CreateMemoryRequest(BaseModel):
    kind: str = Field(..., description="fact, preference, or note")
    text: str = Field(..., max_length=300)


class SaveSkillRequest(BaseModel):
    name: str = Field(..., max_length=60)
    description: Optional[str] = Field(default="", max_length=200)


@app.get("/memories")
def list_memories_endpoint(request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import get_active_memories
    mems = get_active_memories(owner_id)
    return {"memories": [m.to_dict() for m in mems], "count": len(mems)}


@app.post("/memories")
def create_memory_endpoint(payload: CreateMemoryRequest, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import create_memory
    try:
        mem = create_memory(
            owner_id=owner_id,
            kind=payload.kind,
            text=payload.text,
            source="user",
            status="active",
        )
        return {"memory": mem.to_dict(), "status": "created"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/memories/{memory_id}")
def delete_memory_endpoint(memory_id: str, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import delete_memory, get_memory
    existing = get_memory(memory_id, owner_id=owner_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Memory not found")
    deleted = delete_memory(memory_id, owner_id=owner_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "deleted", "id": memory_id}


@app.get("/memories/proposals")
def list_memory_proposals_endpoint(request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import get_proposed_memories
    proposals = get_proposed_memories(owner_id)
    return {"proposals": [p.to_dict() for p in proposals], "count": len(proposals)}


@app.post("/memories/proposals/{memory_id}/approve")
def approve_memory_proposal_endpoint(memory_id: str, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import approve_memory_proposal
    try:
        mem = approve_memory_proposal(memory_id, owner_id=owner_id)
        return {"memory": mem.to_dict(), "status": "approved"}
    except KeyError:
        raise HTTPException(status_code=404, detail="Memory proposal not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/memories/proposals/{memory_id}/reject")
def reject_memory_proposal_endpoint(memory_id: str, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import reject_memory_proposal
    try:
        rejected = reject_memory_proposal(memory_id, owner_id=owner_id)
        if not rejected:
            raise HTTPException(status_code=404, detail="Memory proposal not found")
        return {"status": "rejected", "id": memory_id}
    except KeyError:
        raise HTTPException(status_code=404, detail="Memory proposal not found")


@app.post("/tasks/{task_id}/save-as-skill")
def save_task_as_skill_endpoint(task_id: str, payload: SaveSkillRequest, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.skills import create_skill_from_task
    try:
        skill = create_skill_from_task(
            task_id=task_id,
            owner_id=owner_id,
            name=payload.name,
            description=payload.description or "",
        )
        return {"skill": skill.to_dict(), "status": "saved"}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/skills")
def list_skills_endpoint(request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.skills import get_skills
    skills_list = get_skills(owner_id)
    return {"skills": [s.to_dict() for s in skills_list], "count": len(skills_list)}


@app.delete("/skills/{skill_id}")
def delete_skill_endpoint(skill_id: str, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.skills import delete_skill, get_skill
    existing = get_skill(skill_id, owner_id=owner_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Skill not found")
    deleted = delete_skill(skill_id, owner_id=owner_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"status": "deleted", "id": skill_id}


@app.post("/skills/{skill_id}/run")
def run_skill_endpoint(skill_id: str, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.skills import run_skill
    try:
        result = run_skill(skill_id, owner_id=owner_id)
        return result
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """Ensure no absolute paths or raw stack traces leak to callers."""
    logging.exception(f"Unhandled server error: {exc}")
    if is_public_mode():
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal server error occurred. Details suppressed in public mode."}
        )
    msg = str(exc)
    msg = re.sub(r'[A-Za-z]:\\[\w\s\\.-]+', '[PATH]', msg)
    msg = re.sub(r'/[\w\s/.-]+', '[PATH]', msg)
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {msg}"}
    )

# In-memory rate limiters for POST /tasks in public mode
IP_POST_HISTORY = defaultdict(list)
OWNER_POST_HISTORY = defaultdict(list)
RATE_LIMIT_MAX_PER_HOUR = 10
RATE_LIMIT_WINDOW_SECONDS = 3600


def is_demo_mode() -> bool:
    return os.getenv("SAHA_DEMO_MODE", "true").lower() in ("true", "1", "yes")


def get_owner_info(request: Request) -> Tuple[str, str]:
    """Extract owner token and hashed owner_id from request header X-Saha-Owner."""
    raw_token = request.headers.get("X-Saha-Owner", "").strip()
    if is_public_mode():
        if not raw_token or len(raw_token) < 32:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized: Valid X-Saha-Owner header (minimum 32 characters) required in public mode.",
            )
    else:
        if not raw_token:
            raw_token = "default-anonymous-owner"
    owner_id = hash_owner_token(raw_token)
    return raw_token, owner_id


def check_rate_limits(client_ip: str, owner_id: str) -> None:
    """Enforce in-memory token bucket per-IP and per-owner in public mode."""
    if not is_public_mode():
        return
    now = time.time()

    # Check IP
    ip_hist = [t for t in IP_POST_HISTORY[client_ip] if now - t < RATE_LIMIT_WINDOW_SECONDS]
    IP_POST_HISTORY[client_ip] = ip_hist
    if len(ip_hist) >= RATE_LIMIT_MAX_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=f"Too Many Requests: IP rate limit exceeded ({RATE_LIMIT_MAX_PER_HOUR} task creations per hour).",
        )

    # Check Owner
    owner_hist = [t for t in OWNER_POST_HISTORY[owner_id] if now - t < RATE_LIMIT_WINDOW_SECONDS]
    OWNER_POST_HISTORY[owner_id] = owner_hist
    if len(owner_hist) >= RATE_LIMIT_MAX_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=f"Too Many Requests: Owner rate limit exceeded ({RATE_LIMIT_MAX_PER_HOUR} task creations per hour).",
        )

    IP_POST_HISTORY[client_ip].append(now)
    OWNER_POST_HISTORY[owner_id].append(now)


class CreateTaskRequest(BaseModel):
    goal: str = Field(..., min_length=1, description="Goal to execute")


class ApproveActionRequest(BaseModel):
    always_allow: bool = Field(default=False, description="Whether to update owner policy with this permission.")


@app.get("/health")
def health() -> Any:
    data_status = get_data_dir_status()

    # Prevent reporting healthy if recovery failed or if lifespan was started but recovery did not complete
    if STARTUP_RECOVERY_STATUS.get("error") is not None or (
        STARTUP_RECOVERY_STATUS.get("lifespan_started") and not STARTUP_RECOVERY_STATUS.get("completed")
    ):
        content = {
            "status": "unhealthy",
            "service": "Saha",
            "recovery_ok": False,
            "public_mode": is_public_mode(),
            "demo_mode": is_demo_mode(),
            "data_dir_ok": data_status["data_dir_ok"],
            "data_persistent": data_status["data_persistent"],
        }
        if not is_public_mode():
            content["recovery_error"] = (
                STARTUP_RECOVERY_STATUS.get("error") or "Startup recovery did not complete"
            )
            content["data_dir"] = data_status["effective_data_dir"]
        return JSONResponse(status_code=503, content=content)

    resp = {
        "status": "ok",
        "service": "Saha",
        "public_mode": is_public_mode(),
        "demo_mode": is_demo_mode(),
        "data_dir_ok": data_status["data_dir_ok"],
        "data_persistent": data_status["data_persistent"],
    }
    if not is_public_mode():
        resp["data_dir"] = data_status["effective_data_dir"]
    return resp


@app.get("/metrics")
def metrics(request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    return get_llm_metrics(owner_id=owner_id if is_public_mode() else None)


# -------------------------------------------------------------------------
# PRIVATE SPACES & OWNER MANAGEMENT
# -------------------------------------------------------------------------

@app.post("/owner/pair-link")
def pair_link(request: Request) -> Dict[str, Any]:
    """Return a pairing link with the owner's token in the URL fragment."""
    raw_token = request.headers.get("X-Saha-Owner", "").strip()
    if not raw_token:
        raw_token = secrets.token_hex(16)
    owner_id = hash_owner_token(raw_token)
    base_url = str(request.base_url).rstrip("/")
    pair_url = f"{base_url}/#pair={raw_token}"
    return {
        "pair_link": pair_url,
        "pair_url": pair_url,
        "token": raw_token,
        "token_preview": f"{raw_token[:8]}...",
        "owner_id": owner_id,
        "note": "Bearer-token pairing: anyone with this link gains full access to this private space. Not full authentication.",
    }


@app.delete("/owner/space")
def delete_space(request: Request) -> Dict[str, Any]:
    """Delete ALL tasks, steps, files, policies, approvals, and egress logs for this owner."""
    _, owner_id = get_owner_info(request)
    db_res = delete_owner_space(owner_id)
    delete_owner_workspace(owner_id)
    return {
        "status": "deleted",
        "owner_id": owner_id,
        "deleted_tasks": db_res["deleted_tasks"],
        "message": "All workspace files, tasks, approvals, policies, and privacy ledger records deleted.",
    }


# -------------------------------------------------------------------------
# POLICY & APPROVALS
# -------------------------------------------------------------------------

@app.get("/policy")
def get_policy(request: Request) -> Dict[str, Any]:
    """Return the owner's current execution policy."""
    _, owner_id = get_owner_info(request)
    policy = get_owner_policy(owner_id)
    return {"owner_id": owner_id, "policy": policy.model_dump()}


@app.put("/policy")
def update_policy(policy_data: Policy, request: Request) -> Dict[str, Any]:
    """Update the owner's execution policy."""
    _, owner_id = get_owner_info(request)
    if is_public_mode():
        unpermitted = [t for t in policy_data.allowed_tools if t not in PUBLIC_ALLOWED_TOOLS]
        if unpermitted:
            raise HTTPException(
                status_code=400,
                detail=f"In public mode, tools outside {sorted(PUBLIC_ALLOWED_TOOLS)} cannot be permitted: {unpermitted}",
            )
    try:
        saved = set_owner_policy(owner_id, policy_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "updated", "owner_id": owner_id, "policy": saved.model_dump()}


@app.get("/approvals")
def list_approvals(request: Request, status: Optional[str] = None) -> Dict[str, Any]:
    """List pending or resolved approvals for the current owner."""
    _, owner_id = get_owner_info(request)
    approvals = get_owner_approvals(owner_id, status=status)
    return {"owner_id": owner_id, "approvals": approvals}


@app.post("/approvals/{approval_id}/approve")
def approve_step(approval_id: str, request: Request, body: Optional[ApproveActionRequest] = None) -> Dict[str, Any]:
    """Approve a blocked step, re-queue to pending, resume execution in background, and optionally update policy."""
    _, owner_id = get_owner_info(request)
    always_allow = body.always_allow if body else False
    if is_public_mode():
        with get_db() as conn:
            row = conn.execute("SELECT tool FROM approvals WHERE id = ? AND owner_id = ?", (approval_id, owner_id)).fetchone()
            if row and row["tool"] not in PUBLIC_ALLOWED_TOOLS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Cannot approve tool '{row['tool']}': tool is strictly forbidden in public mode.",
                )
    try:
        res = approve_request(approval_id, owner_id=owner_id, always_allow=always_allow)
    except ValueError as e:
        err_msg = str(e)
        if "forbidden" in err_msg.lower() or "cannot be approved" in err_msg.lower():
            raise HTTPException(status_code=400, detail=err_msg)
        raise HTTPException(status_code=404, detail="Approval not found")

    task_id = res.get("task_id")
    if task_id:
        task = Ledger.get_task(task_id, owner_id=owner_id)
        if task:
            # Guard against double-start
            if task.status != "running":
                Ledger.update_task_status(task_id, "running")
                db_path = str(get_db_path())
                threading.Thread(target=run_task, args=(task_id, db_path), daemon=True).start()
                res["task_status"] = "running"
            else:
                res["task_status"] = "already_running"

    return res


@app.post("/approvals/{approval_id}/deny")
def deny_step(approval_id: str, request: Request) -> Dict[str, Any]:
    """Deny a blocked step and mark it and the task failed."""
    _, owner_id = get_owner_info(request)
    try:
        res = deny_request(approval_id, owner_id=owner_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Approval not found")

    task_id = res.get("task_id")
    if task_id:
        Ledger.update_task_status(task_id, "failed")
        res["task_status"] = "failed"

    return res


# -------------------------------------------------------------------------
# PRIVACY LEDGER (EGRESS)
# -------------------------------------------------------------------------

@app.get("/egress")
def get_egress_logs(request: Request, limit: int = 50) -> Dict[str, Any]:
    """Return the owner's privacy ledger egress logs (character and redaction counts, NO content)."""
    _, owner_id = get_owner_info(request)
    logs = get_owner_egress_logs(owner_id, limit=limit)
    return {"owner_id": owner_id, "egress": logs, "egress_logs": logs}


# -------------------------------------------------------------------------
# TASKS & LEDGER
# -------------------------------------------------------------------------

@app.post("/tasks")
def create_task(req: CreateTaskRequest, request: Request) -> Dict[str, Any]:
    client_ip = request.client.host if request.client else "127.0.0.1"
    _, owner_id = get_owner_info(request)
    check_rate_limits(client_ip, owner_id)

    goal = req.goal.strip()
    if is_public_mode() and len(goal) > 300:
        raise HTTPException(
            status_code=422,
            detail="Task goal exceeds maximum allowed length of 300 characters in public mode.",
        )

    task = Ledger.create_task(goal, owner_id=owner_id)

    try:
        step_plans = generate_plan(goal, owner_id=owner_id)
    except Exception as e:
        Ledger.update_task_status(task.id, "failed")
        msg = "Planning failed. Please try again." if is_public_mode() else f"Planning failed: {e}"
        raise HTTPException(status_code=500, detail=msg)

    # Policy enforcement on candidate steps
    owner_policy = get_owner_policy(owner_id)
    if is_public_mode() and len(step_plans) > owner_policy.max_steps:
        Ledger.update_task_status(task.id, "failed")
        raise HTTPException(
            status_code=422,
            detail=f"Task plan has {len(step_plans)} steps, which exceeds maximum allowed steps of {owner_policy.max_steps} in public mode.",
        )

    steps_data = []
    has_blocked_steps = False

    for sp in step_plans:
        d_on = list(getattr(sp, "depends_on", []))
        if not d_on and ("combine" in sp.intent.lower() or "summary" in sp.intent.lower()):
            d_on = [1, 2]

        # Evaluate candidate step against owner policy
        allowed, block_reason = evaluate_step_policy(
            tool=sp.tool,
            args=sp.args,
            policy=owner_policy,
            current_step_index=sp.idx,
        )

        step_dict = {
            "intent": sp.intent,
            "tool": sp.tool,
            "args": sp.args,
            "probe": sp.probe,
            "depends_on": d_on,
            "status": "pending" if allowed else "blocked",
            "block_reason": block_reason if not allowed else None,
            "error": block_reason if not allowed else None,
        }
        steps_data.append(step_dict)
        if not allowed:
            has_blocked_steps = True

    steps = Ledger.add_steps(task.id, steps_data)

    # For any blocked step, create an approval row immediately
    if has_blocked_steps:
        for s in steps:
            if s.status == "blocked":
                create_approval(
                    owner_id=owner_id,
                    task_id=task.id,
                    step_id=s.id,
                    tool=s.tool,
                    args=s.to_dict().get("args", {}),
                    reason=s.error or "Blocked by owner policy",
                )
        Ledger.update_task_status(task.id, "paused")

    summary = Ledger.get_task_summary(task.id, owner_id=owner_id)

    return {
        "task": summary["task"],
        "steps": summary["steps"],
    }


@app.get("/tasks")
def list_tasks(request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    tasks = Ledger.list_tasks(owner_id=owner_id, limit=50)
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/tasks/{task_id}")
def get_task_detail(task_id: str, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    task = Ledger.get_task(task_id, owner_id=owner_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    summary = Ledger.get_task_summary(task_id, owner_id=owner_id)
    return summary


@app.post("/tasks/{task_id}/run")
def run_task_endpoint(task_id: str, request: Request, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    task = Ledger.get_task(task_id, owner_id=owner_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    db_path = str(get_db_path())
    Ledger.update_task_status(task_id, "running")
    threading.Thread(target=run_task, args=(task_id, db_path), daemon=True).start()
    return {"status": "running", "task_id": task_id}


@app.post("/tasks/{task_id}/resume")
def resume_task_endpoint(task_id: str, request: Request, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    task = Ledger.get_task(task_id, owner_id=owner_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    db_path = str(get_db_path())
    Ledger.update_task_status(task_id, "running")
    threading.Thread(target=resume_task, args=(task_id, db_path), daemon=True).start()
    return {"status": "resuming", "task_id": task_id}


@app.post("/tasks/{task_id}/sabotage")
def sabotage_task_endpoint(task_id: str, request: Request) -> Dict[str, Any]:
    if not is_demo_mode():
        raise HTTPException(status_code=403, detail="Sabotage endpoint is disabled when SAHA_DEMO_MODE is false.")

    _, owner_id = get_owner_info(request)
    task = Ledger.get_task(task_id, owner_id=owner_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    summary = Ledger.get_task_summary(task_id, owner_id=owner_id)
    target_rel = None
    for s in summary["steps"]:
        if s["tool"] == "write_file":
            target_rel = s["args"].get("path")
            break

    if not target_rel:
        target_rel = "facts1.txt"

    target_disk_path = resolve_workspace_path(target_rel, task_id=task_id, owner_id=owner_id)
    existed = target_disk_path.exists()
    if existed:
        target_disk_path.unlink()

    return {
        "status": "sabotaged",
        "task_id": task_id,
        "deleted_file": target_rel,
        "message": f"Successfully deleted '{target_rel}' from task workspace without modifying ledger.",
    }



# -------------------------------------------------------------------------
# MEMORY & SAVED ROUTINES (Phase 6-Lite)
# -------------------------------------------------------------------------


class CreateMemoryRequest(BaseModel):
    kind: str = Field(..., description="fact, preference, or note")
    text: str = Field(..., max_length=300)


class SaveSkillRequest(BaseModel):
    name: str = Field(..., max_length=60)
    description: Optional[str] = Field(default="", max_length=200)


@app.get("/memories")
def list_memories_endpoint(request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import get_active_memories
    mems = get_active_memories(owner_id)
    return {"memories": [m.to_dict() for m in mems], "count": len(mems)}


@app.post("/memories")
def create_memory_endpoint(payload: CreateMemoryRequest, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import create_memory
    try:
        mem = create_memory(
            owner_id=owner_id,
            kind=payload.kind,
            text=payload.text,
            source="user",
            status="active",
        )
        return {"memory": mem.to_dict(), "status": "created"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/memories/{memory_id}")
def delete_memory_endpoint(memory_id: str, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import delete_memory, get_memory
    existing = get_memory(memory_id, owner_id=owner_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Memory not found")
    deleted = delete_memory(memory_id, owner_id=owner_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory not found")
    return {"status": "deleted", "id": memory_id}


@app.get("/memories/proposals")
def list_memory_proposals_endpoint(request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import get_proposed_memories
    proposals = get_proposed_memories(owner_id)
    return {"proposals": [p.to_dict() for p in proposals], "count": len(proposals)}


@app.post("/memories/proposals/{memory_id}/approve")
def approve_memory_proposal_endpoint(memory_id: str, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import approve_memory_proposal
    try:
        mem = approve_memory_proposal(memory_id, owner_id=owner_id)
        return {"memory": mem.to_dict(), "status": "approved"}
    except KeyError:
        raise HTTPException(status_code=404, detail="Memory proposal not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/memories/proposals/{memory_id}/reject")
def reject_memory_proposal_endpoint(memory_id: str, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.memory import reject_memory_proposal
    try:
        rejected = reject_memory_proposal(memory_id, owner_id=owner_id)
        if not rejected:
            raise HTTPException(status_code=404, detail="Memory proposal not found")
        return {"status": "rejected", "id": memory_id}
    except KeyError:
        raise HTTPException(status_code=404, detail="Memory proposal not found")


@app.post("/tasks/{task_id}/save-as-skill")
def save_task_as_skill_endpoint(task_id: str, payload: SaveSkillRequest, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.skills import create_skill_from_task
    try:
        skill = create_skill_from_task(
            task_id=task_id,
            owner_id=owner_id,
            name=payload.name,
            description=payload.description or "",
        )
        return {"skill": skill.to_dict(), "status": "saved"}
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/skills")
def list_skills_endpoint(request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.skills import get_skills
    skills_list = get_skills(owner_id)
    return {"skills": [s.to_dict() for s in skills_list], "count": len(skills_list)}


@app.delete("/skills/{skill_id}")
def delete_skill_endpoint(skill_id: str, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.skills import delete_skill, get_skill
    existing = get_skill(skill_id, owner_id=owner_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Skill not found")
    deleted = delete_skill(skill_id, owner_id=owner_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Skill not found")
    return {"status": "deleted", "id": skill_id}


@app.post("/skills/{skill_id}/run")
def run_skill_endpoint(skill_id: str, request: Request) -> Dict[str, Any]:
    _, owner_id = get_owner_info(request)
    from backend.skills import run_skill
    try:
        result = run_skill(skill_id, owner_id=owner_id)
        return result
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if is_public_mode():
        return JSONResponse(
            status_code=500,
            content={"detail": "An internal error occurred. Please try again."},
        )
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.get("/")
def serve_index() -> FileResponse:
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Web UI not found")
    return FileResponse(index_file)
