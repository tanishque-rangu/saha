"""Saha Database Module.

SQLite schema, connection management, telemetry, and append-only ledger state.
Enforces WAL mode, foreign keys, monotonic audit tracking, and private owner spaces.
"""

import hashlib
import json
import logging
import os
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "saha.db"

# Global tracking for data directory health and fallback
DATA_DIR_STATUS = {
    "data_dir_ok": True,
    "data_persistent": True,
    "effective_data_dir": "/data",
}


def is_dir_writable(path: Path) -> bool:
    """Test if a directory can be created and written to."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        test_file = path / f".write_test_{uuid.uuid4().hex}"
        test_file.write_bytes(b"test")
        test_file.unlink(missing_ok=True)
        return True
    except (PermissionError, OSError):
        return False


def check_and_init_data_dirs() -> None:
    """Verify SAHA_DB_PATH directory and SAHA_WORKSPACE_ROOT are writable, else fallback to /tmp/saha-data.
    
    If SAHA_REQUIRE_PERSISTENT is enabled, raises RuntimeError if storage is not writable or persistent.
    """
    global DATA_DIR_STATUS
    configured_db = os.getenv("SAHA_DB_PATH")
    configured_ws = os.getenv("SAHA_WORKSPACE_ROOT")
    require_persistent = os.getenv("SAHA_REQUIRE_PERSISTENT", "false").lower() in ("true", "1", "yes")

    db_dir = Path(configured_db).parent if configured_db else DEFAULT_DB_PATH.parent
    ws_dir = Path(configured_ws) if configured_ws else (PROJECT_ROOT / "workspace")

    db_ok = is_dir_writable(db_dir)
    ws_ok = is_dir_writable(ws_dir)

    if db_ok and ws_ok:
        DATA_DIR_STATUS["data_dir_ok"] = True
        # If explicitly pointed to tmp, persistent is False, else True
        is_tmp = "/tmp" in str(db_dir) or "\\tmp" in str(db_dir) or "temp" in str(db_dir).lower()
        DATA_DIR_STATUS["data_persistent"] = not is_tmp
        DATA_DIR_STATUS["effective_data_dir"] = "/data" if str(db_dir).startswith("/data") else str(db_dir)

        if require_persistent and is_tmp:
            raise RuntimeError(
                f"SAHA_REQUIRE_PERSISTENT is enabled, but configured storage '{DATA_DIR_STATUS['effective_data_dir']}' is temporary."
            )
    else:
        if require_persistent:
            raise RuntimeError(
                "SAHA_REQUIRE_PERSISTENT is enabled, but the configured data directory is not writable."
            )

        logger.warning("Configured database or workspace directory is not writable. Falling back to /tmp/saha-data.")
        fallback_root = Path(tempfile.gettempdir()) / "saha-data"
        fallback_db_dir = fallback_root
        fallback_ws_dir = fallback_root / "workspace"
        fallback_db_dir.mkdir(parents=True, exist_ok=True)
        fallback_ws_dir.mkdir(parents=True, exist_ok=True)

        os.environ["SAHA_DB_PATH"] = str(fallback_db_dir / "saha.db")
        os.environ["SAHA_WORKSPACE_ROOT"] = str(fallback_ws_dir)

        DATA_DIR_STATUS["data_dir_ok"] = False
        DATA_DIR_STATUS["data_persistent"] = False
        DATA_DIR_STATUS["effective_data_dir"] = "/tmp/saha-data"


def get_data_dir_status() -> Dict[str, Any]:
    """Return status of data directory health without exposing user secrets."""
    return dict(DATA_DIR_STATUS)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def hash_owner_token(token: Optional[str]) -> str:
    """Derive deterministic SHA-256 hex digest for an owner token."""
    raw = (token or DEFAULT_ANONYMOUS_TOKEN).strip()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


DEFAULT_ANONYMOUS_TOKEN = "default-anonymous-owner"
DEFAULT_OWNER_ID = hash_owner_token(DEFAULT_ANONYMOUS_TOKEN)


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
    owner_id TEXT NOT NULL DEFAULT 'default',
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
    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'done', 'verified', 'drifted', 'superseded', 'failed', 'blocked')),
    drift_reason TEXT,
    block_reason TEXT,
    superseded_by TEXT REFERENCES steps(id),
    resolved_by TEXT REFERENCES steps(id),
    depends_on_json TEXT NOT NULL DEFAULT '[]',
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

CREATE TABLE IF NOT EXISTS policies (
    owner_id TEXT PRIMARY KEY,
    policy_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    step_id TEXT NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
    tool TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied')),
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS llm_calls (
    id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL DEFAULT 'default',
    model TEXT NOT NULL,
    role TEXT,
    context_tags TEXT DEFAULT '[]',
    input_chars INTEGER DEFAULT 0,
    output_chars INTEGER DEFAULT 0,
    redaction_count INTEGER DEFAULT 0,
    dropped_tags INTEGER DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner_id, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_steps_task_seq ON steps(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_steps_status ON steps(task_id, status);
CREATE INDEX IF NOT EXISTS idx_drift_events_task ON drift_events(task_id);
CREATE INDEX IF NOT EXISTS idx_approvals_owner ON approvals(owner_id, status);
CREATE INDEX IF NOT EXISTS idx_llm_calls_owner ON llm_calls(owner_id, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_calls_model ON llm_calls(model);
"""


