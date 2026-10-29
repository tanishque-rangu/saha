"""Sandboxed shell execution with native Python commands and scrubbed subprocess.

Guarantees:
- shell=True removed everywhere. No cmd.exe / PowerShell / bash involvement.
- ls, cat, head, tail, grep, echo implemented as native Python functions strictly
  confined to the task workspace.
- Allowlist removes git, find, type, dir.
- ONE subprocess capability: `python <script.py>` running with `[sys.executable, "-I", script]`,
  cwd=task workspace, 64 KB output cap, and scrubbed environment (never NEBIUS_API_KEY).
- Strict rejection of shell metacharacters (; | & > < ` $ ( ) ), "..", UNC, absolute,
  and drive-relative paths.
- Disabled entirely if SAHA_PUBLIC_MODE=true.
"""

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.tools.sandbox import get_workspace_dir, is_drive_relative, is_unc_path, resolve_workspace_path

FORBIDDEN_CHARS = {";", "|", "&", ">", "<", "`", "$", "(", ")"}
MAX_OUTPUT_BYTES = 64 * 1024  # 64 KB cap


def is_public_mode() -> bool:
    """Return True if SAHA_PUBLIC_MODE is active."""
    return os.getenv("SAHA_PUBLIC_MODE", "false").lower() in ("true", "1", "yes")


def native_echo(args: List[str]) -> str:
    """Native implementation of echo."""
    return " ".join(args)


def native_ls(args: List[str], task_id: Optional[str] = None) -> str:
    """Native implementation of ls within task workspace."""
    non_flags = [a for a in args if not a.startswith("-")]
    target_rel = non_flags[0] if non_flags else "."
    target_path = resolve_workspace_path(target_rel, task_id=task_id)
    if not target_path.exists():
        raise FileNotFoundError(f"Directory not found: {target_rel}")
    if target_path.is_file():
        return f"{target_path.name} ({target_path.stat().st_size} bytes)"

    lines = []
    for item in sorted(target_path.iterdir()):
        type_str = "<DIR>" if item.is_dir() else f"{item.stat().st_size:>8} bytes"
        lines.append(f"{type_str}  {item.name}")
    return "\n".join(lines)


def native_cat(args: List[str], task_id: Optional[str] = None) -> str:
    """Native implementation of cat for files within task workspace."""
    if not args:
        raise ValueError("cat requires at least one file argument")
    outputs = []
    for arg in args:
        p = resolve_workspace_path(arg, task_id=task_id)
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {arg}")
        outputs.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(outputs)


def native_head(args: List[str], task_id: Optional[str] = None) -> str:
    """Native implementation of head (-n lines)."""
    lines_count = 10
    file_arg = None
    i = 0
    while i < len(args):
        if args[i] == "-n" and i + 1 < len(args):
            lines_count = int(args[i + 1])
            i += 2
        elif args[i].startswith("-n"):
            lines_count = int(args[i][2:])
            i += 1
        else:
            file_arg = args[i]
            i += 1

    if not file_arg:
        raise ValueError("head requires a file argument")

    p = resolve_workspace_path(file_arg, task_id=task_id)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {file_arg}")

    with p.open("r", encoding="utf-8", errors="replace") as fp:
        selected = [fp.readline() for _ in range(lines_count)]
    return "".join(selected).rstrip()


def native_tail(args: List[str], task_id: Optional[str] = None) -> str:
    """Native implementation of tail (-n lines)."""
    lines_count = 10
    file_arg = None
    i = 0
    while i < len(args):
        if args[i] == "-n" and i + 1 < len(args):
            lines_count = int(args[i + 1])
            i += 2
        elif args[i].startswith("-n"):
            lines_count = int(args[i][2:])
            i += 1
        else:
            file_arg = args[i]
            i += 1

    if not file_arg:
        raise ValueError("tail requires a file argument")

    p = resolve_workspace_path(file_arg, task_id=task_id)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {file_arg}")

    with p.open("r", encoding="utf-8", errors="replace") as fp:
        all_lines = fp.readlines()
    return "".join(all_lines[-lines_count:]).rstrip()


def native_grep(args: List[str], task_id: Optional[str] = None) -> str:
    """Native implementation of grep."""
    if len(args) < 2:
        raise ValueError("grep requires pattern and file arguments")
    pattern = args[0]
    file_arg = args[1]
    p = resolve_workspace_path(file_arg, task_id=task_id)
    if not p.is_file():
        raise FileNotFoundError(f"File not found: {file_arg}")

    regex = re.compile(pattern)
    matched = []
    with p.open("r", encoding="utf-8", errors="replace") as fp:
        for idx, line in enumerate(fp, 1):
            if regex.search(line):
                matched.append(f"{idx}: {line.rstrip()}")
    return "\n".join(matched)


def get_scrubbed_env() -> Dict[str, str]:
    """Return a sanitized, minimal environment containing NO secrets or API keys."""
    allowed_keys = {
        "PATH",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "TEMP",
        "TMP",
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
    }
    scrubbed = {}
    for k, v in os.environ.items():
        k_upper = k.upper()
        if k_upper in allowed_keys:
            # Extra safeguard against tainted variables
            if not any(bad in k_upper for bad in ("KEY", "SECRET", "TOKEN", "AUTH", "PASS")):
                scrubbed[k] = v
    # Ensure NEBIUS_API_KEY is purged under all circumstances
    scrubbed.pop("NEBIUS_API_KEY", None)
    return scrubbed


