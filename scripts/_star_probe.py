"""Scratch: sample fields that could collide with markdown emphasis markers."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def main() -> None:
    con = sqlite3.connect("file:data/read-model.db?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    lines: list[str] = []

    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    lines.append("表: " + ", ".join(tables))

    def sample(sql: str, title: str) -> None:
        lines.append(f"\n=== {title} ===")
        try:
            rows = list(con.execute(sql))
        except sqlite3.Error as exc:
            lines.append(f"  查询失败: {exc}")
            return
        lines.extend(f"  {dict(r)}" for r in rows)

    sample(
        "SELECT DISTINCT product_size FROM skus WHERE product_size IS NOT NULL LIMIT 15",
        "skus.product_size 样本",
    )
    sample(
        "SELECT COUNT(*) n FROM skus WHERE product_size LIKE '%*%'",
        "含 * 的 product_size 条数",
    )
    sample(
        "SELECT COUNT(*) n FROM skus WHERE product_name LIKE '%*%'",
        "含 * 的 product_name 条数",
    )
    sample(
        "SELECT DISTINCT product_name FROM skus WHERE product_name LIKE '%*%' LIMIT 10",
        "含 * 的 product_name 明细",
    )
    con.close()
    Path("_star_probe.txt").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
