"""Small durable job and telemetry store; never stores inference content."""

from __future__ import annotations

import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path


class State:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = directory / "state.sqlite3"
        with closing(self.connect()) as con:
            con.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, action TEXT NOT NULL, model TEXT NOT NULL, profile TEXT NOT NULL,
                    state TEXT NOT NULL, message TEXT NOT NULL, source TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS samples (
                    timestamp REAL PRIMARY KEY, ram_used_bytes INTEGER, vram_used_bytes INTEGER
                );
            """)
        self.db.chmod(0o600)

    def connect(self):
        con = sqlite3.connect(self.db, timeout=10)
        con.row_factory = sqlite3.Row
        return con

    def recover(self) -> None:
        with closing(self.connect()) as con, con:
            con.execute("""UPDATE jobs SET state='interrupted', message='Controller process exited',
                           updated_at=? WHERE state IN ('queued', 'running')""", (time.time(),))

    def add(self, action: str, model: str, profile: str, source: str) -> dict:
        now = time.time()
        job = {"id": uuid.uuid4().hex, "action": action, "model": model, "profile": profile,
               "state": "queued", "message": "", "source": source, "created_at": now, "updated_at": now}
        with closing(self.connect()) as con, con:
            con.execute("INSERT INTO jobs VALUES (:id,:action,:model,:profile,:state,:message,"
                        ":source,:created_at,:updated_at)", job)
        return job

    def update(self, job: str, state: str, message: str) -> None:
        with closing(self.connect()) as con, con:
            con.execute("UPDATE jobs SET state=?,message=?,updated_at=? WHERE id=?",
                        (state, message[:1500], time.time(), job))

    def jobs(self, limit: int = 75) -> list[dict]:
        with closing(self.connect()) as con:
            return [dict(row) for row in con.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def get(self, key: str) -> str | None:
        with closing(self.connect()) as con:
            row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        with closing(self.connect()) as con, con:
            con.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, value))

    def sample(self, timestamp: float, ram_used: int | None, vram_used: int | None) -> None:
        with closing(self.connect()) as con, con:
            con.execute("INSERT OR REPLACE INTO samples VALUES (?,?,?)", (timestamp, ram_used, vram_used))
            con.execute("DELETE FROM samples WHERE timestamp < ?", (timestamp - 7 * 86400,))

    def stats(self) -> dict:
        now = time.time()
        with closing(self.connect()) as con:
            # Bucket to one minute and bound the plot payload even on a long-running instance.
            samples = [dict(row) for row in con.execute("""
                SELECT CAST(timestamp / 60 AS INTEGER) * 60 AS timestamp,
                       CAST(AVG(ram_used_bytes) AS INTEGER) AS ram_used_bytes,
                       CAST(AVG(vram_used_bytes) AS INTEGER) AS vram_used_bytes
                FROM samples WHERE timestamp >= ? GROUP BY 1 ORDER BY 1
            """, (now - 6 * 3600,))]
            jobs = [dict(row) for row in con.execute("""
                SELECT model, COUNT(*) AS finished,
                       SUM(CASE WHEN state = 'failed' THEN 1 ELSE 0 END) AS failed,
                       AVG(CASE WHEN action IN ('start','switch') AND state='done'
                           THEN updated_at-created_at ELSE NULL END) AS start_seconds
                FROM jobs WHERE created_at >= ? AND state IN ('done','failed') GROUP BY model
            """, (now - 24 * 3600,))]
        return {"samples": samples, "jobs_by_model": jobs,
                "metrics": {"requests": None, "tokens": None, "prompt_cache_hit_rate": None,
                            "expert_cache_hit_rate": None, "inference_error_rate": None}}
