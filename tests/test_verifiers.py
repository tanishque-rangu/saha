"""Tests for deterministic reality verifier probes."""

from pathlib import Path
import pytest

from backend.tools.files import write_file
from backend.tools.sandbox import resolve_workspace_path
from backend.verifiers import verify_http_get, verify_read_file, verify_run_shell, verify_step, verify_write_file


def test_verify_write_file_probe():
    # 1. Successful verification
    write_res = write_file("probe_test.txt", "verified payload")
    probe = verify_write_file(
        args={"path": "probe_test.txt", "content": "verified payload"},
        result=write_res,
        probe_spec={"expected_sha256": write_res["sha256"]},
    )
    assert probe["passed"] is True
    assert probe["details"]["exists"] is True

    # 2. File deleted in reality (Sabotage simulation)
    file_path = resolve_workspace_path("probe_test.txt")
    file_path.unlink()

    probe_missing = verify_write_file(
        args={"path": "probe_test.txt", "content": "verified payload"},
        result=write_res,
        probe_spec={"expected_sha256": write_res["sha256"]},
    )
    assert probe_missing["passed"] is False
    assert "does not exist" in probe_missing["error"]

    # 3. File content tampered with (SHA mismatch)
    write_file("probe_test.txt", "tampered content")
    probe_tampered = verify_write_file(
        args={"path": "probe_test.txt", "content": "verified payload"},
        result=write_res,
        probe_spec={"expected_sha256": write_res["sha256"]},
    )
    assert probe_tampered["passed"] is False
    assert "SHA256 mismatch" in probe_tampered["error"]

    # Cleanup
    if file_path.exists():
        file_path.unlink()


def test_verify_run_shell_probe():
    # Success with exit code 0 and expected output
    probe_ok = verify_run_shell(
        args={"cmd": "echo test_probe"},
        result={"exit_code": 0, "stdout": "test_probe\n", "stderr": ""},
        probe_spec={"expected_output": "test_probe"},
    )
    assert probe_ok["passed"] is True

    # Failure with non-zero exit code
    probe_fail = verify_run_shell(
        args={"cmd": "python -c \"import sys; sys.exit(1)\""},
        result={"exit_code": 1, "stdout": "", "stderr": "Error"},
        probe_spec={},
    )
    assert probe_fail["passed"] is False
    assert "exit code: 1" in probe_fail["error"]

    # Failure with missing expected output substring
    probe_substr_fail = verify_run_shell(
        args={"cmd": "echo hello"},
        result={"exit_code": 0, "stdout": "hello", "stderr": ""},
        probe_spec={"expected_output": "world"},
    )
    assert probe_substr_fail["passed"] is False
    assert "Expected output substring" in probe_substr_fail["error"]


def test_verify_http_get_probe():
    # Success status check
    probe_ok = verify_http_get(
        args={"url": "https://example.com"},
        result={"status_code": 200, "body": "Example Domain"},
        probe_spec={"expected_status": 200, "expected_body_substring": "Example"},
    )
    assert probe_ok["passed"] is True

    # Status mismatch
    probe_mismatch = verify_http_get(
        args={"url": "https://example.com"},
        result={"status_code": 404, "body": "Not Found"},
        probe_spec={"expected_status": 200},
    )
    assert probe_mismatch["passed"] is False
    assert "status code mismatch" in probe_mismatch["error"]


def test_verify_step_dispatcher():
    # Test unified dispatch
    res = verify_step(
        tool="unknown_tool",
        args={},
        result={},
        probe_spec={},
    )
    assert res["passed"] is False
    assert "No verifier probe" in res["error"]
