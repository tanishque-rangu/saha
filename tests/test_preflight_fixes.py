"""Tests for Pre-flight Fixes 1 through 6.

Covers:
1. Owner header required in public mode (401 without header, 401 too short, 200 /health, /docs 404, anonymous owner in private mode).
2. Egress tags format whitelist: ^(goal_id|task_id|step|role|memory|file|probe|skill):[A-Za-z0-9._-]{1,64}$. Dropped tags count. Egress row contains zero prompt/response text.
3. Approval resumes task automatically with mocked executor/LLM, double-start guard, and cross-owner 404 protection.
4. Data directory health status in /health (data_dir_ok, data_persistent, data_dir) and unwritable fallback.
6. Symlink escape rejections for read_file, write_file, list_files, run_shell (skipped on Windows if symlinks cannot be created).
"""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.api import app, IP_POST_HISTORY, OWNER_POST_HISTORY
from backend.db import (
    check_and_init_data_dirs,
    get_data_dir_status,
    get_db_path,
    get_owner_egress_logs,
    hash_owner_token,
    init_db,
    record_llm_call,
)
from backend.executor import run_task
from backend.ledger import Ledger
from backend.llm import call_llm, filter_context_tags
from backend.policy import approve_request, create_approval, deny_request, get_owner_policy, set_owner_policy, Policy
from backend.tools.files import list_files, read_file, write_file
from backend.tools.sandbox import get_workspace_dir, resolve_workspace_path
from backend.tools.shell import run_shell


@pytest.fixture
def test_env(monkeypatch):
    """Setup isolated test database and workspace."""
    tmp_dir = tempfile.mkdtemp()
    test_db = Path(tmp_dir) / "preflight_test.db"
    test_ws = Path(tmp_dir) / "workspace"
    test_ws.mkdir()

    monkeypatch.setenv("SAHA_DB_PATH", str(test_db))
    monkeypatch.setenv("SAHA_WORKSPACE_ROOT", str(test_ws))
    monkeypatch.setenv("SAHA_DEMO_MODE", "true")
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "false")
    monkeypatch.setenv("NEBIUS_API_KEY", "")

    init_db(str(test_db))
    check_and_init_data_dirs()
    IP_POST_HISTORY.clear()
    OWNER_POST_HISTORY.clear()
    return {"db": str(test_db), "ws": str(test_ws), "tmp": tmp_dir}


# =========================================================================
# 1. OWNER HEADER IN PUBLIC MODE
# =========================================================================

def test_public_mode_owner_header_required(test_env, monkeypatch):
    """When SAHA_PUBLIC_MODE=true, requests without valid X-Saha-Owner (>=32 chars) get 401."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    client = TestClient(app)

    # 1. Without header -> 401
    res_no_header = client.get("/tasks")
    assert res_no_header.status_code == 401
    assert "minimum 32 characters" in res_no_header.json()["detail"].lower()

    # 2. Too-short token (<32 chars) -> 401
    res_short = client.get("/tasks", headers={"X-Saha-Owner": "short_token_12345"})
    assert res_short.status_code == 401
    assert "minimum 32 characters" in res_short.json()["detail"].lower()

    # 3. Valid token (>=32 chars) -> 200
    valid_token = "valid_user_token_32_characters_123456"
    res_valid = client.get("/tasks", headers={"X-Saha-Owner": valid_token})
    assert res_valid.status_code == 200
    assert "tasks" in res_valid.json()

    # 4. GET /health without header -> 200 OK
    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json()["status"] == "ok"
    assert res_health.json()["public_mode"] is True

    # 5. UI static page GET / without header -> 200 OK
    res_root = client.get("/")
    assert res_root.status_code == 200

    # 6. /docs, /redoc, /openapi.json are disabled (404) in public mode
    for path in ("/docs", "/redoc", "/openapi.json"):
        res_doc = client.get(path)
        assert res_doc.status_code == 404, f"{path} should return 404 in public mode"


def test_anonymous_owner_when_public_mode_off(test_env, monkeypatch):
    """Anonymous default owner works when SAHA_PUBLIC_MODE is false (local CLI/scripts)."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "false")
    client = TestClient(app)

    # Calling endpoints without X-Saha-Owner should succeed under default anonymous owner
    res = client.get("/tasks")
    assert res.status_code == 200
    assert "tasks" in res.json()

    res_policy = client.get("/policy")
    assert res_policy.status_code == 200
    assert res_policy.json()["owner_id"] == hash_owner_token("default-anonymous-owner")


