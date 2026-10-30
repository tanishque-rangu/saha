"""Focused End-to-End API Integration Tests with Real OS Process Termination and Restart.

Exercises:
1. Real FastAPI / Uvicorn server in separate OS processes.
2. Real lifespan / startup recovery hook running on server boot.
3. Mid-flight OS process termination (proc.kill()) during an active HTTP mutation.
4. Real authenticated API resume route (POST /tasks/{id}/resume with X-Saha-Owner).
5. Verification that completed steps are not repeated, trusted mutations reuse stable idempotency keys,
   and untrusted ambiguous mutations remain blocked pending owner action.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

from backend.db import hash_owner_token, init_db
from backend.ledger import Ledger
from backend.policy import Policy, get_owner_approvals, set_owner_policy


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_health(port: int, timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def wait_for_marker(marker_file: Path, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker_file.exists():
            return True
        time.sleep(0.05)
    return False


def api_request(url: str, method: str = "GET", headers: dict = None, body: dict = None):
    req_headers = headers or {}
    data = json.dumps(body).encode("utf-8") if body is not None else None
    if body is not None and "Content-Type" not in req_headers:
        req_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as err:
        content = err.read().decode("utf-8")
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = {"raw": content}
        return err.code, parsed


def test_real_api_server_interruption_and_resume_with_trusted_idempotency():
    """Process 1 starts real API server, executes step 1, is terminated mid-flight on step 2.
    Process 2 starts real API server, executes normal startup hook, resumes via authenticated API,
    and finishes task without repeating step 1 and reusing stable idempotency key on step 2."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "api_recovery.db"
        ws_path = tmp_path / "workspace"
        marker_dir = tmp_path / "markers"
        ws_path.mkdir(parents=True, exist_ok=True)
        marker_dir.mkdir(parents=True, exist_ok=True)

        init_db(str(db_path))

        owner_token = "api_owner_tok_trusted_32_chars!!"
        owner_id = hash_owner_token(owner_token)

        # Owner policy authorizes partner domain with trusted idempotency
        policy = Policy(
            allowed_tools=["write_file", "read_file", "http_request", "list_files"],
            allowed_domains=["partner.service.com"],
            trusted_idempotency_domains=["partner.service.com"],
            allowed_paths=["*"],
            auto_heal=False,
            max_steps=10,
        )
        set_owner_policy(owner_id, policy, db_path=str(db_path))

        task = Ledger.create_task("Real API Process Recovery Task", owner_id=owner_id, db_path=str(db_path))
        steps = Ledger.add_steps(
            task.id,
            [
                {
                    "idx": 1,
                    "intent": "Write initial data via API",
                    "tool": "write_file",
                    "args": {"path": "api_s1.txt", "content": "api step 1 data"},
                    "probe": {},
                    "depends_on": [],
                },
                {
                    "idx": 2,
                    "intent": "Execute payment mutation via API",
                    "tool": "http_request",
                    "args": {
                        "url": "https://partner.service.com/v1/charge",
                        "method": "POST",
                        "json": {"amount": 2500},
                    },
                    "probe": {"expected_status": 201},
                    "depends_on": [1],
                },
                {
                    "idx": 3,
                    "intent": "Write receipt via API",
                    "tool": "write_file",
                    "args": {"path": "api_s3.txt", "content": "receipt"},
                    "probe": {},
                    "depends_on": [2],
                },
            ],
            db_path=str(db_path),
        )
        s1_id = steps[0].id
        s2_id = steps[1].id
        s3_id = steps[2].id

        runner_script = PROJECT_ROOT / "scripts" / "api_server_subprocess_runner.py"

        # -------------------------------------------------------------
        # 1. Start Process 1: Real FastAPI server in separate OS process
        # -------------------------------------------------------------
        port1 = find_free_port()
        sub_env = os.environ.copy()
        sub_env["SAHA_DB_PATH"] = str(db_path)
        sub_env["SAHA_WORKSPACE_ROOT"] = str(ws_path)
        sub_env["SAHA_PUBLIC_MODE"] = "true"
        sub_env["SAHA_DEMO_MODE"] = "true"
        sub_env["PYTHONPATH"] = str(PROJECT_ROOT)
        sub_env["NEBIUS_API_KEY"] = ""

        p1 = subprocess.Popen([
            sys.executable,
            str(runner_script),
            "pause_on_mutation",
            str(port1),
            str(db_path),
            str(ws_path),
            str(marker_dir),
        ], env=sub_env)

        assert wait_for_health(port1, timeout=12.0), "Process 1 API server failed to become healthy"

        # Trigger execution through real authenticated API endpoint: POST /tasks/{id}/run
        run_status, run_body = api_request(
            f"http://127.0.0.1:{port1}/tasks/{task.id}/run",
            method="POST",
            headers={"X-Saha-Owner": owner_token},
        )
        assert run_status == 200, f"Failed to start task via API: {run_body}"
        assert run_body.get("status") == "running"

        # Wait until Step 2 is in-flight
        ready_marker = marker_dir / "api_step2_ready_to_kill.marker"
        assert wait_for_marker(ready_marker, timeout=15.0), "Process 1 did not reach in-flight Step 2"

        # -------------------------------------------------------------
        # 2. Terminate Process 1 mid-flight (real OS kill)
        # -------------------------------------------------------------
        p1.kill()
        p1.wait(timeout=5)

        # Audit post-crash database state:
        s1_post = Ledger.get_step(s1_id, db_path=str(db_path))
        s2_post = Ledger.get_step(s2_id, db_path=str(db_path))
        task_post = Ledger.get_task(task.id, db_path=str(db_path))

        assert s1_post.status == "verified", "Step 1 was not persisted as verified"
        assert s2_post.status == "running", "Step 2 should be left in running status"
        assert s2_post.result_json is None, "Step 2 should have no result recorded"
        assert task_post.status == "running", "Task should still have running status in SQLite"

        # Stable idempotency key was persisted by Process 1
        s2_args = json.loads(s2_post.args_json)
        initial_key = s2_args.get("idempotency_key")
        assert initial_key is not None and initial_key.startswith("idem_")

        # -------------------------------------------------------------
        # 3. Start Process 2: Real server boots, runs normal startup hook
        # -------------------------------------------------------------
        port2 = find_free_port()
        p2 = subprocess.Popen([
            sys.executable,
            str(runner_script),
            "normal_resume",
            str(port2),
            str(db_path),
            str(ws_path),
            str(marker_dir),
        ], env=sub_env)

        assert wait_for_health(port2, timeout=12.0), "Process 2 API server failed to become healthy"

        # Audit that Process 2 startup hook (Ledger.recover_stale_running_tasks) executed:
        task_after_boot = Ledger.get_task(task.id, db_path=str(db_path))
        assert task_after_boot.status == "paused", "Startup hook did not recover running task to paused"
        s2_after_boot = Ledger.get_step(s2_id, db_path=str(db_path))
        assert s2_after_boot.status == "pending", "Startup hook did not recover trusted step to pending"

        # -------------------------------------------------------------
        # 4. Resume via Real Authenticated API Route: POST /tasks/{id}/resume
        # -------------------------------------------------------------
        # A. Verify authentication requirement: missing token returns 401
        unauth_status, _ = api_request(f"http://127.0.0.1:{port2}/tasks/{task.id}/resume", method="POST")
        assert unauth_status == 401, f"Expected 401 without X-Saha-Owner header, got {unauth_status}"

        # B. Verify authorization boundary: foreign owner token returns 404
        wrong_token = "wrong_owner_token_32_characters!"
        foreign_status, _ = api_request(
            f"http://127.0.0.1:{port2}/tasks/{task.id}/resume",
            method="POST",
            headers={"X-Saha-Owner": wrong_token},
        )
        assert foreign_status == 404, f"Expected 404 for foreign owner, got {foreign_status}"

        # C. Authorized resume
        resume_status, resume_body = api_request(
            f"http://127.0.0.1:{port2}/tasks/{task.id}/resume",
            method="POST",
            headers={"X-Saha-Owner": owner_token},
        )
        assert resume_status == 200, f"Resume endpoint failed: {resume_body}"
        assert resume_body.get("status") == "resuming"

        # -------------------------------------------------------------
        # 5. Poll GET /tasks/{id} through real API until completion
        # -------------------------------------------------------------
        deadline = time.time() + 15.0
        final_summary = None
        while time.time() < deadline:
            detail_status, detail_body = api_request(
                f"http://127.0.0.1:{port2}/tasks/{task.id}",
                method="GET",
                headers={"X-Saha-Owner": owner_token},
            )
            if detail_status == 200 and detail_body.get("is_complete"):
                final_summary = detail_body
                break
            time.sleep(0.1)

        try:
            p2.terminate()
            p2.wait(timeout=5)
        except Exception:
            p2.kill()

        assert final_summary is not None, "Task did not complete within deadline via API"
        assert final_summary["is_complete"] is True

        # -------------------------------------------------------------
        # 6. Audit Final Verification & Idempotency Invariants
        # -------------------------------------------------------------
        # A. Step 1 was NOT executed twice
        p2_written_file = marker_dir / f"server_{port2}_written_files.json"
        assert p2_written_file.exists()
        p2_written = json.loads(p2_written_file.read_text(encoding="utf-8"))
        assert "api_s1.txt" not in p2_written, "CRITICAL: Step 1 write was repeated in Process 2!"

        # B. Step 2 reused the EXACT same idempotency key
        p2_http_file = marker_dir / "server2_http_called.json"
        assert p2_http_file.exists()
        p2_http_calls = json.loads(p2_http_file.read_text(encoding="utf-8"))
        assert len(p2_http_calls) == 1
        assert p2_http_calls[0]["idempotency_key"] == initial_key, (
            f"Idempotency key changed! Expected {initial_key}, got {p2_http_calls[0]['idempotency_key']}"
        )

        # C. Step 3 was executed to completion
        assert "api_s3.txt" in p2_written


