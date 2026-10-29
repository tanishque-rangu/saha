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
from backend.executor import execute_step, run_task
from backend.ledger import Ledger
from backend.llm import call_llm, filter_context_tags
from backend.planner import generate_plan, get_planner_system_prompt
from backend.policy import approve_request, create_approval, deny_request, get_owner_policy, set_owner_policy, Policy
from backend.resume import resume_task
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


def test_saha_require_persistent_enforcement(monkeypatch):
    """When SAHA_REQUIRE_PERSISTENT=true and data dir is unwritable, raises RuntimeError."""
    monkeypatch.setenv("SAHA_REQUIRE_PERSISTENT", "true")

    unwritable_path = "/nonexistent_root_dir_12345/data/saha.db" if sys.platform != "win32" else "Z:\\nonexistent_drive_12345\\saha.db"
    monkeypatch.setenv("SAHA_DB_PATH", unwritable_path)
    monkeypatch.setenv("SAHA_WORKSPACE_ROOT", unwritable_path)

    with pytest.raises(RuntimeError) as exc_info:
        check_and_init_data_dirs()
    assert "SAHA_REQUIRE_PERSISTENT is enabled" in str(exc_info.value)


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


# =========================================================================
# 7. PUBLIC MODE HARD LIMITS & HEALTH PRIVACY TESTS
# =========================================================================

def test_public_mode_health_returns_only_booleans_and_status(monkeypatch, test_env):
    """GET /health in public mode returns only booleans and status fields (no paths, versions, or host info)."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    client = TestClient(app)
    res = client.get("/health")
    assert res.status_code == 200
    data = res.json()

    # Assert only booleans and status fields
    assert set(data.keys()) == {"status", "service", "public_mode", "demo_mode", "data_dir_ok", "data_persistent"}
    assert data["status"] == "ok"
    assert data["service"] == "Saha"
    assert data["public_mode"] is True
    assert isinstance(data["demo_mode"], bool)
    assert isinstance(data["data_dir_ok"], bool)
    assert isinstance(data["data_persistent"], bool)

    # Assert no paths, hostnames, or dependency versions
    assert "data_dir" not in data
    for k, v in data.items():
        if isinstance(v, str):
            assert "/" not in v and "\\" not in v


def test_public_mode_new_owner_policy_defaults(monkeypatch, test_env):
    """A new owner in public mode receives policy defaults with only {write_file, read_file, http_get, list_files}."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    client = TestClient(app)
    token = "owner_token_new_defaults_32_characters"
    headers = {"X-Saha-Owner": token}

    res = client.get("/policy", headers=headers)
    assert res.status_code == 200
    policy = res.json()["policy"]

    # Verify policy defaults
    assert set(policy["allowed_tools"]) == {"write_file", "read_file", "http_get", "list_files"}
    assert "run_shell" not in policy["allowed_tools"]
    assert policy["max_steps"] == 8
    assert policy["auto_heal"] is True
    assert set(policy["allowed_domains"]) == {"example.com", "wikipedia.org", "httpbin.org"}


def test_public_mode_put_policy_rejects_unpermitted_tools(monkeypatch, test_env):
    """PUT /policy cannot add run_shell or any tool outside allowed set in public mode (rejects with 400)."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    client = TestClient(app)
    token = "owner_token_put_policy_32_characters_123"
    headers = {"X-Saha-Owner": token}

    # 1. Attempt to add run_shell
    payload = {
        "allowed_tools": ["write_file", "read_file", "run_shell"],
        "allowed_domains": ["example.com"],
        "allowed_paths": ["*"],
        "auto_heal": True,
        "max_steps": 8,
    }
    res = client.put("/policy", headers=headers, json=payload)
    assert res.status_code == 400
    assert "run_shell" in res.json()["detail"]

    # 2. Attempt to add unknown/arbitrary tool
    payload["allowed_tools"] = ["write_file", "read_file", "custom_eval_tool"]
    res2 = client.put("/policy", headers=headers, json=payload)
    assert res2.status_code == 400
    assert "custom_eval_tool" in res2.json()["detail"]


def test_public_mode_approval_rejects_run_shell(monkeypatch, test_env):
    """Approvals for run_shell cannot be approved in public mode (rejects with 400)."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    client = TestClient(app)
    token = "owner_token_appr_reject_32_chars_long_12"
    headers = {"X-Saha-Owner": token}
    oid = hash_owner_token(token)
    db_path = test_env["db"]

    task = Ledger.create_task("Public RunShell Approval Task", owner_id=oid, db_path=db_path)
    step = Ledger.add_steps(
        task.id,
        [{"intent": "Shell step", "tool": "run_shell", "args": {"cmd": "ls"}, "probe": {}, "depends_on": []}],
        db_path=db_path,
    )[0]
    appr_id = create_approval(oid, task.id, step.id, "run_shell", {"cmd": "ls"}, "Run shell blocked", db_path=db_path)

    res = client.post(f"/approvals/{appr_id}/approve", headers=headers)
    assert res.status_code == 400
    assert "run_shell" in res.json()["detail"]
    assert "strictly forbidden" in res.json()["detail"].lower()


