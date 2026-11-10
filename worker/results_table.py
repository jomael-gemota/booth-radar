"""
results_table.py  –  The conversation's results table: any columns, de-duplicated rows.

Rows always carry every column (empty string when missing), so the column order
travels with the rows: it is the key order of the first row.
"""

from __future__ import annotations

Row = dict[str, str]


class ResultsTable:
    def __init__(self, rows: list[Row] | None = None) -> None:
        rows = rows or []
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
        self.key_fields = [k for k in (key_fields or fields) if k in self.columns] or fields
        self._reindex()
        return None

    def add(self, rows: list[Row]) -> tuple[int, int]:
        """
        Add rows; a row whose key matches an existing one fills that row's empty
        cells instead. Returns (rows added, rows updated).
        """
        added = updated = 0
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

    def clear(self) -> None:
        self.columns, self.key_fields, self.rows = [], [], []
        self._reindex()

    def snapshot(self) -> list[Row]:
        return [dict(r) for r in self.rows]