def run_python_subprocess(
    script_arg: str,
    script_params: List[str],
    task_id: Optional[str] = None,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Execute a single python script strictly inside the task workspace."""
    resolved_script = resolve_workspace_path(script_arg, task_id=task_id)
    if not resolved_script.is_file():
        raise FileNotFoundError(f"Python script not found in task workspace: {script_arg}")
    if not resolved_script.name.endswith(".py"):
        raise PermissionError(f"Target file must be a .py script: {script_arg}")

    ws = get_workspace_dir(task_id)
    cmd = [sys.executable, "-I", str(resolved_script)] + script_params
    scrubbed_env = get_scrubbed_env()

    try:
        proc = subprocess.run(
            cmd,
            shell=False,  # STRICTLY NO SHELL
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=scrubbed_env,
        )
        stdout = (proc.stdout or "")[:MAX_OUTPUT_BYTES]
        stderr = (proc.stderr or "")[:MAX_OUTPUT_BYTES]
        return {
            "cmd": f"python {script_arg}",
            "exit_code": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "cmd": f"python {script_arg}",
            "exit_code": -1,
            "stdout": (e.stdout or "")[:MAX_OUTPUT_BYTES],
            "stderr": f"Execution timed out after {timeout} seconds",
            "error": "timeout",
        }


def validate_shell_security(cmd_str: str) -> None:
    """Validate that command contains no shell metacharacters or dangerous path escapes."""
    # 1. Metacharacters check
    for ch in FORBIDDEN_CHARS:
        if ch in cmd_str:
            raise PermissionError(
                f"Shell metacharacter '{ch}' is strictly forbidden in run_shell."
            )

    # 2. Raw traversal checks
    tokens = shlex.split(cmd_str)
    for t in tokens:
        if ".." in t:
            raise PermissionError(f"Directory traversal '..' is strictly forbidden: '{t}'")
        if is_drive_relative(t):
            raise PermissionError(f"Drive-relative paths are forbidden: '{t}'")
        if is_unc_path(t):
            raise PermissionError(f"UNC paths are forbidden: '{t}'")
        # Reject absolute paths in arguments (must be relative to task workspace)
        if len(tokens) > 1 and t != tokens[0]:
            p = Path(t)
            if p.is_absolute():
                raise PermissionError(f"Absolute paths in command arguments are forbidden: '{t}'")


def run_shell(cmd: str, task_id: Optional[str] = None, timeout: float = 15.0) -> Dict[str, Any]:
    """Execute a safe command inside the task workspace.
    
    NO shell=True. Dispatches to native implementations or sandboxed python subprocess.
    """
    if is_public_mode():
        raise PermissionError("run_shell is disabled in SAHA_PUBLIC_MODE.")

    cmd_str = cmd.strip()
    if not cmd_str:
        return {"cmd": cmd, "exit_code": 0, "stdout": "", "stderr": ""}

    validate_shell_security(cmd_str)
    tokens = shlex.split(cmd_str)
    if not tokens:
        return {"cmd": cmd, "exit_code": 0, "stdout": "", "stderr": ""}

    prog = tokens[0].lower()
    args = tokens[1:]

    try:
        if prog == "echo":
            out = native_echo(args)
            return {"cmd": cmd, "exit_code": 0, "stdout": out + "\n", "stderr": ""}

        elif prog == "ls":
            out = native_ls(args, task_id=task_id)
            return {"cmd": cmd, "exit_code": 0, "stdout": out + "\n", "stderr": ""}

        elif prog == "cat":
            out = native_cat(args, task_id=task_id)
            return {"cmd": cmd, "exit_code": 0, "stdout": out, "stderr": ""}

        elif prog == "head":
            out = native_head(args, task_id=task_id)
            return {"cmd": cmd, "exit_code": 0, "stdout": out + "\n", "stderr": ""}

        elif prog == "tail":
            out = native_tail(args, task_id=task_id)
            return {"cmd": cmd, "exit_code": 0, "stdout": out + "\n", "stderr": ""}

        elif prog == "grep":
            out = native_grep(args, task_id=task_id)
            return {"cmd": cmd, "exit_code": 0, "stdout": out + "\n", "stderr": ""}

        elif prog in ("python", "python3"):
            if not args:
                raise ValueError("python command requires a script argument: 'python <script.py>'")
            if args[0].startswith("-"):
                raise PermissionError(f"Inline flags or '-c' are forbidden. Only 'python <script.py>' is allowed.")
            script_path = args[0]
            script_params = args[1:]
            return run_python_subprocess(script_path, script_params, task_id=task_id, timeout=timeout)

        else:
            raise PermissionError(
                f"Command '{prog}' is not in the safe allowlist. "
                "Only ls, cat, head, tail, grep, echo, and python <script.py> are permitted."
            )

    except PermissionError:
        raise
    except Exception as e:
        return {
            "cmd": cmd,
            "exit_code": 1,
            "stdout": "",
            "stderr": str(e),
            "error": str(e),
        }
