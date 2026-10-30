"""Container Verification Test: runs scripts/verify_container.sh when Docker is present."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="Docker CLI not found in environment (Windows host without Docker daemon/WSL)"
)
def test_container_verification_script():
    """Execute the full container verification suite via scripts/verify_container.sh."""
    script_path = PROJECT_ROOT / "scripts" / "verify_container.sh"
    assert script_path.exists(), f"Verification script not found: {script_path}"

    res = subprocess.run(
        ["bash", str(script_path)],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    print("STDOUT:\n", res.stdout)
    print("STDERR:\n", res.stderr)
    assert res.returncode == 0, f"verify_container.sh failed with exit code {res.returncode}:\n{res.stderr}\n{res.stdout}"
