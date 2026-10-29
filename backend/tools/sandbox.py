"""Workspace sandbox path resolution and security boundary."""

import os
import re
import stat
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def get_base_workspace() -> Path:
    """Return configured base workspace root directory."""
    env_ws = os.getenv("SAHA_WORKSPACE_ROOT")
    if env_ws:
        return Path(env_ws).resolve()
    return (PROJECT_ROOT / "workspace").resolve()


def is_drive_relative(path_str: str) -> bool:
    """Check for Windows drive-relative paths like 'C:foo' or 'D:bar'."""
    return bool(re.match(r"^[a-zA-Z]:[^\\/]", str(path_str).strip()))


def is_unc_path(path_str: str) -> bool:
    r"""Check for UNC network paths like '\\server\share'."""
    s = str(path_str).strip()
    return s.startswith(r"\\") or s.startswith("//")


def get_workspace_dir(task_id: Optional[str] = None, owner_id: Optional[str] = None) -> Path:
    """Return the absolute path to workspace/<owner_id>/<task_id> or workspace/<owner_id> or workspace/."""
    from backend.db import get_task_owner_id

    base = get_base_workspace()
    if not task_id and not owner_id:
        ws = base
    elif not task_id and owner_id:
        safe_oid = re.sub(r"[^a-zA-Z0-9_\-]", "", str(owner_id))
        ws = (base / safe_oid).resolve()
    else:
        # task_id is present
        safe_tid = re.sub(r"[^a-zA-Z0-9_\-]", "", str(task_id))
        effective_owner = owner_id or get_task_owner_id(task_id) or "default"
        safe_oid = re.sub(r"[^a-zA-Z0-9_\-]", "", str(effective_owner))
        ws = (base / safe_oid / safe_tid).resolve()

    ws.mkdir(parents=True, exist_ok=True)
    return ws


def delete_owner_workspace(owner_id: str) -> None:
    """Delete all files and directories in workspace/<owner_id>/."""
    import shutil

    base = get_base_workspace()
    safe_oid = re.sub(r"[^a-zA-Z0-9_\-]", "", str(owner_id))
    owner_ws = (base / safe_oid).resolve()
    if owner_ws.exists() and owner_ws.is_relative_to(base) and owner_ws != base:
        shutil.rmtree(owner_ws, ignore_errors=True)


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


def resolve_workspace_path(path: str, task_id: Optional[str] = None, owner_id: Optional[str] = None) -> Path:
    """Resolve and validate that a path is strictly inside workspace/<owner_id>/<task_id>/.
    
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

    ws = get_workspace_dir(task_id, owner_id=owner_id)
    p = Path(path_str)

    target_unresolved = p if p.is_absolute() else (ws / p)

    # Check unresolved path components for symlinks or reparse points
    curr = target_unresolved
    while True:
        try:
            if curr.is_symlink() or os.path.islink(curr):
                raise PermissionError(f"Symlinks and junctions are strictly forbidden: '{path_str}'")
            if os.name == "nt" and curr.exists():
                attrs = os.lstat(curr).st_file_attributes
                if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise PermissionError(f"Symlinks and junctions are strictly forbidden: '{path_str}'")
        except PermissionError:
            raise
        except Exception:
            pass
        if curr == ws or curr.parent == curr:
            break
        curr = curr.parent

    resolved = target_unresolved.resolve()

    # Recheck containment AFTER resolution
    try:
        resolved.relative_to(ws)
    except ValueError:
        raise PermissionError(
            f"Path traversal rejected: '{path_str}' resolves outside task workspace boundary."
        )

    # Check resolved path components for symlinks or reparse points
    curr = resolved
    while True:
        try:
            if curr.is_symlink() or os.path.islink(curr):
                raise PermissionError(f"Symlinks and junctions are strictly forbidden: '{path_str}'")
            if os.name == "nt" and curr.exists():
                attrs = os.lstat(curr).st_file_attributes
                if attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise PermissionError(f"Symlinks and junctions are strictly forbidden: '{path_str}'")
        except PermissionError:
            raise
        except Exception:
            pass
        if curr == ws or curr.parent == curr:
            break
        curr = curr.parent

    return resolved
