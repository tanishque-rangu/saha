"""Focused tests for HTTP mutation recovery under process interruption.

Tests the following critical recovery requirements:
1. Interrupted HTTP mutation with idempotency key: persists stable key in SQLite,
   reuses the exact same key on retry, ensuring remote service deduplication.
2. Interrupted HTTP mutation without idempotency: outcome is ambiguous and cannot
   be verified; does NOT automatically retry, records explicit ambiguous/blocked
   state, and requires an authorized owner decision.
3. Owner authorization flow: approving the ambiguous mutation allows the task to resume;
   denying the ambiguous mutation marks the step and task failed.
4. Pre-crash completed mutation ('done' state with recorded result): probe verifies
   without repeating the external HTTP mutation.
5. Strict policy preservation: resumed HTTP actions strictly enforce policy and domain
   allowlists before network dispatch.
"""

import json
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from backend.api import app
from backend.db import get_db, hash_owner_token, init_db, check_and_init_data_dirs
from backend.executor import execute_step, run_task
from backend.ledger import Ledger
from backend.policy import approve_request, deny_request, get_owner_approvals, get_owner_policy, set_owner_policy
from backend.resume import resume_task
from backend.tools.http import is_http_mutation_step, target_supports_idempotency


@pytest.fixture
def http_recovery_env(monkeypatch):
    """Setup isolated test environment for HTTP mutation recovery testing."""
    tmp_dir = tempfile.mkdtemp()
    test_db = Path(tmp_dir) / "http_recovery_test.db"
    test_ws = Path(tmp_dir) / "workspace"
    test_ws.mkdir()

    monkeypatch.setenv("SAHA_DB_PATH", str(test_db))
    monkeypatch.setenv("SAHA_WORKSPACE_ROOT", str(test_ws))
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "false")
    monkeypatch.setenv("SAHA_DEMO_MODE", "true")
    monkeypatch.setenv("NEBIUS_API_KEY", "")

    init_db(str(test_db))
    check_and_init_data_dirs()
    return {"db": str(test_db), "ws": str(test_ws), "tmp": tmp_dir}


def test_interrupted_http_mutation_with_idempotency_reuses_stable_key(http_recovery_env):
    """Proof that an interrupted HTTP mutation step persists its idempotency key to SQLite
    and reuses the exact same key upon restart recovery instead of generating a new one."""
    db_path = http_recovery_env["db"]
    token = "owner_token_idem_key_test_32_chars!"
    owner_id = hash_owner_token(token)

    task = Ledger.create_task("Payment mutation with idempotency", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "Create invoice via external HTTP POST mutation",
                "tool": "http_request",
                "args": {
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "json": {"amount": 500, "currency": "USD"},
                    "supports_idempotency": True,
                },
                "probe": {"expected_status": [200, 201]},
                "depends_on": [],
            }
        ],
        db_path=db_path,
    )
    step1_id = steps[0].id

    recorded_http_calls = []

    def mock_http_request(url, method="GET", headers=None, data=None, json_data=None,
                          timeout=5.0, allowed_domains=None, idempotency_key=None,
                          idempotency_header="Idempotency-Key"):
        recorded_http_calls.append({
            "url": url,
            "method": method,
            "headers": headers,
            "idempotency_key": idempotency_key,
            "idempotency_header": idempotency_header,
        })
        return {
            "url": url,
            "method": method,
            "status_code": 201,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"id": "inv_123", "status": "created"}),
            "body_length": 42,
            "idempotency_key": idempotency_key,
        }

    # 1. Step starts execution: key is generated and persisted to SQLite step args before network call.
    # Simulate: process is killed / interrupted during the HTTP request before any response is recorded.
    def interrupt_during_request(url, method="GET", headers=None, data=None, json_data=None,
                                 timeout=5.0, allowed_domains=None, idempotency_key=None,
                                 idempotency_header="Idempotency-Key"):
        recorded_http_calls.append({
            "url": url,
            "method": method,
            "headers": headers,
            "idempotency_key": idempotency_key,
            "idempotency_header": idempotency_header,
        })
        raise KeyboardInterrupt("Simulated crash / SIGKILL mid-flight before HTTP response")

    with patch("backend.executor.http_request", side_effect=interrupt_during_request):
        try:
            execute_step(step1_id, db_path=db_path)
        except KeyboardInterrupt:
            pass

    # Verify the initial attempt sent the request and persisted the key before crash
    assert len(recorded_http_calls) == 1
    initial_key = recorded_http_calls[0]["idempotency_key"]
    assert initial_key is not None
    assert initial_key.startswith("idem_")

    crashed_step = Ledger.get_step(step1_id, db_path=db_path)
    # Step was left in 'running' status with NO result recorded
    assert crashed_step.status == "running"
    assert crashed_step.result_json is None

    # But the stable idempotency key WAS safely persisted in SQLite args_json!
    persisted_args = json.loads(crashed_step.args_json)
    assert persisted_args["idempotency_key"] == initial_key

    # Clear call records to verify ONLY calls made during restart recovery
    recorded_http_calls.clear()

    # 3. Process restart & recovery
    with patch("backend.executor.call_llm", return_value="Created invoice."),          patch("backend.executor.http_request", side_effect=mock_http_request):

        recovered_task = run_task(task.id, db_path=db_path)

    # 4. Verify recovery invariants:
    # A. One HTTP request was sent on retry
    assert len(recorded_http_calls) == 1
    # B. The retry MUST use the EXACT SAME stable idempotency key!
    retry_key = recorded_http_calls[0]["idempotency_key"]
    assert retry_key == initial_key, f"Expected identical key {initial_key}, got {retry_key}"

    # C. Task is complete and step is verified
    final_step = Ledger.get_step(step1_id, db_path=db_path)
    assert final_step.status == "verified"
    assert recovered_task.status == "complete"


