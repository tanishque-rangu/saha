"""Script to set up UI demonstration states and capture mobile (390px) screenshots."""

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "saha.db"
CHROME_PATH = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
SCREENSHOTS_DIR = PROJECT_ROOT / "docs" / "screenshots"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)


def setup_tasks():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Task 2: Running Task
    running_task_id = "demo-task-running"
    cur.execute("DELETE FROM tasks WHERE id = ?", (running_task_id,))
    cur.execute("DELETE FROM steps WHERE task_id = ?", (running_task_id,))
    cur.execute(
        "INSERT INTO tasks (id, goal, status, created_at, updated_at) VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        (running_task_id, "Analyze system state, write diagnostics report, and check connectivity", "running"),
    )
    steps_running = [
        (running_task_id, 1, 1, "write_file", '{"path": "diagnostics.txt"}', '{"bytes_written": 240, "sha256": "abc1234"}', '{"probe_type": "file_probe", "expected_path": "diagnostics.txt"}', "verified", "Create diagnostics report", None, None, None),
        (running_task_id, 2, 2, "run_shell", '{"command": "python test_health.py"}', None, '{"probe_type": "command_probe", "expected_exit_code": 0}', "running", "Execute health checks script", None, None, None),
        (running_task_id, 3, 3, "http_get", '{"url": "https://example.com"}', None, '{"probe_type": "http_probe", "expected_status": 200}', "pending", "Verify external network reachability", None, None, None),
    ]
    for s in steps_running:
        cur.execute(
            """INSERT INTO steps (id, task_id, idx, seq, tool, args_json, result_json, probe_json, status, intent, drift_reason, resolved_by, superseded_by)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"{s[0]}-step-{s[1]}", s[0], s[1], s[2], s[3], s[4], s[5], s[6], s[7], s[8], s[9], s[10], s[11]),
        )

    # Task 3: Drift Banner Active
    drift_task_id = "demo-task-drifted"
    cur.execute("DELETE FROM tasks WHERE id = ?", (drift_task_id,))
    cur.execute("DELETE FROM steps WHERE task_id = ?", (drift_task_id,))
    cur.execute("DELETE FROM drift_events WHERE task_id = ?", (drift_task_id,))
    cur.execute(
        "INSERT INTO tasks (id, goal, status, created_at, updated_at) VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        (drift_task_id, "Create two small data files with facts about Hyderabad and combine into a summary", "paused"),
    )
    cur.execute(
        """INSERT INTO steps (id, task_id, idx, seq, tool, args_json, result_json, probe_json, status, intent, drift_reason)
           VALUES (?, ?, 1, 1, 'write_file', '{"path": "facts1.txt"}', '{"bytes_written": 140}', '{"probe_type": "file_probe", "expected_path": "facts1.txt"}', 'drifted', 'Create first facts file', 'File was deleted or missing from task workspace')""",
        (f"{drift_task_id}-s1", drift_task_id),
    )
    cur.execute(
        """INSERT INTO steps (id, task_id, idx, seq, tool, args_json, result_json, probe_json, status, intent)
           VALUES (?, ?, 2, 2, 'write_file', '{"path": "facts2.txt"}', '{"bytes_written": 150}', '{"probe_type": "file_probe", "expected_path": "facts2.txt"}', 'verified', 'Create second facts file')""",
        (f"{drift_task_id}-s2", drift_task_id),
    )
    cur.execute(
        """INSERT INTO steps (id, task_id, idx, seq, tool, args_json, result_json, probe_json, status, intent)
           VALUES (?, ?, 3, 3, 'write_file', '{"path": "summary.txt"}', '{"bytes_written": 300}', '{"probe_type": "file_probe", "expected_path": "summary.txt"}', 'verified', 'Combine facts into summary file')""",
        (f"{drift_task_id}-s3", drift_task_id),
    )
    cur.execute(
        """INSERT INTO drift_events (id, task_id, step_id, tool, reason, explanation, detected_at)
           VALUES ('drift-evt-1', ?, ?, 'write_file', 'File was deleted or missing from task workspace', 'Step 1 created facts1.txt, but probe now reports the file is missing from the workspace.', datetime('now'))""",
        (drift_task_id, f"{drift_task_id}-s1"),
    )

    # Task 4: After Resume Complete
    resume_task_id = "demo-task-resumed"
    cur.execute("DELETE FROM tasks WHERE id = ?", (resume_task_id,))
    cur.execute("DELETE FROM steps WHERE task_id = ?", (resume_task_id,))
    cur.execute("DELETE FROM drift_events WHERE task_id = ?", (resume_task_id,))
    cur.execute(
        "INSERT INTO tasks (id, goal, status, created_at, updated_at) VALUES (?, ?, ?, datetime('now'), datetime('now'))",
        (resume_task_id, "Create two small data files with facts about Hyderabad and combine into a summary", "complete"),
    )
    steps_resumed = [
        (f"{resume_task_id}-s1", 1, 1, "write_file", "drifted", "Create first Hyderabad facts file", "File facts1.txt was missing from workspace", f"{resume_task_id}-s5", None),
        (f"{resume_task_id}-s2", 2, 2, "write_file", "verified", "Create second Hyderabad facts file", None, None, None),
        (f"{resume_task_id}-s3", 3, 3, "write_file", "superseded", "Combine facts into a summary file", None, None, f"{resume_task_id}-s6"),
        (f"{resume_task_id}-s4", 4, 4, "http_get", "verified", "Check reachability of example.com", None, None, None),
        (f"{resume_task_id}-s5", 5, 5, "write_file", "verified", "Repair: Re-create facts1.txt with verified Hyderabad facts", None, None, None),
        (f"{resume_task_id}-s6", 6, 6, "write_file", "verified", "Replacement: Re-combine facts into summary file", None, None, None),
    ]
    for s in steps_resumed:
        cur.execute(
            """INSERT INTO steps (id, task_id, idx, seq, tool, args_json, result_json, probe_json, status, intent, drift_reason, resolved_by, superseded_by)
               VALUES (?, ?, ?, ?, ?, '{"path": "file"}', '{"sha256": "123"}', '{"probe_type": "file_probe"}', ?, ?, ?, ?, ?)""",
            (s[0], resume_task_id, s[1], s[2], s[3], s[4], s[5], s[6], s[7], s[8]),
        )

    conn.commit()
    conn.close()
    print("Tasks configured for UI screenshots.")


def capture(url: str, output_path: Path, window_size: str = "390,920"):
    output_path.unlink(missing_ok=True)
    temp_profile = PROJECT_ROOT / ".chrome_temp"
    cmd = [
        str(CHROME_PATH),
        "--headless=new",
        "--disable-gpu",
        f"--user-data-dir={temp_profile}",
        f"--screenshot={output_path}",
        f"--window-size={window_size}",
        url,
    ]
    subprocess.run(cmd, check=True)
    time.sleep(1)
    if output_path.exists():
        print(f"✔ Captured: {output_path.name} ({output_path.stat().st_size} bytes)")
    else:
        print(f"✖ Failed: {output_path.name}")


def main():
    setup_tasks()
    base_url = "http://127.0.0.1:8000"

    # 1. Empty state
    capture(f"{base_url}/?empty=1", SCREENSHOTS_DIR / "01_empty_state.png", "390,844")

    # 2. Running task
    capture(f"{base_url}/?task=demo-task-running", SCREENSHOTS_DIR / "02_running_task.png", "390,920")

    # 3. Drift banner
    capture(f"{base_url}/?task=demo-task-drifted", SCREENSHOTS_DIR / "03_drift_banner.png", "390,960")

    # 4. After resume
    capture(f"{base_url}/?task=demo-task-resumed", SCREENSHOTS_DIR / "04_after_resume.png", "390,1050")


if __name__ == "__main__":
    main()