def init_db(db_path: Optional[str] = None) -> None:
    """Initialize database tables, run incremental migrations, and create indexes."""
    with get_db(db_path) as conn:
        conn.executescript(TABLES_SQL)

        # 1. Migrate tasks table columns if missing
        task_cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        if "owner_id" not in task_cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'default';")

        # 2. Migrate steps table columns if missing
        step_cols = {row["name"] for row in conn.execute("PRAGMA table_info(steps)").fetchall()}
        if "seq" not in step_cols:
            conn.execute("ALTER TABLE steps ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;")
        if "drift_reason" not in step_cols:
            conn.execute("ALTER TABLE steps ADD COLUMN drift_reason TEXT;")
        if "block_reason" not in step_cols:
            conn.execute("ALTER TABLE steps ADD COLUMN block_reason TEXT;")
        if "superseded_by" not in step_cols:
            conn.execute("ALTER TABLE steps ADD COLUMN superseded_by TEXT REFERENCES steps(id);")
        if "resolved_by" not in step_cols:
            conn.execute("ALTER TABLE steps ADD COLUMN resolved_by TEXT REFERENCES steps(id);")
        if "depends_on_json" not in step_cols:
            conn.execute("ALTER TABLE steps ADD COLUMN depends_on_json TEXT NOT NULL DEFAULT '[]';")

        # 3. Check if steps table CHECK constraint includes 'blocked'
        r = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='steps'").fetchone()
        if r and r[0] and "'blocked'" not in r[0]:
            conn.execute("PRAGMA foreign_keys = OFF;")
            conn.execute("ALTER TABLE steps RENAME TO steps_old;")
            conn.execute("""
            CREATE TABLE steps (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                idx INTEGER NOT NULL,
                seq INTEGER NOT NULL DEFAULT 0,
                intent TEXT NOT NULL,
                tool TEXT NOT NULL,
                args_json TEXT NOT NULL DEFAULT '{}',
                result_json TEXT,
                probe_json TEXT,
                status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'done', 'verified', 'drifted', 'superseded', 'failed', 'blocked')),
                drift_reason TEXT,
                superseded_by TEXT REFERENCES steps(id),
                resolved_by TEXT REFERENCES steps(id),
                depends_on_json TEXT NOT NULL DEFAULT '[]',
                error TEXT,
                started_at TEXT,
                finished_at TEXT
            );
            """)
            old_cols = [row["name"] for row in conn.execute("PRAGMA table_info(steps_old)").fetchall()]
            common_cols = ", ".join(old_cols)
            conn.execute(f"INSERT INTO steps ({common_cols}) SELECT {common_cols} FROM steps_old;")
            conn.execute("DROP TABLE steps_old;")
            conn.execute("PRAGMA foreign_keys = ON;")

        # 3b. Self-heal any foreign key references pointing to steps_old
        for tbl in ("approvals", "drift_events"):
            r_tbl = conn.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{tbl}'").fetchone()
            if r_tbl and r_tbl[0] and "steps_old" in r_tbl[0]:
                conn.execute(f"ALTER TABLE {tbl} RENAME TO {tbl}_old;")
                if tbl == "approvals":
                    conn.execute("""
                    CREATE TABLE approvals (
                        id TEXT PRIMARY KEY,
                        owner_id TEXT NOT NULL,
                        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                        step_id TEXT NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
                        tool TEXT NOT NULL,
                        args_json TEXT NOT NULL DEFAULT '{}',
                        reason TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied')),
                        created_at TEXT NOT NULL,
                        resolved_at TEXT
                    );
                    """)
                elif tbl == "drift_events":
                    conn.execute("""
                    CREATE TABLE drift_events (
                        id TEXT PRIMARY KEY,
                        task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                        step_id TEXT NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
                        tool TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        explanation TEXT,
                        detected_at TEXT NOT NULL
                    );
                    """)
                cols = [row["name"] for row in conn.execute(f"PRAGMA table_info({tbl}_old)").fetchall()]
                common = ", ".join(cols)
                conn.execute(f"INSERT INTO {tbl} ({common}) SELECT {common} FROM {tbl}_old;")
                conn.execute(f"DROP TABLE {tbl}_old;")

        # 4. Migrate llm_calls table columns if missing
        llm_cols = {row["name"] for row in conn.execute("PRAGMA table_info(llm_calls)").fetchall()}
        if "owner_id" not in llm_cols:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'default';")
        if "context_tags" not in llm_cols:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN context_tags TEXT DEFAULT '[]';")
        if "input_chars" not in llm_cols:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN input_chars INTEGER DEFAULT 0;")
        if "output_chars" not in llm_cols:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN output_chars INTEGER DEFAULT 0;")
        if "redaction_count" not in llm_cols:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN redaction_count INTEGER DEFAULT 0;")
        if "dropped_tags" not in llm_cols:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN dropped_tags INTEGER DEFAULT 0;")

        conn.executescript(INDEXES_SQL)