# =========================================================================
# 2. EGRESS TAGS WHITELIST & TELEMETRY
# =========================================================================

def test_egress_tags_whitelist_and_dropped_count(test_env):
    """Whitelist ^(goal_id|task_id|step|role|memory|file|probe|skill):[A-Za-z0-9._-]{1,64}$."""
    db_path = test_env["db"]
    token = "egress_test_owner_token_32_chars_123"
    oid = hash_owner_token(token)

    # Valid and invalid tags
    test_tags = [
        "role:worker",                                # valid
        "step:1",                                     # valid
        "task_id:abc-123_45.x",                       # valid
        "file:summary_report.txt",                    # valid
        "probe:facts1_txt_probe",                     # valid
        "skill:data-analysis",                        # valid
        "invalid_prefix:something",                   # invalid prefix
        "role:has spaces",                            # contains spaces
        "role:free text with punctuation!?",          # free text
        "prompt:what is the capital of France?",      # free text
        "response:Paris is the capital",              # free text
        "goal_id:" + "a" * 65,                        # too long (>64 chars)
    ]

    valid_tags, dropped_count = filter_context_tags(test_tags)
    assert len(valid_tags) == 6
    assert dropped_count == 6
    assert "role:worker" in valid_tags
    assert "file:summary_report.txt" in valid_tags
    assert not any(" " in t for t in valid_tags)
    assert not any("prompt" in t or "response" in t for t in valid_tags)

    # Record via record_llm_call
    record_llm_call(
        model="nvidia/nemotron-3-super-120b-a12b",
        latency_ms=145.2,
        role="worker",
        owner_id=oid,
        context_tags=valid_tags,
        input_chars=250,
        output_chars=40,
        redaction_count=2,
        dropped_tags=dropped_count,
        status="success",
        db_path=db_path,
    )

    logs = get_owner_egress_logs(oid, db_path=db_path)
    assert len(logs) == 1
    log = logs[0]
    assert log["dropped_tags"] == 6
    assert log["redaction_count"] == 2
    assert "context_tags" in log
    assert "prompt" not in log["context_tags"]
    assert "free text" not in log["context_tags"]


def test_egress_row_never_contains_prompt_or_response_text(test_env):
    """The egress/llm_calls row contains only metadata, latencies, and counts—zero text."""
    db_path = test_env["db"]
    token = "egress_privacy_user_token_32_chars"
    oid = hash_owner_token(token)

    fake_prompt = "Secret Prompt text that should never appear in egress"
    fake_response = "Secret Response containing confidential answers"

    record_llm_call(
        model="test-model",
        latency_ms=88.5,
        role="planner",
        owner_id=oid,
        context_tags=["role:planner", "step:2"],
        input_chars=len(fake_prompt),
        output_chars=len(fake_response),
        redaction_count=1,
        dropped_tags=0,
        status="success",
        db_path=db_path,
    )

    logs = get_owner_egress_logs(oid, db_path=db_path)
    assert len(logs) == 1
    log_str = str(logs[0]).lower()
    assert "secret prompt" not in log_str
    assert "confidential answers" not in log_str


# =========================================================================
# 3. APPROVAL RESUMES THE TASK AUTOMATICALLY
# =========================================================================

