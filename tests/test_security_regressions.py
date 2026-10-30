"""Security regression tests verifying that all audit attack vectors are blocked."""

import os
import tempfile
from pathlib import Path
import pytest
import httpx

from backend.tools.files import read_file, write_file
from backend.tools.http import http_get
from backend.tools.sandbox import get_workspace_dir, resolve_workspace_path
from backend.tools.shell import run_shell
from backend.planner import generate_plan


def test_shell_blocks_inline_python_dash_c():
    """python -c must be rejected."""
    with pytest.raises(PermissionError):
        run_shell('python -c "print(1)"')


def test_shell_blocks_python_outside_workspace():
    """python ../outside.py must be rejected."""
    with pytest.raises(PermissionError):
        run_shell("python ../outside.py")


def test_shell_blocks_chaining_and_metacharacters():
    """; && | > $() metacharacters must be rejected."""
    for cmd in [
        "echo hi ; echo evil",
        "echo first && echo second",
        "echo piped | cat",
        "echo redirected > file.txt",
        "echo $(whoami)",
        "echo `id`",
    ]:
        with pytest.raises(PermissionError):
            run_shell(cmd)


def test_shell_blocks_disallowed_programs():
    """git, find, dir, type must be rejected."""
    for cmd in ["git status", "find .", "dir", "type file.txt"]:
        with pytest.raises(PermissionError):
            run_shell(cmd)


def test_shell_blocks_path_escapes_in_args():
    """Absolute, UNC, and drive-relative paths in arguments must be rejected."""
    with pytest.raises(PermissionError):
        run_shell(r"cat C:\Windows\win.ini")

    with pytest.raises(PermissionError):
        run_shell(r"cat \\server\share\file.txt")

    with pytest.raises(PermissionError):
        run_shell("cat C:test.txt")


def test_shell_blocks_unc_paths_all_representations():
    """UNC paths in unquoted, double-quoted, single-quoted, and forward-slash formats must be rejected."""
    prohibited_unc_commands = [
        r"cat \\server\share\file.txt",
        'cat "\\\\server\\share\\file.txt"',
        "cat '\\\\server\\share\\file.txt'",
        r"cat \\\\server\\\\share\\\\file.txt",
        'cat "\\\\server\\\\share\\\\file.txt"',
        "cat //server/share/file.txt",
        'cat "//server/share/file.txt"',
        "cat '//server/share/file.txt'",
        r"ls \\server\share",
        "ls //server/share",
        r"head -n 5 \\server\share\file.txt",
        r"tail -n 5 \\server\share\file.txt",
        r"grep pattern \\server\share\file.txt",
    ]
    for cmd in prohibited_unc_commands:
        with pytest.raises(PermissionError):
            run_shell(cmd)


def test_shell_blocks_tokenization_and_drive_escapes():
    """Drive paths, escaped backslashes, and tokenization differences must be rejected."""
    prohibited_commands = [
        r"cat C:\Windows\system32\cmd.exe",
        'cat "C:\\Windows\\win.ini"',
        "cat 'C:\\Windows\\win.ini'",
        "cat D:relative.txt",
        'cat "D:relative.txt"',
        "cat ../secret.txt",
        'cat "../secret.txt"',
        "cat '..\\secret.txt'",
    ]
    for cmd in prohibited_commands:
        with pytest.raises(PermissionError):
            run_shell(cmd)


def test_shell_allows_normal_valid_commands():
    """Normal safe commands with legitimate arguments must execute without false positives."""
    tid = "test_shell_valid_cmds"
    ws = get_workspace_dir(tid)
    test_file = ws / "sample.txt"
    test_file.write_text("line 1\nline 2\nline 3\npattern target\nline 5\n", encoding="utf-8")

    try:
        # Echo
        res_echo = run_shell("echo hello world", task_id=tid)
        assert res_echo["exit_code"] == 0
        assert "hello world" in res_echo["stdout"]

        # Cat unquoted and quoted
        res_cat = run_shell("cat sample.txt", task_id=tid)
        assert res_cat["exit_code"] == 0
        assert "pattern target" in res_cat["stdout"]

        res_cat_q = run_shell('cat "sample.txt"', task_id=tid)
        assert res_cat_q["exit_code"] == 0

        # Ls
        res_ls = run_shell("ls", task_id=tid)
        assert res_ls["exit_code"] == 0
        assert "sample.txt" in res_ls["stdout"]

        # Head and Tail
        res_head = run_shell("head -n 2 sample.txt", task_id=tid)
        assert res_head["exit_code"] == 0
        assert "line 1" in res_head["stdout"]

        res_tail = run_shell("tail -n 2 sample.txt", task_id=tid)
        assert res_tail["exit_code"] == 0
        assert "line 5" in res_tail["stdout"]

        # Grep
        res_grep = run_shell('grep "pattern" sample.txt', task_id=tid)
        assert res_grep["exit_code"] == 0
        assert "pattern target" in res_grep["stdout"]
    finally:
        if test_file.exists():
            test_file.unlink()


