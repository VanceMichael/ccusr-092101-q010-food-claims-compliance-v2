
import os
import sqlite3
from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def migrate(database_path: Path) -> list[str]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    applied: list[str] = []
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        done = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = path.stem
            if version in done:
                continue
            connection.executescript(path.read_text(encoding="utf-8"))
            # 迁移文件可能自带登记语句；补齐未自登记的
            connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (?)", (version,)
            )
            applied.append(version)
    return applied


if __name__ == "__main__":
    target = Path(os.getenv("DATABASE_PATH", "data/app.sqlite3"))
    versions = migrate(target)
    if versions:
        print("应用迁移：" + ", ".join(versions))
    print(f"数据库迁移完成：{target}")
