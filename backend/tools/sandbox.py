"""Workspace sandbox path resolution and security boundary."""

import os
import re
import stat
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BASE_WORKSPACE = (PROJECT_ROOT / "workspace").resolve()


def is_drive_relative(path_str: str) -> bool:
    """Check for Windows drive-relative paths like 'C:foo' or 'D:bar'."""
    return bool(re.match(r"^[a-zA-Z]:[^\\/]", str(path_str).strip()))


def is_unc_path(path_str: str) -> bool:
    """Check for UNC network paths like '\\server\share'."""
    s = str(path_str).strip()
    return s.startswith(r"\\") or s.startswith("//")


def get_workspace_dir(task_id: Optional[str] = None) -> Path:
    """Return the absolute path to workspace/<task_id> or workspace/."""
    if task_id:
        # Sanitize task_id to prevent traversal
        safe_tid = re.sub(r"[^a-zA-Z0-9_\-]", "", str(task_id))
        ws = (BASE_WORKSPACE / safe_tid).resolve()
    else:
        ws = BASE_WORKSPACE

    ws.mkdir(parents=True, exist_ok=True)
    return ws


def is_reparse_or_symlink(path: Path) -> bool:
    """Check if a path or any of its parents is a symlink or Windows junction."""
    try:
        curr = path
        while True:
            if curr.is_symlink() or os.path.islink(curr):
                return True
            if os.name == "nt" and curr.exists():
                attrs = os.lstat(curr).st_file_attributes
                if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    return True
            parent = curr.parent
            if parent == curr:
                break
            curr = parent
    except Exception:
        pass
    return False


def resolve_workspace_path(path: str, task_id: Optional[str] = None) -> Path:
    """Resolve and validate that a path is strictly inside workspace/<task_id>/.
    
    Rejects:
    - Empty paths
    - Drive-relative paths (C:foo)
    - UNC paths (\\\\server\\share)
    - Path traversal segments (..)
    - Absolute paths outside the task workspace
    - Symlinks and Windows reparse points/junctions
    """
    path_str = str(path).strip()
    if not path_str:
        raise ValueError("Path cannot be empty")

    if is_drive_relative(path_str):
        raise PermissionError(f"Drive-relative paths like '{path_str}' are strictly forbidden.")

    if is_unc_path(path_str):
        raise PermissionError(f"UNC paths like '{path_str}' are strictly forbidden.")

    # Reject raw traversal markers
    parts = re.split(r"[\\/]", path_str)
    if ".." in parts:
        raise PermissionError(f"Path traversal '..' is strictly forbidden: '{path_str}'")

    ws = get_workspace_dir(task_id)
    p = Path(path_str)

    if p.is_absolute():
        resolved = p.resolve()
    else:
        resolved = (ws / p).resolve()

    # Recheck containment AFTER resolution
    try:
        resolved.relative_to(ws)
    except ValueError:
        raise PermissionError(
            f"Path traversal rejected: '{path_str}' resolves outside task workspace boundary ({ws})"
        )

    # Reject symlinks and junctions
    if resolved.exists() and is_reparse_or_symlink(resolved):
        raise PermissionError(f"Symlinks and junctions are strictly forbidden: '{path_str}'")

    return resolved
