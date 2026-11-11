"""
results_table.py  –  The conversation's results table: any columns, de-duplicated rows.

Rows always carry every column (empty string when missing), so the column order
travels with the rows: it is the key order of the first row.

A chat turn and a background job may fill the same table from different threads,
so every change takes the table's lock. While a job is filling the table
(`job_lock` is set), it can't be replaced or cleared and its key columns stay fixed.
"""

from __future__ import annotations

import threading

Row = dict[str, str]


class ResultsTable:
    def __init__(self, rows: list[Row] | None = None, conversation_id: str = "") -> None:
        rows = rows or []
        self.conversation_id = conversation_id
        self.job_lock = ""
        self._lock = threading.RLock()
        self.columns: list[str] = list(rows[0].keys()) if rows else []
        self.key_fields: list[str] = list(self.columns)
        self.rows: list[Row] = [self._fit(r) for r in rows]
        self._reindex()

    def __len__(self) -> int:
        return len(self.rows)

    def _fit(self, row: Row) -> Row:
        return {c: str(row.get(c) or "").strip() for c in self.columns}

    def _key(self, row: Row) -> tuple[str, ...]:
        fields = self.key_fields if any(row.get(c) for c in self.key_fields) else self.columns
        return tuple(row.get(c, "").casefold() for c in fields)

    def _reindex(self) -> None:
        self._index: dict[tuple[str, ...], Row] = {}
        for row in self.rows:
            self._index.setdefault(self._key(row), row)

    def prepare(
        self,
        fields: list[str],
        key_fields: list[str] | None = None,
        replace: bool = False,
    ) -> str | None:
        """
        Make the table ready for rows with *fields*. Returns an error message when
        the fields don't overlap the existing columns and *replace* is not set.
        """
        fields = list(dict.fromkeys(f.strip() for f in fields if f and f.strip()))
        if not fields:
            return "Error: 'fields' must list at least one column name."
        with self._lock:
            if self.job_lock and (replace or not set(fields) & set(self.columns)):
                return f"Error: {self.job_lock}"
            if replace or not self.rows:
                self.columns = fields
                self.rows = []
            elif not set(fields) & set(self.columns):
                return (
                    f"Error: the results table already has {len(self.rows)} rows with columns "
                    f"{self.columns}. Ask the user whether to replace them, or pass replace=true "
                    "if they asked for a new table."
                )
            else:
                self.columns += [f for f in fields if f not in self.columns]
                self.rows = [self._fit(r) for r in self.rows]
            if not self.job_lock or not self.key_fields:
                self.key_fields = [k for k in (key_fields or fields) if k in self.columns] or fields
            self._reindex()
            return None

    def add(self, rows: list[Row]) -> tuple[int, int]:
        """
        Add rows; a row whose key matches an existing one fills that row's empty
        cells instead. Returns (rows added, rows updated).
        """
        added = updated = 0
        with self._lock:
            for raw in rows:
                row = self._fit(raw)
                if not any(row.values()):
                    continue
                key = self._key(row)
                existing = self._index.get(key)
                if existing is None:
                    self._index[key] = row
                    self.rows.append(row)
                    added += 1
                    continue
                filled = False
                for column, value in row.items():
                    if value and not existing[column]:
                        existing[column] = value
                        filled = True
                updated += filled
        return added, updated

    def clear(self) -> str | None:
        """Empty the table; returns an error message while a job is filling it."""
        with self._lock:
            if self.job_lock:
                return f"Error: {self.job_lock}"
            self.columns, self.key_fields, self.rows = [], [], []
            self._reindex()
            return None

    def snapshot(self) -> list[Row]:
        with self._lock:
            return [dict(r) for r in self.rows]
