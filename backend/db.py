"""Saha Database Module.

SQLite schema, connection management, telemetry, and append-only ledger state.
Enforces WAL mode, foreign keys, and monotonic audit tracking.
"""

import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "saha.db"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_db_path(custom_path: Optional[str] = None) -> Path:
    """Return the database path from argument, env var, or default."""
    if custom_path:
        return Path(custom_path)
    env_path = os.getenv("SAHA_DB_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


def get_db_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Create a new SQLite connection configured with WAL and foreign keys."""
    path = get_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn


@contextmanager
def get_db(db_path: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
    """Context manager for SQLite database transactions."""
    conn = get_db_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


TABLES_SQL = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planning', 'running', 'paused', 'complete', 'failed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    intent TEXT NOT NULL,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    result_json TEXT,
    probe_json TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'done', 'verified', 'drifted', 'superseded', 'failed')),
    drift_reason TEXT,
    superseded_by TEXT REFERENCES steps(id),
    error TEXT,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS drift_events (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
    tool TEXT NOT NULL,
    reason TEXT NOT NULL,
    explanation TEXT,
    detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    role TEXT,
    latency_ms REAL NOT NULL,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('success', 'error')),
    error TEXT,
    created_at TEXT NOT NULL
);
"""

INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_steps_task_seq ON steps(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_steps_status ON steps(task_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_drift_events_task ON drift_events(task_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_model ON llm_calls(model);
"""


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize database tables, run incremental migrations, and create indexes."""
    with get_db(db_path) as conn:
        conn.executescript(TABLES_SQL)

        # Migrate existing steps table columns if missing
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(steps)").fetchall()}
        if "seq" not in cols:
            conn.execute("ALTER TABLE steps ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;")
        if "drift_reason" not in cols:
            conn.execute("ALTER TABLE steps ADD COLUMN drift_reason TEXT;")
        if "superseded_by" not in cols:
            conn.execute("ALTER TABLE steps ADD COLUMN superseded_by TEXT REFERENCES steps(id);")

        conn.executescript(INDEXES_SQL)


def record_llm_call(
    model: str,
    latency_ms: float,
    role: Optional[str] = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    status: str = "success",
    error: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    """Record LLM call telemetry into SQLite."""
    init_db(db_path)
    call_id = str(uuid.uuid4())
    now = utc_now_iso()
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO llm_calls (id, model, role, latency_ms, prompt_tokens, completion_tokens, total_tokens, status, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                model,
                role or "unknown",
                float(latency_ms),
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                int(total_tokens or 0),
                status,
                error,
                now,
            ),
        )
    return call_id


def get_llm_metrics(db_path: Optional[str] = None) -> Dict[str, Any]:
    """Calculate average latency and token usage grouped by model and role."""
    init_db(db_path)
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                model,
                role,
                COUNT(*) as call_count,
                AVG(latency_ms) as avg_latency_ms,
                SUM(prompt_tokens) as total_prompt_tokens,
                SUM(completion_tokens) as total_completion_tokens,
                SUM(total_tokens) as total_tokens
            FROM llm_calls
            WHERE status = 'success'
            GROUP BY model, role
            ORDER BY call_count DESC
            """
        ).fetchall()
        models_data = []
        for r in rows:
            models_data.append({
                "model": r["model"],
                "role": r["role"],
                "call_count": r["call_count"],
                "avg_latency_ms": round(r["avg_latency_ms"], 2),
                "total_prompt_tokens": r["total_prompt_tokens"] or 0,
                "total_completion_tokens": r["total_completion_tokens"] or 0,
                "total_tokens": r["total_tokens"] or 0,
            })
        total_calls = conn.execute("SELECT COUNT(*) as cnt FROM llm_calls").fetchone()["cnt"]
        return {
            "total_calls": total_calls,
            "models": models_data,
        }