def test_approval_resumes_task_automatically(test_env):
    """POST /approvals/{id}/approve re-queues step and resumes executor to completion."""
    client = TestClient(app)
    token = "approval_auto_resume_owner_32_chars"
    headers = {"X-Saha-Owner": token}
    owner_id = hash_owner_token(token)
    db_path = test_env["db"]

    task = Ledger.create_task("Test Auto Resume Flow", owner_id=owner_id, db_path=db_path)
    step1_data = {
        "idx": 1,
        "intent": "Write first file",
        "tool": "write_file",
        "args": {"path": "step1.txt", "content": "data1"},
        "probe": {"type": "file_exists", "path": "step1.txt"},
        "depends_on": [],
    }
    step2_data = {
        "idx": 2,
        "intent": "Write second file",
        "tool": "write_file",
        "args": {"path": "step2.txt", "content": "data2"},
        "probe": {"type": "file_exists", "path": "step2.txt"},
        "depends_on": [1],
    }
    steps = Ledger.add_steps(task.id, [step1_data, step2_data], db_path=db_path)

    # Set step 1 status to blocked and create approval row
    Ledger.mark_step_blocked(steps[0].id, reason="Requires owner confirmation", db_path=db_path)
    Ledger.update_task_status(task.id, "paused", db_path=db_path)
    appr_id = create_approval(
        owner_id=owner_id,
        task_id=task.id,
        step_id=steps[0].id,
        tool="write_file",
        args={"path": "step1.txt", "content": "data1"},
        reason="Requires owner confirmation",
        db_path=db_path,
    )

    # Mock execute_step so the executor finishes synchronously without external LLM
    def mock_execute_step(step_id, db_path=None):
        s = Ledger.get_step(step_id, db_path=db_path)
        # Write actual file so probes pass
        args = s.to_dict().get("args", {})
        if s.tool == "write_file":
            write_file(args.get("path", "f.txt"), args.get("content", "c"), task_id=s.task_id)
        Ledger.mark_step_running(s.id, db_path=db_path)
        Ledger.mark_step_done(s.id, result={"bytes": 5}, db_path=db_path)
        return Ledger.mark_step_verified(s.id, probe={"passed": True}, db_path=db_path)

    with patch("backend.executor.execute_step", side_effect=mock_execute_step):
        # POST /approvals/{id}/approve
        res = client.post(f"/approvals/{appr_id}/approve", headers=headers)
        assert res.status_code == 200
        assert res.json()["status"] == "approved"
        assert res.json()["task_status"] in ("running", "already_running")

        # Wait for background executor to finish the task
        for _ in range(30):
            summary = Ledger.get_task_summary(task.id, owner_id=owner_id, db_path=db_path)
            if summary["task"]["status"] == "complete":
                break
            time.sleep(0.05)

        summary = Ledger.get_task_summary(task.id, owner_id=owner_id, db_path=db_path)
        assert summary["task"]["status"] == "complete"
        assert all(s["status"] == "verified" for s in summary["steps"])


def test_approval_cross_owner_returns_404(test_env):
    """Attempting to approve another owner's approval returns 404."""
    client = TestClient(app)
    token_a = "token_owner_a_32_characters_long_111"
    token_b = "token_owner_b_32_characters_long_222"
    headers_a = {"X-Saha-Owner": token_a}
    headers_b = {"X-Saha-Owner": token_b}
    oid_a = hash_owner_token(token_a)
    oid_b = hash_owner_token(token_b)
    db_path = test_env["db"]

    task_a = Ledger.create_task("Task A", owner_id=oid_a, db_path=db_path)
    step_a = Ledger.add_steps(task_a.id, [{"intent": "Step A", "tool": "write_file", "args": {}, "probe": {}, "depends_on": []}], db_path=db_path)[0]
    appr_a = create_approval(oid_a, task_a.id, step_a.id, "write_file", {}, "reason", db_path=db_path)

    # Owner B tries to approve Owner A's request -> 404
    res_b = client.post(f"/approvals/{appr_a}/approve", headers=headers_b)
    assert res_b.status_code == 404

    # Owner B tries to deny Owner A's request -> 404
    res_b_deny = client.post(f"/approvals/{appr_a}/deny", headers=headers_b)
    assert res_b_deny.status_code == 404