def check_daily_llm_limit(owner_id: Optional[str] = None, db_path: Optional[str] = None) -> None:
    """Check if daily LLM call cap has been exceeded per-owner or globally in the last 24 hours."""
    global_cap_str = os.getenv("SAHA_DAILY_LLM_CALL_CAP", "500")
    try:
        global_cap = int(global_cap_str)
    except ValueError:
        global_cap = 500

    owner_cap_str = os.getenv("SAHA_OWNER_DAILY_LLM_CAP", "100")
    try:
        owner_cap = int(owner_cap_str)
    except ValueError:
        owner_cap = 100

    effective_owner = owner_id or "default"

    init_db(db_path)
    with get_db(db_path) as conn:
        # Check per-owner cap
        row_owner = conn.execute(
            """
            SELECT COUNT(*) as cnt
            FROM llm_calls
            WHERE owner_id = ? AND created_at >= datetime('now', '-24 hours')
            """,
            (effective_owner,),
        ).fetchone()
        owner_cnt = row_owner["cnt"] if row_owner else 0
        if owner_cnt >= owner_cap:
            raise RuntimeError(
                f"Owner daily LLM call cap of {owner_cap} calls exceeded (429: Rate limit exceeded). Current usage: {owner_cnt}"
            )

        # Check global cap
        row_global = conn.execute(
            """
            SELECT COUNT(*) as cnt
            FROM llm_calls
            WHERE created_at >= datetime('now', '-24 hours')
            """
        ).fetchone()
        global_cnt = row_global["cnt"] if row_global else 0
        if global_cnt >= global_cap:
            raise RuntimeError(
                f"Global daily LLM call cap of {global_cap} calls exceeded (429: Rate limit exceeded). Current usage: {global_cnt}"
            )


