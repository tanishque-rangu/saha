"""File operation tools restricted strictly to task workspace."""

import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

from backend.tools.sandbox import resolve_workspace_path


def compute_sha256(content_bytes: bytes) -> str:
    """Compute SHA-256 hexadecimal digest of bytes."""
    return hashlib.sha256(content_bytes).hexdigest()


def write_file(path: str, content: str, task_id: Optional[str] = None) -> Dict[str, Any]:
    """Write content to a file inside the task workspace sandbox."""
    target = resolve_workspace_path(path, task_id=task_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    content_bytes = content.encode("utf-8")
    target.write_bytes(content_bytes)
    digest = compute_sha256(content_bytes)
    return {
        "path": path,
        "bytes_written": len(content_bytes),
        "sha256": digest,
    }


def read_file(path: str, task_id: Optional[str] = None) -> Dict[str, Any]:
    """Read content from a file inside the task workspace sandbox."""
    target = resolve_workspace_path(path, task_id=task_id)
    if not target.exists():
        raise FileNotFoundError(f"File not found in workspace: {path}")
    if not target.is_file():
        raise IsADirectoryError(f"Target is a directory, not a file: {path}")

    content_bytes = target.read_bytes()
    text = content_bytes.decode("utf-8", errors="replace")
    digest = compute_sha256(content_bytes)
    return {
        "path": path,
        "content": text,
        "bytes_read": len(content_bytes),
        "sha256": digest,
    }


def get_file_sha256(path: str, task_id: Optional[str] = None) -> Optional[str]:
    """Return SHA-256 of file in workspace if it exists, else None."""
    try:
        target = resolve_workspace_path(path, task_id=task_id)
        if target.is_file():
            return compute_sha256(target.read_bytes())
    except Exception:
        pass
    return None
