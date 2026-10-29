"""Saha FastAPI Backend Server.

Provides endpoints for task creation, execution, drift reconciliation,
telemetry metrics, health checks, and demo sabotage simulation.
"""

import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from backend.db import get_llm_metrics
from backend.executor import is_public_mode, run_task
from backend.ledger import Ledger
from backend.planner import generate_plan
from backend.resume import resume_task
from backend.tools.sandbox import get_workspace_dir, resolve_workspace_path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = PROJECT_ROOT / "web"

app = FastAPI(
    title="Saha API",
    description="Persistent, drift-resilient personal AI ledger server.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory IP rate limiter for POST endpoints in public mode
IP_POST_HISTORY = defaultdict(list)
RATE_LIMIT_MAX_PER_HOUR = 10
RATE_LIMIT_WINDOW_SECONDS = 3600


def is_demo_mode() -> bool:
    return os.getenv("SAHA_DEMO_MODE", "true").lower() in ("true", "1", "yes")


def check_ip_rate_limit(client_ip: str) -> None:
    """Enforce in-memory token bucket: 10 task creations per hour per IP in public mode."""
    if not is_public_mode():
        return
    now = time.time()
    history = IP_POST_HISTORY[client_ip]
    valid_history = [t for t in history if now - t < RATE_LIMIT_WINDOW_SECONDS]
    IP_POST_HISTORY[client_ip] = valid_history
    if len(valid_history) >= RATE_LIMIT_MAX_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=f"Too Many Requests: Rate limit exceeded ({RATE_LIMIT_MAX_PER_HOUR} task creations per hour).",
        )
    IP_POST_HISTORY[client_ip].append(now)


class CreateTaskRequest(BaseModel):
    goal: str = Field(..., min_length=1, description="Goal to execute")


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "service": "Saha",
        "public_mode": is_public_mode(),
        "demo_mode": is_demo_mode(),
    }


@app.get("/metrics")
def metrics() -> Dict[str, Any]:
    return get_llm_metrics()


@app.post("/tasks")
def create_task(req: CreateTaskRequest, request: Request) -> Dict[str, Any]:
    client_ip = request.client.host if request.client else "127.0.0.1"
    check_ip_rate_limit(client_ip)

    goal = req.goal.strip()
    if is_public_mode() and len(goal) > 300:
        raise HTTPException(
            status_code=422,
            detail="Task goal exceeds maximum allowed length of 300 characters in public mode.",
        )

    task = Ledger.create_task(goal)

    try:
        step_plans = generate_plan(goal)
    except Exception as e:
        Ledger.update_task_status(task.id, "failed")
        raise HTTPException(status_code=500, detail=f"Planning failed: {e}")

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

    steps = Ledger.add_steps(task.id, steps_data)
    summary = Ledger.get_task_summary(task.id)

    return {
        "task": task.to_dict(),
        "steps": summary["steps"],
    }


@app.get("/tasks")
def list_tasks() -> Dict[str, Any]:
    tasks = Ledger.list_tasks(limit=50)
    return {"tasks": [t.to_dict() for t in tasks]}


@app.get("/tasks/{task_id}")
def get_task_detail(task_id: str) -> Dict[str, Any]:
    task = Ledger.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    summary = Ledger.get_task_summary(task_id)
    return summary


@app.post("/tasks/{task_id}/run")
def run_task_endpoint(task_id: str, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    task = Ledger.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    Ledger.update_task_status(task_id, "running")
    threading.Thread(target=run_task, args=(task_id,), daemon=True).start()
    return {"status": "running", "task_id": task_id}


@app.post("/tasks/{task_id}/resume")
def resume_task_endpoint(task_id: str, background_tasks: BackgroundTasks) -> Dict[str, Any]:
    task = Ledger.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    Ledger.update_task_status(task_id, "running")
    threading.Thread(target=resume_task, args=(task_id,), daemon=True).start()
    return {"status": "resuming", "task_id": task_id}


@app.post("/tasks/{task_id}/sabotage")
def sabotage_task_endpoint(task_id: str) -> Dict[str, Any]:
    if not is_demo_mode():
        raise HTTPException(status_code=403, detail="Sabotage endpoint is disabled when SAHA_DEMO_MODE is false.")

    task = Ledger.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    summary = Ledger.get_task_summary(task_id)
    target_rel = None
    for s in summary["steps"]:
        if s["tool"] == "write_file":
            target_rel = s["args"].get("path")
            break

    if not target_rel:
        target_rel = "facts1.txt"

    target_disk_path = resolve_workspace_path(target_rel, task_id=task_id)
    existed = target_disk_path.exists()
    if existed:
        target_disk_path.unlink()

    return {
        "status": "sabotaged",
        "task_id": task_id,
        "deleted_file": target_rel,
        "path": str(target_disk_path),
        "file_existed_before_deletion": existed,
        "ledger_touched": False,
    }


@app.get("/")
def serve_index() -> FileResponse:
    index_file = WEB_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Web UI not found")
    return FileResponse(index_file)