def test_interrupted_mutation_without_deduplication_blocks_and_requires_authorization(http_recovery_env):
    """Proof that when an interrupted HTTP mutation does NOT support idempotency, recovery
    does NOT blindly re-execute it. It records an explicit ambiguous state and pauses for owner decision."""
    db_path = http_recovery_env["db"]
    token = "owner_token_ambig_test_32_chars__"
    owner_id = hash_owner_token(token)

    task = Ledger.create_task("Non-idempotent legacy transfer", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "Non-idempotent wire transfer mutation",
                "tool": "http_request",
                "args": {
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "data": "wire_transfer_data",
                    "supports_idempotency": False,  # Deduplication unavailable!
                },
                "probe": {"expected_status": 200},
                "depends_on": [],
            },
            {
                "idx": 2,
                "intent": "Subsequent report step",
                "tool": "write_file",
                "args": {"path": "report.txt", "content": "transfer done"},
                "probe": {},
                "depends_on": [1],
            },
        ],
        db_path=db_path,
    )
    s1_id = steps[0].id
    s2_id = steps[1].id

    # 1. Step 1 starts running and process is interrupted mid-flight
    Ledger.mark_step_running(s1_id, db_path=db_path)
    Ledger.update_task_status(task.id, "running", db_path=db_path)

    recorded_http_calls = []

    def mock_http_request(*args, **kwargs):
        recorded_http_calls.append(kwargs)
        return {"status_code": 200, "body": "ok", "url": "https://httpbin.org/post"}

    # 2. Invoke task recovery
    with patch("backend.executor.http_request", side_effect=mock_http_request):
        recovered_task = run_task(task.id, db_path=db_path)

    # 3. Verify safety invariants:
    # A. ZERO HTTP requests were sent! No blind retry!
    assert len(recorded_http_calls) == 0, "CRITICAL: Non-idempotent mutation was re-executed without authorization!"

    # B. Step 1 is in explicit 'blocked' state with explanatory block_reason
    s1 = Ledger.get_step(s1_id, db_path=db_path)
    assert s1.status == "blocked"
    assert s1.block_reason is not None
    assert "Ambiguous external HTTP mutation" in s1.block_reason
    assert "idempotency" in s1.block_reason.lower()

    # C. Step 2 was NOT executed
    s2 = Ledger.get_step(s2_id, db_path=db_path)
    assert s2.status == "pending"

    # D. An approval was created in the ledger requiring owner decision
    approvals = get_owner_approvals(owner_id, status="pending", db_path=db_path)
    assert len(approvals) == 1
    assert approvals[0]["step_id"] == s1_id
    assert approvals[0]["task_id"] == task.id

    # E. Task is paused waiting for reconciliation / approval
    assert recovered_task.status == "paused"


def test_ambiguous_mutation_owner_approval_decision_flow(http_recovery_env):
    """Test that an owner can explicitly authorize or deny resuming an ambiguous mutation."""
    db_path = http_recovery_env["db"]
    token = "owner_token_auth_flow_test_32_chars"
    owner_id = hash_owner_token(token)

    task = Ledger.create_task("Ambiguous mutation approval task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "External mutation requiring decision",
                "tool": "http_request",
                "args": {
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "supports_idempotency": False,
                },
                "probe": {"expected_status": 200},
                "depends_on": [],
            }
        ],
        db_path=db_path,
    )
    step_id = steps[0].id

    # Simulate crash in 'running'
    Ledger.mark_step_running(step_id, db_path=db_path)
    Ledger.recover_interrupted_step(Ledger.get_step(step_id, db_path=db_path), task.id, db_path=db_path)

    # Step is blocked
    s = Ledger.get_step(step_id, db_path=db_path)
    assert s.status == "blocked"
    approvals = get_owner_approvals(owner_id, status="pending", db_path=db_path)
    assert len(approvals) == 1
    appr_id = approvals[0]["id"]

    # Branch A: Owner approves the resume decision
    with patch("backend.executor.call_llm", return_value="Authorized execution."),          patch("backend.executor.http_request", return_value={"status_code": 200, "body": "ok", "url": "https://httpbin.org/post", "body_length": 2}):

        res = approve_request(appr_id, owner_id=owner_id, db_path=db_path)
        assert res["status"] == "approved"

        # Step is re-queued to pending with block_reason cleared
        s_after_approve = Ledger.get_step(step_id, db_path=db_path)
        assert s_after_approve.status == "pending"
        assert s_after_approve.block_reason is None

        # Task can now run to completion
        final_task = run_task(task.id, db_path=db_path)
        assert final_task.status == "complete"
        assert Ledger.get_step(step_id, db_path=db_path).status == "verified"