def record_llm_call(
    model: str,
    latency_ms: float,
    role: Optional[str] = None,
    owner_id: Optional[str] = None,
    context_tags: Optional[List[str]] = None,
    input_chars: int = 0,
    output_chars: int = 0,
    redaction_count: int = 0,
    dropped_tags: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    status: str = "success",
    error: Optional[str] = None,
    db_path: Optional[str] = None,
) -> str:
    """Record LLM call telemetry and privacy ledger metadata into SQLite."""
    init_db(db_path)
    call_id = str(uuid.uuid4())
    now = utc_now_iso()
    tags_json = json.dumps(context_tags or [])
    effective_owner = owner_id or "default"
    with get_db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO llm_calls (
                id, owner_id, model, role, context_tags, input_chars, output_chars,
                redaction_count, dropped_tags, latency_ms, prompt_tokens, completion_tokens,
                total_tokens, status, error, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                call_id,
                effective_owner,
                model,
                role or "unknown",
                tags_json,
                int(input_chars or 0),
                int(output_chars or 0),
                int(redaction_count or 0),
                int(dropped_tags or 0),
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


def get_llm_metrics(owner_id: Optional[str] = None, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Calculate average latency and token usage grouped by model and role."""
    init_db(db_path)
    with get_db(db_path) as conn:
        where_clause = "WHERE status = 'success'"
        params: List[Any] = []
        if owner_id:
            where_clause += " AND owner_id = ?"
            params.append(owner_id)

        rows = conn.execute(
            f"""
            SELECT
                model,
                role,
                COUNT(*) as call_count,
                AVG(latency_ms) as avg_latency_ms,
                SUM(prompt_tokens) as total_prompt_tokens,
                SUM(completion_tokens) as total_completion_tokens,
                SUM(total_tokens) as total_tokens
            FROM llm_calls
            {where_clause}
            GROUP BY model, role
            ORDER BY call_count DESC
            """,
            params,
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
        cnt_query = "SELECT COUNT(*) as cnt FROM llm_calls"
        if owner_id:
            total_calls = conn.execute(f"{cnt_query} WHERE owner_id = ?", (owner_id,)).fetchone()["cnt"]
        else:
            total_calls = conn.execute(cnt_query).fetchone()["cnt"]
        return {
            "total_calls": total_calls,
            "models": models_data,
        }


def get_owner_egress_logs(owner_id: str, limit: int = 100, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return owner's privacy ledger egress logs with character and redaction counts (NO content)."""
    init_db(db_path)
    with get_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT id, owner_id, model, role, context_tags, input_chars, output_chars,
                   redaction_count, dropped_tags, latency_ms, total_tokens, status, error, created_at
            FROM llm_calls
            WHERE owner_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (owner_id, limit),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "owner_id": r["owner_id"],
                "model": r["model"],
                "role": r["role"],
                "tags": json.loads(r["context_tags"]) if r["context_tags"] else [],
                "context_tags": json.loads(r["context_tags"]) if r["context_tags"] else [],
                "input_chars": r["input_chars"],
                "output_chars": r["output_chars"],
                "redaction_count": r["redaction_count"],
                "dropped_tags": r["dropped_tags"] if "dropped_tags" in r.keys() else 0,
                "latency_ms": r["latency_ms"],
                "total_tokens": r["total_tokens"],
                "status": r["status"],
                "error": r["error"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


def delete_owner_space(owner_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Delete all database records associated with an owner."""
    init_db(db_path)
    with get_db(db_path) as conn:
        task_rows = conn.execute("SELECT id FROM tasks WHERE owner_id = ?", (owner_id,)).fetchall()
        deleted_tasks = len(task_rows)
        conn.execute("DELETE FROM tasks WHERE owner_id = ?", (owner_id,))
        conn.execute("DELETE FROM approvals WHERE owner_id = ?", (owner_id,))
        conn.execute("DELETE FROM policies WHERE owner_id = ?", (owner_id,))
        conn.execute("DELETE FROM llm_calls WHERE owner_id = ?", (owner_id,))
    return {"deleted_tasks": deleted_tasks}


def get_task_owner_id(task_id: str, db_path: Optional[str] = None) -> Optional[str]:
    """Look up owner_id for a given task ID."""
    try:
        init_db(db_path)
        with get_db(db_path) as conn:
            r = conn.execute("SELECT owner_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if r and "owner_id" in r.keys():
                return r["owner_id"]
    except Exception:
        pass
    return None
