import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator


class IdempotencyConflictError(Exception):
    """A request ID was reused with a different request payload."""


@dataclass(frozen=True)
class RequestRecord:
    status: str
    response: dict[str, Any] | None = None
    detail: str | None = None
    attempts: int = 0


class RequestStore:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    response_json TEXT,
                    detail TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL
                )
                """
            )
            connection.commit()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def claim(
        self,
        request_id: str,
        request_data: dict[str, Any],
        lease_seconds: float,
    ) -> RequestRecord:
        request_json = json.dumps(
            request_data,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        request_hash = hashlib.sha256(request_json.encode("utf-8")).hexdigest()
        now = time.time()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()

            if row is None:
                connection.execute(
                    """
                    INSERT INTO requests (
                        request_id, request_hash, request_json, status,
                        created_at, updated_at, lease_expires_at
                    ) VALUES (?, ?, ?, 'in_progress', ?, ?, ?)
                    """,
                    (
                        request_id,
                        request_hash,
                        request_json,
                        now,
                        now,
                        now + lease_seconds,
                    ),
                )
                connection.commit()
                return RequestRecord(status="claimed")

            if row["request_hash"] != request_hash:
                connection.rollback()
                raise IdempotencyConflictError(
                    "request_id was already used with a different payload"
                )

            status = row["status"]
            detail = row["detail"]
            if status == "in_progress" and row["lease_expires_at"] <= now:
                status = "unknown"
                detail = "The previous worker did not record a final result"
                connection.execute(
                    """
                    UPDATE requests
                    SET status = ?, detail = ?, updated_at = ?
                    WHERE request_id = ? AND status = 'in_progress'
                    """,
                    (status, detail, now, request_id),
                )

            connection.commit()
            response = (
                json.loads(row["response_json"])
                if row["response_json"] is not None
                else None
            )
            return RequestRecord(
                status=status,
                response=response,
                detail=detail,
                attempts=row["attempts"],
            )

    def mark_success(self, request_id: str, response: dict[str, Any]) -> None:
        self._finish(
            request_id,
            status="success",
            response_json=json.dumps(response, separators=(",", ":")),
            detail=None,
            attempts=response["attempts"],
        )

    def mark_unknown(self, request_id: str, detail: str, attempts: int) -> None:
        self._finish(
            request_id,
            status="unknown",
            response_json=None,
            detail=detail,
            attempts=attempts,
        )

    def mark_failed(self, request_id: str, detail: str, attempts: int) -> None:
        self._finish(
            request_id,
            status="failed",
            response_json=None,
            detail=detail,
            attempts=attempts,
        )

    def _finish(
        self,
        request_id: str,
        status: str,
        response_json: str | None,
        detail: str | None,
        attempts: int,
    ) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE requests
                SET status = ?, response_json = ?, detail = ?, attempts = ?, updated_at = ?
                WHERE request_id = ? AND status = 'in_progress'
                """,
                (status, response_json, detail, attempts, time.time(), request_id),
            )
            connection.commit()
            if cursor.rowcount != 1:
                raise RuntimeError(f"Request {request_id} is not in progress")
