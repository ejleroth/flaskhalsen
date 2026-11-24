"""SQLite storage: recording sessions, samples, token usage, settings."""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from .util import DATA_DIR

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(exist_ok=True)
        _conn = sqlite3.connect(DATA_DIR / "flaskhalsen.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, label TEXT, started REAL,
                ended REAL, planned_seconds INTEGER);
            CREATE TABLE IF NOT EXISTS samples (session_id INTEGER, ts REAL, metrics TEXT);
            CREATE INDEX IF NOT EXISTS idx_samples ON samples(session_id, ts);
            CREATE TABLE IF NOT EXISTS tokens (ts REAL, model TEXT, prompt_tokens INTEGER,
                eval_tokens INTEGER, eval_seconds REAL, purpose TEXT);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
        """)
    return _conn


def execute(sql: str, args=()) -> sqlite3.Cursor:
    with _lock:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur


def query(sql: str, args=()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


# --- sessions -------------------------------------------------------------------

def new_session(label: str, seconds: int) -> int:
    return execute("INSERT INTO sessions(label, started, planned_seconds) VALUES (?,?,?)",
                   (label, time.time(), seconds)).lastrowid


def end_session(sid: int) -> None:
    execute("UPDATE sessions SET ended=? WHERE id=?", (time.time(), sid))


def add_sample(sid: int, ts: float, metrics: dict) -> None:
    execute("INSERT INTO samples(session_id, ts, metrics) VALUES (?,?,?)", (sid, ts, json.dumps(metrics)))


def sessions() -> list[dict]:
    return query("""SELECT s.*, COUNT(x.ts) AS n FROM sessions s LEFT JOIN samples x ON x.session_id = s.id
                    GROUP BY s.id ORDER BY s.id DESC LIMIT 50""")


def samples(sid: int) -> list[dict]:
    rows = query("SELECT ts, metrics FROM samples WHERE session_id=? ORDER BY ts", (sid,))
    return [{"ts": r["ts"], **json.loads(r["metrics"])} for r in rows]


# --- tokens ---------------------------------------------------------------------

def add_tokens(model: str, prompt: int, evals: int, seconds: float, purpose: str) -> None:
    execute("INSERT INTO tokens VALUES (?,?,?,?,?,?)", (time.time(), model, prompt, evals, seconds, purpose))


def token_totals() -> dict:
    r = query("""SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(eval_tokens),0) e,
                        COALESCE(SUM(eval_seconds),0) s, COUNT(*) n FROM tokens""")[0]
    per_model = query("""SELECT model, SUM(prompt_tokens) p, SUM(eval_tokens) e,
                         SUM(eval_tokens)/NULLIF(SUM(eval_seconds),0) tps FROM tokens GROUP BY model""")
    return {"prompt_tokens": r["p"], "generated_tokens": r["e"], "total_tokens": r["p"] + r["e"],
            "calls": r["n"], "per_model": per_model}


# --- settings -------------------------------------------------------------------

def get_setting(key: str, default=None):
    rows = query("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(rows[0]["value"]) if rows else default


def set_setting(key: str, value) -> None:
    execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))
