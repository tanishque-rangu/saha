"""Focused tests for HTTP mutation recovery under process interruption.

Tests the following critical recovery requirements & trust boundaries:
1. Planner trust boundary: planner-supplied flags (supports_idempotency: True)
   CANNOT authorize automatic retry of an ambiguous external mutation.
2. Planner trust boundary: planner-supplied headers (Idempotency-Key)
   CANNOT authorize automatic retry of an ambiguous external mutation.
3. Trusted owner configuration: setting policy.trusted_idempotency_domains DOES
   enable stable idempotency key persistence and identical reuse across retries.
4. Ambiguous mutation approval message: approval request reason clearly explains
   that the remote outcome may be unknown and approving will re-dispatch (retry)
   the mutation while denying will fail the step without network calls.
5. Owner decision execution: approving resumes and executes the mutation;
   denying marks the step failed with zero network requests.
6. Pre-crash completed mutation ('done' state with recorded result): probe verifies
   without repeating the external HTTP mutation.
7. Policy preservation: resumed HTTP actions strictly enforce policy and domain
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
from backend.tools.http import is_http_mutation_step, is_trusted_idempotency_target, target_supports_idempotency


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


def test_planner_supplied_flags_cannot_authorize_automatic_retry(http_recovery_env):
    """TRUST BOUNDARY TEST:
    A planner-generated step setting supports_idempotency: True CANNOT authorize automatic
    retry of an ambiguous mutation if the domain is not in owner policy trusted_idempotency_domains.
    """
    db_path = http_recovery_env["db"]
    token = "owner_token_planner_flag_test_32_c"
    owner_id = hash_owner_token(token)

    # Ensure owner policy has domain in allowed_domains, but NOT in trusted_idempotency_domains
    policy = get_owner_policy(owner_id, db_path=db_path)
    policy.allowed_domains = ["httpbin.org"]
    policy.trusted_idempotency_domains = []  # NOT trusted by owner!
    set_owner_policy(owner_id, policy, db_path=db_path)

    task = Ledger.create_task("Untrusted planner flag task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "Mutation with planner claiming idempotency support",
                "tool": "http_request",
                "args": {
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "json": {"action": "transfer"},
                    "supports_idempotency": True,      # Planner output (UNTRUSTED)
                    "idempotency_supported": True,     # Planner output (UNTRUSTED)
                },
                "probe": {"expected_status": 200},
                "depends_on": [],
            }
        ],
        db_path=db_path,
    )
    step_id = steps[0].id

    # Simulate interruption during flight before response was recorded
    Ledger.mark_step_running(step_id, db_path=db_path)
    Ledger.update_task_status(task.id, "running", db_path=db_path)

    recorded_calls = []

    def mock_http_request(*args, **kwargs):
        recorded_calls.append(kwargs)
        return {"status_code": 200, "body": "ok"}

    with patch("backend.executor.http_request", side_effect=mock_http_request):
        recovered_task = run_task(task.id, db_path=db_path)

    # INVARIANTS:
    # 1. ZERO HTTP calls were made! Planner flag was REJECTED as an authorization to retry.
    assert len(recorded_calls) == 0, "CRITICAL: Planner-supplied flag authorized automatic retry!"
    # 2. Step was safely blocked
    crashed_step = Ledger.get_step(step_id, db_path=db_path)
    assert crashed_step.status == "blocked"
    assert "Ambiguous external HTTP mutation" in (crashed_step.block_reason or "")
    assert "not confirmed by trusted owner policy" in (crashed_step.block_reason or "")
    # 3. Task paused
    assert recovered_task.status == "paused"
    # 4. Approval request logged
    approvals = get_owner_approvals(owner_id, status="pending", db_path=db_path)
    assert len(approvals) == 1
    assert approvals[0]["step_id"] == step_id


def test_planner_supplied_headers_cannot_authorize_automatic_retry(http_recovery_env):
    """TRUST BOUNDARY TEST:
    A planner-generated step supplying an Idempotency-Key header CANNOT authorize automatic
    retry of an ambiguous mutation if the domain is not in owner policy trusted_idempotency_domains.
    """
    db_path = http_recovery_env["db"]
    token = "owner_token_planner_hdr_test_32_ch"
    owner_id = hash_owner_token(token)

    # Owner policy does NOT trust httpbin.org for idempotency
    policy = get_owner_policy(owner_id, db_path=db_path)
    policy.allowed_domains = ["httpbin.org"]
    policy.trusted_idempotency_domains = []
    set_owner_policy(owner_id, policy, db_path=db_path)

    task = Ledger.create_task("Untrusted planner header task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "Mutation with planner supplying idempotency header",
                "tool": "http_request",
                "args": {
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "headers": {"Idempotency-Key": "untrusted-planner-hallucinated-key"},
                    "idempotency_key": "untrusted-planner-key",
                },
                "probe": {"expected_status": 200},
                "depends_on": [],
            }
        ],
        db_path=db_path,
    )
    step_id = steps[0].id

    # Simulate interruption mid-flight
    Ledger.mark_step_running(step_id, db_path=db_path)
    Ledger.update_task_status(task.id, "running", db_path=db_path)

    recorded_calls = []
    with patch("backend.executor.http_request", side_effect=lambda *a, **kw: recorded_calls.append(kw)):
        recovered_task = run_task(task.id, db_path=db_path)

    # Invariants: 0 calls, blocked, approval created
    assert len(recorded_calls) == 0, "Planner header authorized automatic retry!"
    step = Ledger.get_step(step_id, db_path=db_path)
    assert step.status == "blocked"
    assert "not confirmed by trusted owner policy" in (step.block_reason or "")
    assert recovered_task.status == "paused"


def test_trusted_owner_configuration_enables_stable_key_reuse(http_recovery_env):
    """Proof that when the owner explicitly configures a domain in trusted_idempotency_domains,
    a stable idempotency key is persisted to SQLite and reused identically upon restart retry."""
    db_path = http_recovery_env["db"]
    token = "owner_token_trusted_cfg_test_32_ch"
    owner_id = hash_owner_token(token)

    # Explicitly configure trusted domain in owner policy
    policy = get_owner_policy(owner_id, db_path=db_path)
    policy.allowed_domains = ["httpbin.org"]
    policy.trusted_idempotency_domains = ["httpbin.org"]
    set_owner_policy(owner_id, policy, db_path=db_path)

    task = Ledger.create_task("Trusted idempotency task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "Mutation to trusted idempotency target",
                "tool": "http_request",
                "args": {
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "json": {"item": "widget", "qty": 2},
                },
                "probe": {"expected_status": [200, 201]},
                "depends_on": [],
            }
        ],
        db_path=db_path,
    )
    step1_id = steps[0].id

    recorded_calls = []

    def interrupt_during_request(url, method="GET", headers=None, data=None, json_data=None,
                                 timeout=5.0, allowed_domains=None, idempotency_key=None,
                                 idempotency_header="Idempotency-Key"):
        recorded_calls.append({
            "url": url,
            "method": method,
            "headers": headers,
            "idempotency_key": idempotency_key,
            "idempotency_header": idempotency_header,
        })
        raise KeyboardInterrupt("Simulated crash mid-flight before HTTP response")

    # 1. Step starts running: stable key is generated and saved to SQLite before network call
    with patch("backend.executor.http_request", side_effect=interrupt_during_request):
        try:
            execute_step(step1_id, db_path=db_path)
        except KeyboardInterrupt:
            pass

    assert len(recorded_calls) == 1
    initial_key = recorded_calls[0]["idempotency_key"]
    assert initial_key is not None
    assert initial_key.startswith("idem_")

    # The stable key was saved in SQLite
    crashed_step = Ledger.get_step(step1_id, db_path=db_path)
    assert crashed_step.status == "running"
    persisted_args = json.loads(crashed_step.args_json)
    assert persisted_args["idempotency_key"] == initial_key

    # 2. Restart and recover
    recorded_calls.clear()

    def successful_request(url, method="GET", headers=None, data=None, json_data=None,
                           timeout=5.0, allowed_domains=None, idempotency_key=None,
                           idempotency_header="Idempotency-Key"):
        recorded_calls.append({"idempotency_key": idempotency_key})
        return {
            "url": url,
            "method": method,
            "status_code": 201,
            "headers": {"content-type": "application/json"},
            "body": json.dumps({"id": "widget_1", "status": "created"}),
            "body_length": 38,
            "idempotency_key": idempotency_key,
        }

    with patch("backend.executor.call_llm", return_value="Created widget."),          patch("backend.executor.http_request", side_effect=successful_request):

        final_task = run_task(task.id, db_path=db_path)

    # Invariants:
    # 1. The retry sent exactly one request
    assert len(recorded_calls) == 1
    # 2. The retry reused the EXACT SAME idempotency key
    assert recorded_calls[0]["idempotency_key"] == initial_key
    # 3. Step is verified and task is complete
    assert final_task.status == "complete"
    assert Ledger.get_step(step1_id, db_path=db_path).status == "verified"


def test_ambiguous_mutation_approval_reason_clarity_and_decision_flow(http_recovery_env):
    """Test that the approval request clearly explains the unknown remote outcome and retry risk,
    and that approving resumes/retries while denying fails without network calls."""
    db_path = http_recovery_env["db"]
    token = "owner_token_appr_clarity_test_32_"
    owner_id = hash_owner_token(token)

    task = Ledger.create_task("Approval clarity task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "High value bank transfer",
                "tool": "http_request",
                "args": {
                    "url": "https://httpbin.org/post",
                    "method": "POST",
                    "json": {"transfer": 100000},
                },
                "probe": {"expected_status": 200},
                "depends_on": [],
            }
        ],
        db_path=db_path,
    )
    step_id = steps[0].id

    # Crash during flight
    Ledger.mark_step_running(step_id, db_path=db_path)
    Ledger.recover_interrupted_step(Ledger.get_step(step_id, db_path=db_path), task.id, db_path=db_path)

    approvals = get_owner_approvals(owner_id, status="pending", db_path=db_path)
    assert len(approvals) == 1
    reason = approvals[0]["reason"]

    # Verify clarity of approval reason
    assert "Ambiguous external HTTP mutation (POST https://httpbin.org/post)" in reason
    assert "remote outcome may already have taken effect or may be unknown" in reason
    assert "Approving this request will re-dispatch (retry) the mutation" in reason
    assert "denying it will mark the step failed without re-sending the request" in reason

    # Branch A: Owner approves -> resumes and retries
    with patch("backend.executor.call_llm", return_value="Transfer executed."),          patch("backend.executor.http_request", return_value={"status_code": 200, "body": "ok", "url": "https://httpbin.org/post", "body_length": 2}):

        res = approve_request(approvals[0]["id"], owner_id=owner_id, db_path=db_path)
        assert res["status"] == "approved"

        final_task = run_task(task.id, db_path=db_path)
        assert final_task.status == "complete"
        assert Ledger.get_step(step_id, db_path=db_path).status == "verified"


def test_ambiguous_mutation_owner_deny_prevents_network_dispatch(http_recovery_env):
    """Test that denying an ambiguous mutation halts the task with status 'failed'
    and dispatches ZERO network requests."""
    db_path = http_recovery_env["db"]
    token = "owner_token_deny_prevents_dispatch_3"
    owner_id = hash_owner_token(token)

    task = Ledger.create_task("Deny prevents dispatch task", owner_id=owner_id, db_path=db_path)
    steps = Ledger.add_steps(
        task.id,
        [
            {
                "idx": 1,
                "intent": "Ambiguous operation to deny",
                "tool": "http_request",
                "args": {"url": "https://httpbin.org/post", "method": "POST"},
                "probe": {},
                "depends_on": [],
            }
        ],
        db_path=db_path,
    )
    step_id = steps[0].id

    Ledger.mark_step_running(step_id, db_path=db_path)
    Ledger.recover_interrupted_step(Ledger.get_step(step_id, db_path=db_path), task.id, db_path=db_path)

    approvals = get_owner_approvals(owner_id, status="pending", db_path=db_path)
    appr_id = approvals[0]["id"]

    recorded_calls = []
    with patch("backend.executor.http_request", side_effect=lambda *a, **kw: recorded_calls.append(kw)):
        deny_res = deny_request(appr_id, owner_id=owner_id, db_path=db_path)
        final_task = run_task(task.id, db_path=db_path)

    # ZERO network calls dispatched
    assert len(recorded_calls) == 0
    assert deny_res["status"] == "denied"
    assert Ledger.get_step(step_id, db_path=db_path).status == "failed"
    assert final_task.status == "failed"


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

    # Owner policy trusts the domain for idempotency, but explicitly disallows it in allowed_domains
    policy = get_owner_policy(owner_id, db_path=db_path)
    policy.allowed_domains = ["httpbin.org"]  # unauthorized-payment.org is forbidden!
    policy.trusted_idempotency_domains = ["unauthorized-payment.org"]
    set_owner_policy(owner_id, policy, db_path=db_path)

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