def test_public_mode_executor_refuses_run_shell(monkeypatch, test_env):
    """Executor marks step failed immediately if tool is run_shell in public mode."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    oid = "test_owner_public_executor"
    db_path = test_env["db"]

    task = Ledger.create_task("Shell Execution Task", owner_id=oid, db_path=db_path)
    step = Ledger.add_steps(
        task.id,
        [{"intent": "Execute shell", "tool": "run_shell", "args": {"cmd": "echo hello"}, "probe": {}, "depends_on": []}],
        db_path=db_path,
    )[0]

    res_step = execute_step(step.id, db_path=db_path)
    assert res_step.status == "failed"
    assert "strictly forbidden in public mode" in res_step.error


def test_public_mode_planner_excludes_run_shell(monkeypatch):
    """Planner prompt strictly excludes run_shell, and generate_plan rejects plans with unpermitted tools in public mode."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    prompt = get_planner_system_prompt()
    assert "run_shell" in prompt
    assert "'run_shell' and any tools outside" in prompt
    assert "strictly DISABLED and forbidden" in prompt

    # Mock call_llm_json to return run_shell step in public mode
    mock_plan = {
        "steps": [
            {
                "idx": 1,
                "intent": "Try running shell",
                "tool": "run_shell",
                "args": {"cmd": "echo test"},
                "probe": {},
                "depends_on": [],
            }
        ]
    }
    with patch("backend.planner.call_llm_json", return_value=mock_plan):
        with pytest.raises(ValueError) as exc:
            generate_plan("Test run_shell rejection")
        assert "forbidden" in str(exc.value)