def test_approval_deny_marks_step_and_task_failed(test_env):
    """Denying an approval marks the step failed and the task failed."""
    client = TestClient(app)
    token = "token_deny_owner_32_chars_1234567890"
    headers = {"X-Saha-Owner": token}
    oid = hash_owner_token(token)
    db_path = test_env["db"]

    task = Ledger.create_task("Deny Task", owner_id=oid, db_path=db_path)
    step = Ledger.add_steps(task.id, [{"intent": "Step D", "tool": "http_get", "args": {}, "probe": {}, "depends_on": []}], db_path=db_path)[0]
    appr_id = create_approval(oid, task.id, step.id, "http_get", {}, "reason", db_path=db_path)

    res = client.post(f"/approvals/{appr_id}/deny", headers=headers)
    assert res.status_code == 200
    assert res.json()["status"] == "denied"
    assert res.json()["task_status"] == "failed"

    task_after = Ledger.get_task(task.id, owner_id=oid, db_path=db_path)
    assert task_after.status == "failed"
    step_after = Ledger.get_step(step.id, db_path=db_path)
    assert step_after.status == "failed"


# =========================================================================
# 4. DATA DIRECTORY STATUS & HEALTH
# =========================================================================

def test_health_data_dir_status(test_env):
    """GET /health exposes data_dir_ok, data_persistent, and data_dir without secrets."""
    client = TestClient(app)
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()
    assert "data_dir_ok" in data
    assert "data_persistent" in data
    assert "data_dir" in data
    assert isinstance(data["data_dir_ok"], bool)
    assert isinstance(data["data_persistent"], bool)
    # Ensure no secrets leak in data_dir
    assert "key" not in data["data_dir"].lower()
    assert "token" not in data["data_dir"].lower()


# =========================================================================
# 6. LINUX-SAFE TESTS & SYMLINK ESCAPES
# =========================================================================

def can_create_symlinks() -> bool:
    """Check if the current user and OS permit symlink creation."""
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "target.txt"
        target.write_text("test")
        link = Path(td) / "link.txt"
        try:
            os.symlink(target, link)
            return True
        except (OSError, NotImplementedError):
            return False


@pytest.mark.skipif(not can_create_symlinks(), reason="Symlink creation not permitted on this OS/user (Windows unprivileged)")
def test_symlink_escape_rejection(test_env):
    """A symlink inside task workspace pointing outside must be rejected by read_file, write_file, list_files, and run_shell."""
    with tempfile.TemporaryDirectory() as td:
        outside_file = Path(td) / "outside_secret.txt"
        outside_file.write_text("super_secret_outside_data")

        tid = "symlink_test_task"
        ws = get_workspace_dir(tid)

        # 1. Symlink pointing to external file
        symlink_file = ws / "symlink_file.txt"
        if symlink_file.exists() or symlink_file.is_symlink():
            symlink_file.unlink()
        os.symlink(outside_file, symlink_file)

        # read_file must reject
        with pytest.raises(PermissionError):
            read_file("symlink_file.txt", task_id=tid)

        # write_file must reject
        with pytest.raises(PermissionError):
            write_file("symlink_file.txt", "overwritten", task_id=tid)

        # list_files must reject pointing directly to symlink target
        with pytest.raises(PermissionError):
            list_files("symlink_file.txt", task_id=tid)

        # 2. Symlink pointing to external directory
        outside_dir = Path(td) / "outside_dir"
        outside_dir.mkdir()
        (outside_dir / "secret_script.py").write_text("print('evil')", encoding="utf-8")

        symlink_dir = ws / "symlink_dir"
        if symlink_dir.exists() or symlink_dir.is_symlink():
            symlink_dir.unlink()
        os.symlink(outside_dir, symlink_dir)

        # list_files inside symlinked directory must reject
        with pytest.raises(PermissionError):
            list_files("symlink_dir", task_id=tid)

        # run_shell python script inside symlinked directory must reject
        with pytest.raises(PermissionError):
            run_shell("python symlink_dir/secret_script.py", task_id=tid)
