#!/usr/bin/env python3
"""Real FastAPI server subprocess runner for process termination and API resume tests.

Executes the actual FastAPI / Uvicorn server in a separate OS process,
allowing real OS termination (proc.kill()) during an in-flight mutation,
and restarting the real server so its normal startup hook runs.

REAL VS MOCKED COMPONENTS:
- REAL:
  * Operating system process lifecycle (subprocess.Popen, SIGKILL / TerminateProcess).
  * Real Uvicorn ASGI server and HTTP loopback socket communication.
  * Real FastAPI lifespan / startup hook (Ledger.recover_stale_running_tasks).
  * Real authenticated resume endpoint (POST /tasks/{id}/resume with X-Saha-Owner).
  * Real security middleware (public-mode token validation, cross-owner isolation).
  * Real SQLite database transactions, WAL recovery, and ledger state.
  * Real sandboxed filesystem operations and SHA-256 verifier probes.
  * Real policy evaluation and approval ledger records.
- MOCKED:
  * External HTTP mutation target: In-process mock records dispatch headers (Idempotency-Key)
    and simulates mid-flight pause or success without external internet traffic or SSRF bypass.
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


def run_server(mode: str, port: int, db_path: str, ws_path: str, marker_dir: str):
    marker_path = Path(marker_dir)
    marker_path.mkdir(parents=True, exist_ok=True)

    os.environ["SAHA_DB_PATH"] = db_path
    os.environ["SAHA_WORKSPACE_ROOT"] = ws_path
    os.environ["SAHA_PUBLIC_MODE"] = "true"
    os.environ["SAHA_DEMO_MODE"] = "true"
    os.environ["NEBIUS_API_KEY"] = ""

    from backend import tools
    from backend.api import app
    import uvicorn

    real_write = tools.write_file
    written_files = []

    def spy_write(path, content, task_id=None):
        written_files.append(path)
        out_file = marker_path / f"server_{port}_written_files.json"
        out_file.write_text(json.dumps(written_files), encoding="utf-8")
        return real_write(path, content, task_id=task_id)

    if mode == "pause_on_mutation":
        def mock_step2_http(url, method="GET", headers=None, data=None, json_data=None,
                            timeout=5.0, allowed_domains=None, idempotency_key=None,
                            idempotency_header="Idempotency-Key"):
            info = {
                "url": url,
                "method": method,
                "idempotency_key": idempotency_key,
                "idempotency_header": idempotency_header,
            }
            (marker_path / "api_step2_called.json").write_text(json.dumps(info), encoding="utf-8")
            (marker_path / "api_step2_ready_to_kill.marker").write_text("ready", encoding="utf-8")

            # Pause in loop while mutation is in-flight waiting for OS kill
            while True:
                time.sleep(0.05)

        with patch("backend.executor.call_llm", return_value="Deterministic worker response."), \
             patch("backend.resume.call_llm", return_value="Deterministic fast explanation."), \
             patch("backend.executor.write_file", side_effect=spy_write), \
             patch("backend.executor.http_request", side_effect=mock_step2_http):
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

    elif mode == "normal_resume":
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
            (marker_path / "server2_http_called.json").write_text(json.dumps(recorded_http), encoding="utf-8")
            return {
                "url": url,
                "method": method,
                "status_code": 201,
                "headers": {"content-type": "application/json"},
                "body": json.dumps({"status": "success", "id": "tx_api_777"}),
                "body_length": 35,
                "idempotency_key": idempotency_key,
            }

        with patch("backend.executor.call_llm", return_value="Deterministic worker response."), \
             patch("backend.resume.call_llm", return_value="Deterministic fast explanation."), \
             patch("backend.executor.write_file", side_effect=spy_write), \
             patch("backend.executor.http_request", side_effect=mock_resume_http):
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    if len(sys.argv) < 6:
        print("Usage: api_server_subprocess_runner.py <mode> <port> <db_path> <ws_path> <marker_dir>")
        sys.exit(1)
    run_server(sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5])