def test_step_blocked_then_approved_clears_block_reason_and_drift_reason_null(monkeypatch, test_env):
    """A step blocked by policy has block_reason set and drift_reason is null.
    Once approved, block_reason is cleared (null) and drift_reason is null.
    If denied, block_reason is kept intact."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    client = TestClient(app)
    token = "owner_token_block_reason_test_32_chars_1"
    headers = {"X-Saha-Owner": token}
    oid = hash_owner_token(token)
    db_path = test_env["db"]

    # 1. Create a task with an unallowed domain step (http_get to python.org)
    task = Ledger.create_task("Block Reason Task", owner_id=oid, db_path=db_path)
    step = Ledger.add_steps(
        task.id,
        [{
            "intent": "Check python.org",
            "tool": "http_get",
            "args": {"url": "https://www.python.org"},
            "probe": {"expected_status": 200},
            "depends_on": [],
        }],
        db_path=db_path,
    )[0]

    # Execute step -> policy blocks it
    blocked_step = execute_step(step.id, db_path=db_path)
    assert blocked_step.status == "blocked"
    assert blocked_step.block_reason is not None
    assert "not in policy allowed_domains" in blocked_step.block_reason
    assert blocked_step.drift_reason is None

    # Verify database directly
    row_blocked = Ledger.get_step(step.id, db_path=db_path)
    assert row_blocked.status == "blocked"
    assert row_blocked.block_reason is not None
    assert row_blocked.drift_reason is None

    # 2. Approve the step
    apprs = client.get("/approvals?status=pending", headers=headers).json()["approvals"]
    assert len(apprs) == 1
    appr_id = apprs[0]["id"]

    with patch("backend.api.run_task"):
        res_appr = client.post(f"/approvals/{appr_id}/approve", headers=headers, json={"always_allow": True})
        assert res_appr.status_code == 200

    # Step must now be pending with block_reason is None and drift_reason is None
    row_approved = Ledger.get_step(step.id, db_path=db_path)
    assert row_approved.status == "pending"
    assert row_approved.block_reason is None
    assert row_approved.drift_reason is None

    # 3. Test Deny keeps block_reason
    step2 = Ledger.add_steps(
        task.id,
        [{
            "intent": "Check other unallowed",
            "tool": "http_get",
            "args": {"url": "https://www.ruby-lang.org"},
            "probe": {"expected_status": 200},
            "depends_on": [],
        }],
        db_path=db_path,
    )[-1]
    blocked_step2 = execute_step(step2.id, db_path=db_path)
    assert blocked_step2.status == "blocked"
    assert blocked_step2.block_reason is not None

    apprs2 = client.get("/approvals?status=pending", headers=headers).json()["approvals"]
    appr2_id = [a["id"] for a in apprs2 if a["step_id"] == step2.id][0]
    res_deny = client.post(f"/approvals/{appr2_id}/deny", headers=headers)
    assert res_deny.status_code == 200

    row_denied = Ledger.get_step(step2.id, db_path=db_path)
    assert row_denied.status == "failed"
    assert row_denied.block_reason is not None
    assert row_denied.drift_reason is None


def test_public_mode_six_step_plan_with_sabotage_and_repair(monkeypatch, test_env):
    """In public mode with max_steps=8, a 6-step task plus 2 repair steps after sabotage succeeds.
    Proves max_steps applies to newly proposed plan/replan size, not ledger cumulative total."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    monkeypatch.setenv("SAHA_DEMO_MODE", "true")
    oid = "test_owner_six_step_public"
    db_path = test_env["db"]

    task = Ledger.create_task("Six Step Public Task", owner_id=oid, db_path=db_path)

    # 6 initial steps: 5 independent files + 1 combined file depending on 1
    steps_data = [
        {"intent": "Write f1", "tool": "write_file", "args": {"path": "f1.txt", "content": "f1 content"}, "probe": {}, "depends_on": []},
        {"intent": "Write f2", "tool": "write_file", "args": {"path": "f2.txt", "content": "f2 content"}, "probe": {}, "depends_on": []},
        {"intent": "Write f3", "tool": "write_file", "args": {"path": "f3.txt", "content": "f3 content"}, "probe": {}, "depends_on": []},
        {"intent": "Write f4", "tool": "write_file", "args": {"path": "f4.txt", "content": "f4 content"}, "probe": {}, "depends_on": []},
        {"intent": "Write f5", "tool": "write_file", "args": {"path": "f5.txt", "content": "f5 content"}, "probe": {}, "depends_on": []},
        {"intent": "Write f6 summary", "tool": "write_file", "args": {"path": "f6.txt", "content": "f6 summary"}, "probe": {}, "depends_on": [1]},
    ]
    assert len(steps_data) == 6
    steps = Ledger.add_steps(task.id, steps_data, db_path=db_path)
    assert len(steps) == 6

    # Mock Worker model for deterministic step execution
    with patch("backend.executor.call_llm", return_value="Worker generated content"):
        # Run all 6 steps
        run_task(task.id, db_path=db_path)

    summary = Ledger.get_task_summary(task.id, owner_id=oid, db_path=db_path)
    assert summary["task"]["status"] == "complete"
    assert all(s["status"] == "verified" for s in summary["steps"])

    # Sabotage: delete f1.txt from disk
    f1_disk = resolve_workspace_path("f1.txt", task_id=task.id, owner_id=oid)
    assert f1_disk.exists()
    f1_disk.unlink()
    assert not f1_disk.exists()

    # Mock replan_drift to return 2 repair steps (re-write f1 and re-combine f6)
    from backend.planner import StepPlan
    repair_step_plans = [
        StepPlan(
            idx=1,
            intent="Repair f1",
            tool="write_file",
            args={"path": "f1.txt", "content": "repaired f1 content"},
            probe={},
            depends_on=[],
        ),
        StepPlan(
            idx=2,
            intent="Re-combine f6 summary",
            tool="write_file",
            args={"path": "f6.txt", "content": "repaired f6 summary"},
            probe={},
            depends_on=[1],
        ),
    ]

    # Resume task: drift probe marks step 1 drifted, step 6 superseded, appends 2 repairs
    with patch("backend.resume.replan_drift", return_value=repair_step_plans), \
         patch("backend.executor.call_llm", return_value="Worker repaired content"):
        resume_task(task.id, db_path=db_path)

    summary_after = Ledger.get_task_summary(task.id, owner_id=oid, db_path=db_path)
    assert summary_after["task"]["status"] == "complete"
    # Total historical steps in ledger is 8 (6 original + 2 repair)
    assert summary_after["total_steps_count"] == 8
    # Step 1 stays drifted
    assert summary_after["steps"][0]["status"] == "drifted"
    assert summary_after["steps"][0]["drift_reason"] is not None
    assert summary_after["steps"][0]["block_reason"] is None
    # Step 6 is superseded
    assert summary_after["steps"][5]["status"] == "superseded"
    # Repair steps (7 and 8) are verified
    assert summary_after["steps"][6]["status"] == "verified"
    assert summary_after["steps"][7]["status"] == "verified"


