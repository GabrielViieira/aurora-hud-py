"""SQLite local com migrações versionadas.

Escolha da Fase 1: `sqlite3` da stdlib, WAL e uma única conexão protegida por
lock. Sem ORM para não amarrar o núcleo a uma biblioteca que ainda não está
instalada. As migrações são arquivos `.sql` aplicados em ordem.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from aurora.observability import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), check_same_thread=False, isolation_level=None
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")

    # ------------------------------------------------------------------ infra
    def migrate(self, migrations_dir: Path | None = None) -> list[str]:
        directory = Path(migrations_dir or MIGRATIONS_DIR)
        applied: list[str] = []
        with self._lock:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            existing = {
                row["version"]
                for row in self._connection.execute("SELECT version FROM schema_migrations")
            }
            for sql_file in sorted(directory.glob("*.sql")):
                version = sql_file.stem
                if version in existing:
                    continue
                script = sql_file.read_text(encoding="utf-8")
                try:
                    self._connection.executescript(script)
                    self._connection.execute(
                        "INSERT INTO schema_migrations (version, applied_at) "
                        "VALUES (?, datetime('now'))",
                        (version,),
                    )
                except sqlite3.Error:
                    logger.exception("falha ao aplicar migração %s", version)
                    raise
                applied.append(version)
        if applied:
            logger.info("migrações aplicadas: %s", ", ".join(applied))
        return applied

    # -------------------------------------------------------------- operações
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            cursor = self._connection.execute(sql, tuple(params))
            return int(cursor.lastrowid or 0)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._connection.executemany(sql, [tuple(item) for item in seq_of_params])

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection.execute(sql, tuple(params)))

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, tuple(params)).fetchone()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                yield self._connection
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
            else:
                self._connection.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
