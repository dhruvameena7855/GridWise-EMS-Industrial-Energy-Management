from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DB = Path(os.getenv("GRIDWISE_DB_PATH", Path(__file__).resolve().parents[1] / "gridwise.db"))


def _connect() -> sqlite3.Connection:
    DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DEFAULT_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def initialize() -> None:
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS simulation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                site_name TEXT NOT NULL,
                config_json TEXT NOT NULL,
                result_json TEXT NOT NULL
            )
        """)
        conn.commit()


def save_run(result: dict[str, Any]) -> int:
    config = result["config"]
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as conn:
        cursor = conn.execute(
            "INSERT INTO simulation_runs(created_at, site_name, config_json, result_json) VALUES (?, ?, ?, ?)",
            (created_at, config.get("site_name", "Demo Industrial Plant"),
             json.dumps(config), json.dumps(result, separators=(",", ":"))),
        )
        conn.commit()
        return int(cursor.lastrowid)


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, site_name, config_json, result_json FROM simulation_runs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        ).fetchall()
    items = []
    for row in rows:
        result = json.loads(row["result_json"])
        items.append({
            "id": row["id"], "created_at": row["created_at"], "site_name": row["site_name"],
            "config": json.loads(row["config_json"]),
            "metrics": {key: value["metrics"] for key, value in result["strategies"].items()},
        })
    return items


def get_run(run_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT result_json FROM simulation_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return json.loads(row["result_json"])
