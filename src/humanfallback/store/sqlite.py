"""SQLite-backed persistence for Task Contracts.

The full contract is stored as JSON; a few columns are denormalised so
listing and filtering do not require deserialising every row.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from humanfallback.models import ContractStatus, TaskCategory, TaskContract

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    id          TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    category    TEXT NOT NULL,
    title       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    data        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contracts_status ON contracts (status);
CREATE INDEX IF NOT EXISTS contracts_category ON contracts (category);
"""


class ContractStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ContractStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def save(self, contract: TaskContract) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO contracts (id, status, category, title, created_at, updated_at, data)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    category = excluded.category,
                    title = excluded.title,
                    updated_at = excluded.updated_at,
                    data = excluded.data
                """,
                (
                    contract.id,
                    contract.status.value,
                    contract.classification.category.value,
                    contract.title,
                    contract.created_at.isoformat(),
                    contract.updated_at.isoformat(),
                    contract.model_dump_json(),
                ),
            )

    def get(self, contract_id: str) -> TaskContract | None:
        row = self._conn.execute(
            "SELECT data FROM contracts WHERE id = ?", (contract_id,)
        ).fetchone()
        return TaskContract.model_validate_json(row["data"]) if row else None

    def list(
        self,
        *,
        status: ContractStatus | None = None,
        category: TaskCategory | None = None,
    ) -> list[TaskContract]:
        clauses: list[str] = []
        params: list[str] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if category is not None:
            clauses.append("category = ?")
            params.append(category.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT data FROM contracts {where} ORDER BY created_at DESC", params
        ).fetchall()
        return [TaskContract.model_validate_json(r["data"]) for r in rows]

    def delete(self, contract_id: str) -> bool:
        with self._conn:
            cur = self._conn.execute("DELETE FROM contracts WHERE id = ?", (contract_id,))
        return cur.rowcount > 0

    def count_by_status(self) -> dict[ContractStatus, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM contracts GROUP BY status"
        ).fetchall()
        return {ContractStatus(r["status"]): r["n"] for r in rows}
