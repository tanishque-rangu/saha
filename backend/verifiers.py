"""Deterministic Reality Probes for Saha.

Enforces the core rule:
Every step is verified by deterministic code checking reality, NEVER the LLM.
Probes:
- write_file: re-checks filesystem directly -> file exists + sha256 matches
- read_file:  file exists + is readable
- run_shell:  exit code 0 + expected output / artifact check
- http_get:   status code check + body validation
"""

import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

from backend.tools.files import compute_sha256
from backend.tools.http import http_get
from backend.tools.sandbox import resolve_workspace_path
from backend.tools.shell import run_shell


def verify_write_file(
    args: Dict[str, Any],
    result: Optional[Dict[str, Any]] = None,
    probe_spec: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify write_file: checks disk directly for file existence and SHA256 match."""
    path = args.get("path")
    if not path:
        return {"passed": False, "error": "Missing 'path' in step args", "details": {}}

    try:
        target = resolve_workspace_path(path, task_id=task_id)
    except Exception as e:
        return {"passed": False, "error": f"Invalid path resolution: {e}", "details": {"path": path}}

    if not target.exists():
        return {
            "passed": False,
            "error": f"File does not exist in workspace: {path}",
            "details": {"path": path, "exists": False},
        }

    if not target.is_file():
        return {
            "passed": False,
            "error": f"Path is not a regular file: {path}",
            "details": {"path": path, "is_file": False},
        }

    # Read bytes from physical disk
    actual_bytes = target.read_bytes()
    actual_sha256 = compute_sha256(actual_bytes)

    # Expected SHA256 comes from probe_spec, result (what worker wrote), or content
    expected_sha256 = None
    if probe_spec and probe_spec.get("expected_sha256"):
        expected_sha256 = probe_spec["expected_sha256"]
    elif result and result.get("sha256"):
        expected_sha256 = result["sha256"]
    elif "content" in args:
        expected_sha256 = compute_sha256(str(args["content"]).encode("utf-8"))

    if expected_sha256 and actual_sha256 != expected_sha256:
        return {
            "passed": False,
            "error": f"SHA256 mismatch: expected {expected_sha256}, got {actual_sha256}",
            "details": {
                "path": path,
                "exists": True,
                "expected_sha256": expected_sha256,
                "actual_sha256": actual_sha256,
                "size": len(actual_bytes),
            },
        }

    return {
        "passed": True,
        "details": {
            "path": path,
            "exists": True,
            "sha256": actual_sha256,
            "size": len(actual_bytes),
        },
    }


def verify_read_file(
    args: Dict[str, Any],
    result: Optional[Dict[str, Any]] = None,
    probe_spec: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify read_file: checks that file exists and can be read."""
    path = args.get("path")
    if not path:
        return {"passed": False, "error": "Missing 'path' in step args", "details": {}}

    try:
        target = resolve_workspace_path(path, task_id=task_id)
    except Exception as e:
        return {"passed": False, "error": str(e), "details": {"path": path}}

    if not target.is_file():
        return {
            "passed": False,
            "error": f"File does not exist or is unreadable: {path}",
            "details": {"path": path, "exists": False},
        }

    actual_bytes = target.read_bytes()
    return {
        "passed": True,
        "details": {
            "path": path,
            "exists": True,
            "sha256": compute_sha256(actual_bytes),
            "size": len(actual_bytes),
        },
    }


def verify_run_shell(
    args: Dict[str, Any],
    result: Optional[Dict[str, Any]] = None,
    probe_spec: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify run_shell: checks exit code 0 and expected outputs/side effects."""
    cmd = args.get("cmd", "")
    spec = probe_spec or {}

    # If the command was supposed to create a file, verify that the file exists in reality!
    check_file = spec.get("expected_file")
    if check_file:
        try:
            target = resolve_workspace_path(check_file, task_id=task_id)
            if not target.exists():
                return {
                    "passed": False,
                    "error": f"Expected output file '{check_file}' was not found in task workspace",
                    "details": {"expected_file": check_file, "exists": False},
                }
        except Exception as e:
            return {"passed": False, "error": str(e), "details": {}}

    # Validate execution result
    if not result:
        return {"passed": False, "error": "No execution result recorded for shell command", "details": {}}

    exit_code = result.get("exit_code", -1)
    if exit_code != 0:
        return {
            "passed": False,
            "error": f"Command exited with non-zero exit code: {exit_code}. Stderr: {result.get('stderr', '')}",
            "details": result,
        }

    # Verify expected output substring if specified
    expected_substr = spec.get("expected_output")
    if expected_substr:
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        if expected_substr not in stdout and expected_substr not in stderr:
            return {
                "passed": False,
                "error": f"Expected output substring '{expected_substr}' not found in stdout/stderr",
                "details": {"stdout": stdout, "stderr": stderr},
            }

    return {
        "passed": True,
        "details": {
            "cmd": cmd,
            "exit_code": exit_code,
            "checked_file": check_file,
        },
    }


def verify_http_get(
    args: Dict[str, Any],
    result: Optional[Dict[str, Any]] = None,
    probe_spec: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify http_get: status code and body checks."""
    url = args.get("url")
    if not url:
        return {"passed": False, "error": "Missing 'url' in step args", "details": {}}

    spec = probe_spec or {}
    expected_status = spec.get("expected_status", 200)

    if not result:
        return {"passed": False, "error": "No result recorded for http_get step", "details": {}}

    status = result.get("status_code")
    if status != expected_status:
        return {
            "passed": False,
            "error": f"HTTP status code mismatch: expected {expected_status}, got {status}",
            "details": {"status_code": status, "url": url},
        }

    expected_body_substr = spec.get("expected_body_substring")
    if expected_body_substr:
        body = result.get("body", "")
        if expected_body_substr not in body:
            return {
                "passed": False,
                "error": f"Expected substring '{expected_body_substr}' not found in HTTP body",
                "details": {"url": url},
            }

    return {
        "passed": True,
        "details": {"url": url, "status_code": status},
    }


def verify_list_files(
    args: Dict[str, Any],
    result: Optional[Dict[str, Any]] = None,
    probe_spec: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Verify list_files: checks that path exists and was listed."""
    path = args.get("path", ".")
    try:
        target = resolve_workspace_path(path, task_id=task_id)
    except Exception as e:
        return {"passed": False, "error": f"Invalid path resolution: {e}", "details": {"path": path}}

    if not target.exists():
        return {
            "passed": False,
            "error": f"Target path does not exist: {path}",
            "details": {"path": path},
        }

    return {
        "passed": True,
        "details": {"path": path, "count": result.get("count", 0) if result else 0},
    }


def verify_step(
    tool: str,
    args: Dict[str, Any],
    result: Optional[Dict[str, Any]] = None,
    probe_spec: Optional[Dict[str, Any]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch step verification to the deterministic probe for the tool."""
    dispatch = {
        "write_file": lambda: verify_write_file(args, result, probe_spec, task_id=task_id),
        "read_file": lambda: verify_read_file(args, result, probe_spec, task_id=task_id),
        "run_shell": lambda: verify_run_shell(args, result, probe_spec, task_id=task_id),
        "http_get": lambda: verify_http_get(args, result, probe_spec, task_id=task_id),
        "list_files": lambda: verify_list_files(args, result, probe_spec, task_id=task_id),
    }

    verifier = dispatch.get(tool)
    if not verifier:
        return {
            "passed": False,
            "error": f"No verifier probe implemented for tool '{tool}'",
            "details": {},
        }
    try:
        res = verifier()
        res["tool"] = tool
        return res
    except Exception as e:
        return {
            "passed": False,
            "tool": tool,
            "error": f"Verifier probe exception: {e}",
            "details": {},
        }
