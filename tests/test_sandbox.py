"""Security and sandbox validation tests."""

from pathlib import Path
import pytest

from backend.tools.files import read_file, write_file
from backend.tools.sandbox import get_workspace_dir, resolve_workspace_path
from backend.tools.shell import run_shell


def test_path_traversal_rejection():
    # Relative path escaping workspace
    with pytest.raises(PermissionError):
        resolve_workspace_path("../outside.txt")

    with pytest.raises(PermissionError):
        resolve_workspace_path("sub/../../outside.txt")

    with pytest.raises(PermissionError):
        resolve_workspace_path("..\\outside.txt")

    # Absolute path escaping workspace
    outside_abs = str(Path(__file__).resolve().parent.parent / "secret.key")
    with pytest.raises(PermissionError):
        resolve_workspace_path(outside_abs)

    # Drive-relative path rejection
    with pytest.raises(PermissionError):
        resolve_workspace_path("C:test.txt")

    # UNC path rejection
    with pytest.raises(PermissionError):
        resolve_workspace_path(r"\\server\share\file.txt")


def test_valid_workspace_resolution():
    ws = get_workspace_dir()
    resolved = resolve_workspace_path("test_dir/file.txt")
    assert resolved.is_relative_to(ws)
    assert resolved.name == "file.txt"


def test_sandboxed_file_ops():
    # Write and read safely
    res_write = write_file("sandbox_test.txt", "safe content")
    assert res_write["bytes_written"] == len("safe content")

    res_read = read_file("sandbox_test.txt")
    assert res_read["content"] == "safe content"

    # Attempting to read outside workspace must raise PermissionError
    with pytest.raises(PermissionError):
        read_file("../../outside.txt")


def test_native_shell_echo_and_cat():
    # Write file
    write_file("test_echo.txt", "line 1\nline 2\n")

    res_echo = run_shell("echo hello world")
    assert res_echo["exit_code"] == 0
    assert res_echo["stdout"].strip() == "hello world"

    res_cat = run_shell("cat test_echo.txt")
    assert res_cat["exit_code"] == 0
    assert "line 1" in res_cat["stdout"]


def test_shell_disallowed_programs():
    with pytest.raises(PermissionError):
        run_shell("rm -rf something")

    with pytest.raises(PermissionError):
        run_shell("git status")

    with pytest.raises(PermissionError):
        run_shell("find .")
