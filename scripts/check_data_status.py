"""Diagnostic script to inspect bitable sync completeness and attachment status."""

import sqlite3
from pathlib import Path


def main() -> None:
    db_path = Path("data/read-model.db")
    if not db_path.exists():
        print(f"Database {db_path} does not exist!")
        return

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    print("=== 1. SNAPSHOT INFO ===")
    cur.execute(
        "SELECT snapshot_id, source_root, created_at, status, table_count, record_count "
        "FROM source_snapshots"
    )
    snapshots = cur.fetchall()
    for s in snapshots:
        print(
            f"Snapshot ID: {s[0]}, Created: {s[2]}, Status: {s[3]}, "
            f"Tables: {s[4]}, Records: {s[5]}"
        )

    print("\n=== 2. TABLE ROW COUNTS ===")
    tables = [
        "factories",
        "skus",
        "categories",
        "trademarks",
        "batch_reports",
        "attachments",
        "relations",
        "source_rows",
        "build_warnings",
    ]
    for t in tables:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            count = cur.fetchone()[0]
            print(f"  {t:16}: {count}")
        except Exception as e:
            print(f"  {t:16}: ERROR ({e})")

    print("\n=== 3. ATTACHMENT STATUS IN DB ===")
    cur.execute("SELECT status, COUNT(*) FROM attachments GROUP BY status")
    for status, count in cur.fetchall():
        print(f"  status '{status}': {count}")

    print("\n=== 4. ATTACHMENT TYPES / FIELDS ===")
    cur.execute(
        "SELECT table_name, field_name, COUNT(*) "
        "FROM attachments GROUP BY table_name, field_name"
    )
    for tbl, field, count in cur.fetchall():
        print(f"  [{tbl}] {field}: {count}")

    print("\n=== 5. DISK STORAGE STATUS ===")
    attachment_dir = Path("data/resources/attachments")
    if attachment_dir.exists():
        files = list(attachment_dir.glob("*"))
        total_size = sum(f.stat().st_size for f in files if f.is_file())
        empty_files = [f.name for f in files if f.is_file() and f.stat().st_size == 0]
        print(f"  Directory: {attachment_dir}")
        print(f"  Total files on disk: {len(files)}")
        print(f"  Total size: {total_size / (1024 * 1024):.2f} MB")
        print(f"  Empty files: {len(empty_files)}")
    else:
        print(f"  Directory {attachment_dir} does not exist!")

    print("\n=== 6. BUILD WARNINGS (Top 10) ===")
    cur.execute("SELECT code, message FROM build_warnings LIMIT 10")
    warnings = cur.fetchall()
    if warnings:
        for w in warnings:
            print(f"  [{w[0]}] {w[1]}")
    else:
        print("  None")


if __name__ == "__main__":
    main()
