
from app.db import connect, database_path, migrate


def main() -> None:
    path = database_path()
    with connect(path) as connection:
        migrate(connection)
    print(f"数据库迁移完成：{path}")


if __name__ == "__main__":
    main()