def test_child_environment_scrubs_api_key():
    """Child python subprocesses must NEVER inherit NEBIUS_API_KEY."""
    tid = "test_env_scrub"
    script_name = "leak_check.py"
    ws = get_workspace_dir(tid)
    script_path = ws / script_name

    # Write a test script inside task workspace
    script_path.write_text(
        'import os\n'
        'key = os.environ.get("NEBIUS_API_KEY", "")\n'
        'print("KEY_FOUND=" + str(bool(key)))\n',
        encoding="utf-8",
    )

    # Set dummy secret in current environment
    old_val = os.environ.get("NEBIUS_API_KEY")
    os.environ["NEBIUS_API_KEY"] = "super_secret_test_token"
    try:
        res = run_shell(f"python {script_name}", task_id=tid)
        assert res["exit_code"] == 0
        assert "KEY_FOUND=False" in res["stdout"].strip()
    finally:
        if old_val is not None:
            os.environ["NEBIUS_API_KEY"] = old_val
        else:
            os.environ.pop("NEBIUS_API_KEY", None)
        if script_path.exists():
            script_path.unlink()


def test_http_get_ssrf_blocking():
    """http_get must reject loopback, localhost, and link-local addresses."""
    # 127.0.0.1
    res1 = http_get("http://127.0.0.1:8080/test")
    assert res1["status_code"] == -1
    assert "SSRF blocked" in res1["error"]

    # localhost
    res2 = http_get("http://localhost:8080/test")
    assert res2["status_code"] == -1
    assert "SSRF blocked" in res2["error"]

    # 169.254.169.254 (Cloud metadata)
    res3 = http_get("http://169.254.169.254/latest/meta-data")
    assert res3["status_code"] == -1
    assert "SSRF blocked" in res3["error"]


def test_http_get_redirect_to_private_ip(monkeypatch):
    """http_get must block redirects targeting private IPs."""
    # Create a mock response for first hop that redirects to 127.0.0.1
    class MockRedirectClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def stream(self, method, url, **kwargs):
            class MockStreamResponse:
                def __init__(self, status, headers):
                    self.status_code = status
                    self.headers = headers
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def iter_bytes(self):
                    yield b""

            if "example.com" in url:
                return MockStreamResponse(302, {"Location": "http://127.0.0.1:8080/evil"})
            return MockStreamResponse(200, {})

    monkeypatch.setattr(httpx, "Client", MockRedirectClient)
    res = http_get("http://example.com/start")
    assert res["status_code"] == -1
    assert "SSRF blocked" in res["error"]


def test_http_get_size_cap_exceeded(monkeypatch):
    """http_get must reject responses exceeding 1 MB."""
    class MockLargeClient:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def stream(self, method, url, **kwargs):
            class MockStreamLarge:
                def __init__(self):
                    self.status_code = 200
                    self.headers = {}
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    pass
                def iter_bytes(self):
                    # Yield chunks exceeding 1 MB
                    chunk = b"X" * (512 * 1024)
                    yield chunk
                    yield chunk
                    yield chunk
            return MockStreamLarge()

    monkeypatch.setattr(httpx, "Client", MockLargeClient)
    res = http_get("http://example.com/large")
    assert "1 mb limit" in res["error"].lower()


def test_public_mode_restrictions(monkeypatch):
    """In SAHA_PUBLIC_MODE=true, run_shell is forbidden and plan step count is capped."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")

    # run_shell must be blocked
    with pytest.raises(PermissionError) as exc_info:
        run_shell("echo test")
    assert "disabled in SAHA_PUBLIC_MODE" in str(exc_info.value)


def test_public_mode_http_allowlist(monkeypatch):
    """In SAHA_PUBLIC_MODE=true, domains not in SAHA_HTTP_ALLOWLIST are rejected."""
    monkeypatch.setenv("SAHA_PUBLIC_MODE", "true")
    monkeypatch.setenv("SAHA_HTTP_ALLOWLIST", "example.com,httpbin.org")

    # Disallowed domain
    res = http_get("http://unauthorized-domain.org/test")
    assert res["status_code"] == -1
    assert "Public mode restriction" in res["error"]


def test_daily_llm_call_cap(monkeypatch):
    """Exceeding SAHA_DAILY_LLM_CALL_CAP raises 429-style RuntimeError."""
    from backend.db import check_daily_llm_limit, init_db, record_llm_call
    with tempfile.TemporaryDirectory() as tmp_dir:
        test_db = str(Path(tmp_dir) / "test_cap.db")
        init_db(test_db)
        monkeypatch.setenv("SAHA_DAILY_LLM_CALL_CAP", "2")

        # Record 2 calls
        record_llm_call("model-1", 100.0, role="worker", status="success", db_path=test_db)
        record_llm_call("model-1", 100.0, role="worker", status="success", db_path=test_db)

        # 3rd call should trigger daily cap
        with pytest.raises(RuntimeError) as exc_info:
            check_daily_llm_limit(db_path=test_db)
        assert "429: Rate limit exceeded" in str(exc_info.value)
