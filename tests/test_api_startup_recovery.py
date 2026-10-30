"""Tests for FastAPI startup recovery lifecycle, import cleanliness, and failure visibility."""

import importlib
import json
import logging
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import backend.api
from backend.api import STARTUP_RECOVERY_STATUS, app
from backend.db import init_db


def test_importing_backend_api_does_not_run_recovery():
    """Importing backend.api must have zero recovery side effects."""
    with patch("backend.ledger.Ledger.recover_stale_running_tasks") as mock_recover:
        importlib.reload(backend.api)
        mock_recover.assert_not_called()


def test_starting_fastapi_app_runs_recovery_exactly_once():
    """Starting the FastAPI app through lifespan runs recovery exactly once."""
    with patch("backend.ledger.Ledger.recover_stale_running_tasks", return_value=["task_sim_001"]) as mock_recover:
        with TestClient(backend.api.app) as client:
            # Inside lifespan, recovery has run exactly once
            assert mock_recover.call_count == 1

            # Subsequent requests do not trigger recovery again
            res1 = client.get("/health")
            assert res1.status_code == 200
            assert res1.json()["status"] == "ok"

            res2 = client.get("/metrics")
            assert res2.status_code == 200

            assert mock_recover.call_count == 1
            assert backend.api.STARTUP_RECOVERY_STATUS["completed"] is True
            assert backend.api.STARTUP_RECOVERY_STATUS["error"] is None
            assert backend.api.STARTUP_RECOVERY_STATUS["recovered_tasks"] == ["task_sim_001"]


def test_recovery_failure_visible_and_prevents_normal_startup(caplog):
    """When startup recovery fails, lifespan raises an explicit error and logs context."""
    db_err = sqlite3.DatabaseError("database disk image is malformed")

    with caplog.at_level(logging.ERROR, logger="saha.api"):
        with patch("backend.ledger.Ledger.recover_stale_running_tasks", side_effect=db_err):
            with pytest.raises(RuntimeError) as exc_info:
                with TestClient(backend.api.app):
                    pass

            assert "Startup recovery failed: database disk image is malformed" in str(exc_info.value)
            assert isinstance(exc_info.value.__cause__, sqlite3.DatabaseError)

    # Contextual error was logged
    assert any("Startup recovery failed: database disk image is malformed" in record.message for record in caplog.records)
    assert backend.api.STARTUP_RECOVERY_STATUS["completed"] is False
    assert "database disk image is malformed" in backend.api.STARTUP_RECOVERY_STATUS["error"]


def test_recovery_failure_prevents_health_serving():
    """When recovery does not complete or fails, /health returns 503 and refuses to report healthy."""
    # Simulate a failed startup recovery state
    backend.api.STARTUP_RECOVERY_STATUS["lifespan_started"] = True
    backend.api.STARTUP_RECOVERY_STATUS["completed"] = False
    backend.api.STARTUP_RECOVERY_STATUS["error"] = "Unrecoverable WAL corruption"

    client = TestClient(backend.api.app)
    res = client.get("/health")

    assert res.status_code == 503
    data = res.json()
    assert data["status"] == "unhealthy"
    assert data["recovery_ok"] is False
    assert "Unrecoverable WAL corruption" in data["recovery_error"]

    # Reset status after test
    backend.api.STARTUP_RECOVERY_STATUS["lifespan_started"] = False
    backend.api.STARTUP_RECOVERY_STATUS["completed"] = True
    backend.api.STARTUP_RECOVERY_STATUS["error"] = None


def test_real_server_process_aborts_on_recovery_failure():
    """In a real OS subprocess running Uvicorn, recovery failure halts the process before health serving."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_path = tmp_path / "broken.db"
        ws_path = tmp_path / "ws"
        ws_path.mkdir()

        # Write corrupt bytes to SQLite file so recovery crashes on connection
        db_path.write_bytes(b"NOT_A_VALID_SQLITE3_DATABASE_FILE_HEADER")

        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        sub_env = sys.modules["os"].environ.copy()
        sub_env["SAHA_DB_PATH"] = str(db_path)
        sub_env["SAHA_WORKSPACE_ROOT"] = str(ws_path)
        sub_env["SAHA_PUBLIC_MODE"] = "true"
        sub_env["SAHA_DEMO_MODE"] = "true"
        sub_env["PYTHONPATH"] = str(PROJECT_ROOT)
        sub_env["NEBIUS_API_KEY"] = ""

        # Launch real Uvicorn server in separate process
        proc = subprocess.Popen([
            sys.executable,
            "-m", "uvicorn",
            "backend.api:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ], env=sub_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Wait for process to exit (it should abort during startup lifespan)
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()

        # Uvicorn exits with error code when lifespan raises
        assert proc.returncode != 0, f"Expected server to abort with non-zero exit code, got {proc.returncode}"
        combined_output = (stdout.decode("utf-8", errors="replace") + stderr.decode("utf-8", errors="replace"))
        assert "Startup recovery failed" in combined_output or "Application startup failed" in combined_output

        # Verify no socket is listening / health check cannot connect
        with pytest.raises(Exception):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
