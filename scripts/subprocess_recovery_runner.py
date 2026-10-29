#!/usr/bin/env python3
"""Subprocess runner used for genuine OS process termination and restart recovery tests.

REAL VS MOCKED COMPONENTS:
- REAL:
  * Operating system process lifecycle (subprocess.Popen, SIGKILL / TerminateProcess).
  * SQLite WAL database files, concurrent locking, crash recovery, and schema state.
  * Real sandboxed filesystem operations (reading/writing bytes, path isolation).
  * Deterministic verifier probes (SHA-256 disk calculations and status verification).
  * Policy engine enforcement (allowed_tools, allowed_domains, trusted_idempotency_domains).
- MOCKED:
  * External HTTP endpoints: Mocked in-process to record exact dispatch headers
    (including Idempotency-Key) and simulate mid-flight termination without network flakiness.
  * LLM calls: Mocked to return deterministic strings without requiring external API keys.
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Ensure UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from backend.db import init_db
from backend.executor import run_task
from backend.ledger import Ledger
from backend.resume import resume_task
from backend import tools


def run_mode(mode: str, task_id: str, db_path: str, ws_path: str, marker_dir: str):
    marker_path = Path(marker_dir)
    marker_path.mkdir(parents=True, exist_ok=True)

    os.environ["SAHA_DB_PATH"] = db_path
    os.environ["SAHA_WORKSPACE_ROOT"] = ws_path
    os.environ["SAHA_PUBLIC_MODE"] = "false"
    os.environ["SAHA_DEMO_MODE"] = "true"
    os.environ["NEBIUS_API_KEY"] = ""

    init_db(db_path)

    # Track real write_file invocations
    real_write = tools.write_file
    written_files = []

    def spy_write(path, content, task_id=None):
        written_files.append(path)
        out_file = marker_path / f"{mode}_written_files.json"
        out_file.write_text(json.dumps(written_files), encoding="utf-8")
        return real_write(path, content, task_id=task_id)

    if mode == "run_and_pause_on_step2":
        # Process 1: Executes step 1, begins step 2, signals parent, and pauses to be killed
        def mock_step2_http(url, method="GET", headers=None, data=None, json_data=None,
                            timeout=5.0, allowed_domains=None, idempotency_key=None,
                            idempotency_header="Idempotency-Key"):
            info = {
                "url": url,
                "method": method,
                "idempotency_key": idempotency_key,
                "idempotency_header": idempotency_header,
            }
            (marker_path / "step2_called.json").write_text(json.dumps(info), encoding="utf-8")
            # Signal parent process that we are actively running step 2
            (marker_path / "step2_ready_to_kill.marker").write_text("ready", encoding="utf-8")

            # Sleep in loop waiting for OS termination (proc.kill())
            while True:
                time.sleep(0.05)

        with patch("backend.executor.call_llm", return_value="Deterministic worker interpretation."), \
             patch("backend.executor.write_file", side_effect=spy_write), \
             patch("backend.executor.http_request", side_effect=mock_step2_http):
            run_task(task_id, db_path=db_path)

    elif mode == "recover_and_resume":
        # Process 2: Fresh process starts up, runs startup recovery, and resumes execution
        recorded_http = []

        def mock_resume_http(url, method="GET", headers=None, data=None, json_data=None,
                             timeout=5.0, allowed_domains=None, idempotency_key=None,
                             idempotency_header="Idempotency-Key"):
            info = {
                "url": url,
                "method": method,
                "idempotency_key": idempotency_key,
                "idempotency_header": idempotency_header,
            }
            recorded_http.append(info)
            (marker_path / "proc2_http_called.json").write_text(json.dumps(recorded_http), encoding="utf-8")
            return {
                "url": url,
                "method": method,
                "status_code": 201,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"status": "success", "id": "tx_888"}),
                "body_length": 35,
                "idempotency_key": idempotency_key,
            }

        with patch("backend.executor.call_llm", return_value="Deterministic worker interpretation."), \
             patch("backend.executor.write_file", side_effect=spy_write), \
             patch("backend.executor.http_request", side_effect=mock_resume_http):

            # Actual startup recovery: recover stale tasks left running from previous crashed process
            Ledger.recover_stale_running_tasks(db_path=db_path)
            # Resume task execution
            final_res = resume_task(task_id, db_path=db_path)

            (marker_path / "proc2_finished.json").write_text(json.dumps({
                "final_task": final_res.get("task", {}),
                "message": final_res.get("message"),
            }), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) < 6:
        print("Usage: subprocess_recovery_runner.py <mode> <task_id> <db_path> <ws_path> <marker_dir>")
        sys.exit(1)
    run_mode(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
