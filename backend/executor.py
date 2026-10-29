"""Saha Step and Task Executor.

Enforces fundamental ledger and role invariants:
1. Max concurrent tasks check in SAHA_PUBLIC_MODE.
2. Mark step as `running` BEFORE executing it.
3. WORKER model generates file content from `content_brief` and interprets tool results.
4. If WORKER model call fails, step is marked `failed` (no hardcoded fallback).
5. All file and tool operations are strictly isolated to workspace/<task_id>/.
6. Run deterministic verifier probe immediately after. Mark `verified` ONLY if probe passes.
"""

import json
import os
from typing import Any, Dict, Optional

from backend.db import get_db
from backend.ledger import Ledger, Step, Task
from backend.llm import WORKER_MODEL, call_llm
from backend.tools import http_get, read_file, run_shell, write_file
from backend.verifiers import verify_step

MAX_CONCURRENT_TASKS = 2


def is_public_mode() -> bool:
    return os.getenv("SAHA_PUBLIC_MODE", "false").lower() in ("true", "1", "yes")


def check_concurrency(db_path: Optional[str] = None) -> None:
    """Enforce maximum concurrent running tasks in public mode."""
    if not is_public_mode():
        return
    with get_db(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) as c FROM tasks WHERE status = 'running'").fetchone()
        active = row["c"] if row else 0
        if active >= MAX_CONCURRENT_TASKS:
            raise RuntimeError(
                f"Maximum concurrent tasks ({MAX_CONCURRENT_TASKS}) reached in SAHA_PUBLIC_MODE."
            )