def test_real_api_server_interruption_with_untrusted_mutation_remains_blocked():
    """Process 1 starts real API server on an untrusted mutation and is killed mid-flight.
    Process 2 boots, startup hook flags ambiguous mutation, and POST /tasks/{id}/resume
    safely refuses automatic retry, keeping step blocked and task paused."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "api_untrusted.db"
        ws_path = tmp_path / "workspace"
        marker_dir = tmp_path / "markers"
        ws_path.mkdir(parents=True, exist_ok=True)
        marker_dir.mkdir(parents=True, exist_ok=True)

        init_db(str(db_path))

        owner_token = "api_owner_tok_untrusted_32_chars"
        owner_id = hash_owner_token(owner_token)

        # Policy: partner domain allowed in network, but trusted_idempotency_domains is EMPTY!
        policy = Policy(
            allowed_tools=["write_file", "read_file", "http_request", "list_files"],
            allowed_domains=["untrusted-billing.org"],
            trusted_idempotency_domains=[],  # Untrusted!
            allowed_paths=["*"],
            max_steps=10,
        )
        set_owner_policy(owner_id, policy, db_path=str(db_path))

        task = Ledger.create_task("Real API Untrusted Recovery Task", owner_id=owner_id, db_path=str(db_path))
        steps = Ledger.add_steps(
            task.id,
            [
                {
                    "idx": 1,
                    "intent": "Initial write",
                    "tool": "write_file",
                    "args": {"path": "init.txt", "content": "data"},
                    "probe": {},
                    "depends_on": [],
                },
                {
                    "idx": 2,
                    "intent": "Ambiguous mutation",
                    "tool": "http_request",
                    "args": {
                        "url": "https://untrusted-billing.org/charge",
                        "method": "POST",
                        "json": {"wire": 99000},
                        "supports_idempotency": True,  # Untrusted planner claim
                    },
                    "probe": {"expected_status": 201},
                    "depends_on": [1],
                },
            ],
            db_path=str(db_path),
        )
        s1_id = steps[0].id
        s2_id = steps[1].id

        runner_script = PROJECT_ROOT / "scripts" / "api_server_subprocess_runner.py"

        # Start Process 1
        port1 = find_free_port()
        sub_env = os.environ.copy()
        sub_env["SAHA_DB_PATH"] = str(db_path)
        sub_env["SAHA_WORKSPACE_ROOT"] = str(ws_path)
        sub_env["SAHA_PUBLIC_MODE"] = "true"
        sub_env["SAHA_DEMO_MODE"] = "true"
        sub_env["PYTHONPATH"] = str(PROJECT_ROOT)
        sub_env["NEBIUS_API_KEY"] = ""

        p1 = subprocess.Popen([
            sys.executable,
            str(runner_script),
            "pause_on_mutation",
            str(port1),
            str(db_path),
            str(ws_path),
            str(marker_dir),
        ], env=sub_env)

        assert wait_for_health(port1, timeout=12.0)

        # Trigger execution via API
        run_status, _ = api_request(
            f"http://127.0.0.1:{port1}/tasks/{task.id}/run",
            method="POST",
            headers={"X-Saha-Owner": owner_token},
        )
        assert run_status == 200

        # Wait until Step 2 is in-flight and kill Process 1
        ready_marker = marker_dir / "api_step2_ready_to_kill.marker"
        assert wait_for_marker(ready_marker, timeout=15.0)
        p1.kill()
        p1.wait(timeout=5)

        # Start Process 2
        port2 = find_free_port()
        p2 = subprocess.Popen([
            sys.executable,
            str(runner_script),
            "normal_resume",
            str(port2),
            str(db_path),
            str(ws_path),
            str(marker_dir),
        ], env=sub_env)

        assert wait_for_health(port2, timeout=12.0)

        # Resume via real authenticated API endpoint: POST /tasks/{id}/resume
        resume_status, resume_body = api_request(
            f"http://127.0.0.1:{port2}/tasks/{task.id}/resume",
            method="POST",
            headers={"X-Saha-Owner": owner_token},
        )
        assert resume_status == 200

        # Wait a moment for background thread to execute resume check
        time.sleep(1.0)

        # Audit via API GET /tasks/{id}:
        detail_status, detail_body = api_request(
            f"http://127.0.0.1:{port2}/tasks/{task.id}",
            method="GET",
            headers={"X-Saha-Owner": owner_token},
        )
        assert detail_status == 200
        assert detail_body["task"]["status"] == "paused"

        step2_detail = [s for s in detail_body["steps"] if s["id"] == s2_id][0]
        assert step2_detail["status"] == "blocked"
        assert "not confirmed by trusted owner policy" in (step2_detail["block_reason"] or "")

        # Verify no network request was sent in Process 2
        p2_http_file = marker_dir / "server2_http_called.json"
        assert not p2_http_file.exists() or len(json.loads(p2_http_file.read_text(encoding="utf-8"))) == 0

        # Verify approval is present via GET /approvals
        appr_status, appr_body = api_request(
            f"http://127.0.0.1:{port2}/approvals",
            method="GET",
            headers={"X-Saha-Owner": owner_token},
        )
        assert appr_status == 200
        approvals = appr_body.get("approvals", [])
        assert len(approvals) == 1
        assert approvals[0]["step_id"] == s2_id

        try:
            p2.terminate()
            p2.wait(timeout=5)
        except Exception:
            p2.kill()
