"""SQLite 连接与迁移执行。"""

import os
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def database_path() -> Path:
    return Path(os.getenv("DATABASE_PATH", "data/app.sqlite3"))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
        connection.executescript(sql_file.read_text(encoding="utf-8"))
    connection.commit()