def execute_step(step_id: str, db_path: Optional[str] = None) -> Step:
    """Execute a single step according to strict state machine invariants."""
    step = Ledger.get_step(step_id, db_path=db_path)
    if not step:
        raise ValueError(f"Step {step_id} not found")

    # RULE 1: Log running BEFORE executing
    step = Ledger.mark_step_running(step_id, db_path=db_path)

    # Parse arguments and probe spec
    try:
        args = json.loads(step.args_json) if step.args_json else {}
    except Exception:
        args = {}

    try:
        probe_spec = json.loads(step.probe_json) if step.probe_json else {}
    except Exception:
        probe_spec = {}

    task_id = step.task_id

    # Execute tool with WORKER model integration
    try:
        if step.tool == "write_file":
            path = args.get("path")
            if not path:
                raise ValueError("write_file requires 'path' argument")

            # If content_brief is present, WORKER synthesizes the content
            content_brief = args.get("content_brief")
            if content_brief:
                worker_prompt = (
                    f"You are Saha's Worker AI. Generate the exact file content for '{path}' based on this brief:\n"
                    f"{content_brief}\n\n"
                    f"Output strictly the file content. Do not include introductory remarks or conversational filler."
                )
                generated_content = call_llm(
                    model=WORKER_MODEL,
                    messages=[{"role": "user", "content": worker_prompt}],
                    role="worker",
                    temperature=0.0,
                    db_path=db_path,
                )
                actual_content = generated_content.strip()
            elif "content" in args:
                actual_content = str(args["content"])
            else:
                raise ValueError("write_file requires either 'content_brief' or 'content'")

            res = write_file(path, actual_content, task_id=task_id)
            result = {
                "path": res["path"],
                "bytes_written": res["bytes_written"],
                "sha256": res["sha256"],
                "content_preview": actual_content[:200],
                "model_used": WORKER_MODEL,
            }

        elif step.tool == "read_file":
            path = args.get("path")
            if not path:
                raise ValueError("read_file requires 'path' argument")
            res = read_file(path, task_id=task_id)
            result = {
                "path": res["path"],
                "bytes_read": res["bytes_read"],
                "sha256": res["sha256"],
                "content": res["content"],
                "model_used": WORKER_MODEL,
            }

        elif step.tool == "run_shell":
            cmd = args.get("cmd")
            if not cmd:
                raise ValueError("run_shell requires 'cmd' argument")
            res = run_shell(cmd, task_id=task_id)
            if res.get("exit_code") != 0:
                raise RuntimeError(f"Command failed (exit {res.get('exit_code')}): {res.get('stderr')}")

            # WORKER writes a 1-sentence interpretation of tool output
            worker_prompt = (
                f"In exactly one concise sentence, interpret the result of this command:\n"
                f"Command: {cmd}\nOutput: {res.get('stdout', '')[:500]}"
            )
            interpretation = call_llm(
                model=WORKER_MODEL,
                messages=[{"role": "user", "content": worker_prompt}],
                role="worker",
                temperature=0.0,
                db_path=db_path,
            )
            result = {
                "cmd": res["cmd"],
                "exit_code": res["exit_code"],
                "stdout": res["stdout"],
                "stderr": res["stderr"],
                "interpretation": interpretation.strip(),
                "model_used": WORKER_MODEL,
            }

        elif step.tool == "http_get":
            url = args.get("url")
            if not url:
                raise ValueError("http_get requires 'url' argument")
            res = http_get(url)
            if res.get("status_code") != 200:
                raise RuntimeError(f"HTTP request failed with status {res.get('status_code')}: {res.get('error')}")

            # WORKER writes a 1-sentence interpretation of HTTP response
            worker_prompt = (
                f"In exactly one concise sentence, interpret the HTTP response from {url} (status {res.get('status_code')}):\n"
                f"Body excerpt: {res.get('body', '')[:500]}"
            )
            interpretation = call_llm(
                model=WORKER_MODEL,
                messages=[{"role": "user", "content": worker_prompt}],
                role="worker",
                temperature=0.0,
                db_path=db_path,
            )
            result = {
                "url": res["url"],
                "status_code": res["status_code"],
                "body_length": res["body_length"],
                "interpretation": interpretation.strip(),
                "model_used": WORKER_MODEL,
            }

        else:
            raise ValueError(f"Unsupported tool '{step.tool}'")

    except Exception as exec_err:
        return Ledger.mark_step_failed(
            step_id=step_id,
            error=f"Execution error: {exec_err}",
            db_path=db_path,
        )

    # Mark step done with raw execution result
    step = Ledger.mark_step_done(step_id, result=result, db_path=db_path)

    # Run deterministic reality probe (independent of executor claims)
    probe_result = verify_step(
        tool=step.tool,
        args=args,
        result=result,
        probe_spec=probe_spec,
        task_id=task_id,
    )

    # Mark verified only if probe passes
    if probe_result.get("passed"):
        step = Ledger.mark_step_verified(step_id, probe=probe_result, db_path=db_path)
    else:
        err_msg = probe_result.get("error", "Verification probe failed")
        step = Ledger.mark_step_failed(
            step_id=step_id,
            error=err_msg,
            result=result,
            probe=probe_result,
            db_path=db_path,
        )

    return step


def run_task(task_id: str, db_path: Optional[str] = None) -> Task:
    """Run all active pending steps of a task sequentially."""
    task = Ledger.get_task(task_id, db_path=db_path)
    if not task:
        raise ValueError(f"Task {task_id} not found")

    check_concurrency(db_path=db_path)
    Ledger.update_task_status(task_id, "running", db_path=db_path)

    try:
        while True:
            # Re-fetch active (non-superseded) steps
            active_steps = Ledger.get_steps(task_id, include_superseded=False, db_path=db_path)
            pending_steps = [s for s in active_steps if s.status == "pending"]
            if not pending_steps:
                break

            current_step = pending_steps[0]
            executed = execute_step(current_step.id, db_path=db_path)
            if executed.status != "verified":
                Ledger.update_task_status(task_id, "failed", db_path=db_path)
                return Ledger.get_task(task_id, db_path=db_path)

        # Check if all active steps are verified
        summary = Ledger.get_task_summary(task_id, db_path=db_path)
        if summary["is_complete"]:
            Ledger.update_task_status(task_id, "complete", db_path=db_path)
        else:
            Ledger.update_task_status(task_id, "paused", db_path=db_path)

    except Exception as e:
        Ledger.update_task_status(task_id, "failed", db_path=db_path)
        raise

    return Ledger.get_task(task_id, db_path=db_path)