def test_ambiguous_mutation_owner_deny_flow(http_recovery_env):
    """Test that an owner denying an ambiguous mutation halts the task with status 'failed'."""
    db_path = http_recovery_env["db"]
    token = "owner_token_deny_flow_test_32_chars"
    owner_id = hash_owner_token(token)

    task = Ledger.create_task("Ambiguous mutation deny task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "Rejected external mutation",
                "tool": "http_request",
                "args": {"url": "https://httpbin.org/post", "method": "POST", "supports_idempotency": False},
                "probe": {},
                "depends_on": [],
            }
        ],
        db_path=db_path,
    )
    step_id = steps[0].id

    # Simulate crash in 'running'
    Ledger.mark_step_running(step_id, db_path=db_path)
    Ledger.recover_interrupted_step(Ledger.get_step(step_id, db_path=db_path), task.id, db_path=db_path)

    approvals = get_owner_approvals(owner_id, status="pending", db_path=db_path)
    appr_id = approvals[0]["id"]

    # Owner denies the request
    res = deny_request(appr_id, owner_id=owner_id, db_path=db_path)
    assert res["status"] == "denied"

    s_denied = Ledger.get_step(step_id, db_path=db_path)
    assert s_denied.status == "failed"
    assert "Denied" in (s_denied.error or "")


def test_completed_mutation_in_done_state_does_not_repeat_remote_call(http_recovery_env):
    """If an HTTP mutation completed and recorded its response before crash (status='done'),
    recovery runs deterministic verifier probe: marks verified without sending another request."""
    db_path = http_recovery_env["db"]
    owner_id = hash_owner_token("owner_token_done_state_http_32_c")

    task = Ledger.create_task("Done State HTTP Task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "Completed mutation before crash",
                "tool": "http_request",
                "args": {"url": "https://httpbin.org/post", "method": "POST"},
                "probe": {"expected_status": 201},
                "depends_on": [],
            }
        ],
        db_path=db_path,
    )
    s_id = steps[0].id

    # Record completed result into ledger (as executor would have done before crash)
    Ledger.mark_step_running(s_id, db_path=db_path)
    Ledger.mark_step_done(
        s_id,
        result={"url": "https://httpbin.org/post", "status_code": 201, "body": "ok", "body_length": 2},
        db_path=db_path,
    )

    recorded_http_calls = []

    def mock_http_request(*args, **kwargs):
        recorded_http_calls.append(kwargs)
        return {"status_code": 201, "body": "ok"}

    with patch("backend.executor.http_request", side_effect=mock_http_request):
        # Resume task
        final_task = run_task(task.id, db_path=db_path)

    # Invariant: NO new HTTP calls were made!
    assert len(recorded_http_calls) == 0, "Tool was called even though response was verifiably recorded!"
    assert final_task.status == "complete"
    assert Ledger.get_step(s_id, db_path=db_path).status == "verified"


def test_recovery_preserves_policy_on_resumed_http_mutation(http_recovery_env):
    """Resumed execution preserves full tool-policy enforcement: an unauthorized resumed
    mutation (e.g. unallowed domain) is blocked before any network call."""
    db_path = http_recovery_env["db"]
    token = "owner_token_policy_http_test_32_c"
    owner_id = hash_owner_token(token)

    task = Ledger.create_task("Policy HTTP Mutation Task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "Mutation to unallowed domain",
                "tool": "http_request",
                "args": {
                    "url": "https://unauthorized-payment.org/charge",
                    "method": "POST",
                    "supports_idempotency": True,
                },
                "probe": {},
                "depends_on": [],
            }
        ],
        db_path=db_path,
    )
    s_id = steps[0].id

    # Step crashed in running state
    Ledger.mark_step_running(s_id, db_path=db_path)

    recorded_http_calls = []

    def mock_http_request(*args, **kwargs):
        recorded_http_calls.append(kwargs)
        return {"status_code": 200}

    with patch("backend.executor.http_request", side_effect=mock_http_request):
        recovered_task = run_task(task.id, db_path=db_path)

    # Invariant: ZERO HTTP requests dispatched
    assert len(recorded_http_calls) == 0
    # Invariant: Policy blocked the action
    step = Ledger.get_step(s_id, db_path=db_path)
    assert step.status == "blocked"
    assert "not in policy allowed_domains" in (step.block_reason or "")
    assert recovered_task.status == "paused"
