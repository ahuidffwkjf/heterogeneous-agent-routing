"""SQLite-backed dynamic execution-unit registry.

The registry is deliberately small for the local prototype.  It stores the
unit's static capabilities and hardware description together with its latest
heartbeat and runtime state.  A production deployment can replace this class
with PostgreSQL/Redis without changing the Controller API.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable

from router import ExecutionUnit


class UnitRegistry:
    def __init__(self, database_path: str | Path = "registry.db") -> None:
        self.database_path = str(Path(database_path).expanduser().resolve())
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database_path,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_units (
                    unit_id TEXT PRIMARY KEY,
                    unit_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    last_seen REAL NOT NULL,
                    registered_at REAL NOT NULL,
                    heartbeat_required INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    job_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    @staticmethod
    def _unit_payload(unit: ExecutionUnit) -> dict[str, Any]:
        payload = asdict(unit)
        payload["platforms"] = sorted(unit.platforms)
        payload["capabilities"] = sorted(unit.capabilities)
        payload["tools"] = sorted(unit.tools)
        return payload

    def register(
        self,
        unit: ExecutionUnit,
        *,
        heartbeat_required: bool = True,
    ) -> ExecutionUnit:
        now = time.time()
        payload = self._unit_payload(unit)
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT registered_at FROM execution_units WHERE unit_id = ?",
                (unit.unit_id,),
            ).fetchone()
            registered_at = float(existing["registered_at"]) if existing else now
            self._connection.execute(
                """
                INSERT INTO execution_units
                    (unit_id, unit_json, state, last_seen, registered_at, heartbeat_required)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(unit_id) DO UPDATE SET
                    unit_json = excluded.unit_json,
                    state = excluded.state,
                    last_seen = excluded.last_seen,
                    heartbeat_required = excluded.heartbeat_required
                """,
                (
                    unit.unit_id,
                    json.dumps(payload, ensure_ascii=False),
                    unit.state,
                    now,
                    registered_at,
                    int(heartbeat_required),
                ),
            )
        return unit

    def heartbeat(
        self,
        unit_id: str,
        *,
        state: str | None = None,
        load: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ExecutionUnit:
        with self._lock:
            row = self._connection.execute(
                "SELECT unit_json FROM execution_units WHERE unit_id = ?",
                (unit_id,),
            ).fetchone()
            if row is None:
                raise KeyError(unit_id)
            payload = json.loads(row["unit_json"])
            if state is not None:
                payload["state"] = state
            if load is not None:
                payload["load"] = max(0.0, min(1.0, float(load)))
            if metadata:
                payload["metadata"] = {
                    **dict(payload.get("metadata", {})),
                    **metadata,
                }
            unit = ExecutionUnit.from_dict(payload)
            now = time.time()
            with self._connection:
                self._connection.execute(
                    """
                    UPDATE execution_units
                    SET unit_json = ?, state = ?, last_seen = ?
                    WHERE unit_id = ?
                    """,
                    (
                        json.dumps(self._unit_payload(unit), ensure_ascii=False),
                        unit.state,
                        now,
                        unit_id,
                    ),
                )
            return unit

    def mark_stale(self, timeout_seconds: float) -> list[str]:
        cutoff = time.time() - timeout_seconds
        with self._lock, self._connection:
            rows = self._connection.execute(
                """
                SELECT unit_id, unit_json FROM execution_units
                WHERE heartbeat_required = 1 AND last_seen < ? AND state != 'offline'
                """,
                (cutoff,),
            ).fetchall()
            stale_ids: list[str] = []
            for row in rows:
                payload = json.loads(row["unit_json"])
                payload["state"] = "offline"
                self._connection.execute(
                    "UPDATE execution_units SET unit_json = ?, state = ? WHERE unit_id = ?",
                    (json.dumps(payload, ensure_ascii=False), "offline", row["unit_id"]),
                )
                stale_ids.append(row["unit_id"])
            return stale_ids

    def get(self, unit_id: str) -> ExecutionUnit:
        with self._lock:
            row = self._connection.execute(
                "SELECT unit_json FROM execution_units WHERE unit_id = ?",
                (unit_id,),
            ).fetchone()
            if row is None:
                raise KeyError(unit_id)
            return ExecutionUnit.from_dict(json.loads(row["unit_json"]))

    def list_units(self, *, include_offline: bool = True) -> list[ExecutionUnit]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT unit_json FROM execution_units ORDER BY unit_id"
            ).fetchall()
            units = [ExecutionUnit.from_dict(json.loads(row["unit_json"])) for row in rows]
            if include_offline:
                return units
            return [unit for unit in units if unit.state != "offline"]

    def unregister(self, unit_id: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM execution_units WHERE unit_id = ?",
                (unit_id,),
            )

    def save_job(self, payload: dict[str, Any]) -> None:
        now = time.time()
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO jobs (job_id, job_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    job_json = excluded.job_json,
                    updated_at = excluded.updated_at
                """,
                (str(payload["job_id"]), json.dumps(payload, ensure_ascii=False), now),
            )

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT job_json FROM jobs ORDER BY updated_at"
            ).fetchall()
            return [json.loads(row["job_json"]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
