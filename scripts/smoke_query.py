"""Live smoke probe against the running 5330 service.

Hits the local /api/v1/query endpoint (no WeChat traffic, no state writes) and
dumps the raw bubble text so we can eyeball JSON-envelope leakage and residual
markdown artifacts after a restart.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

QUERIES = [
    "查询商品 6976070461130 的信息与关联工厂",
    "EO灭菌和电子束灭菌有什么区别？",
    "帮我汇总一下瀚思一次性枕套这个商品的规格和工厂",
]

ENVELOPE_HINT = re.compile(r'^\s*[\{\[]|"reply"\s*:|"answer"\s*:')
MD_HINT = re.compile(r"\*\*|^#{1,6}\s|```|^\s*[-*]\s+", re.MULTILINE)


def main() -> int:
    lines: list[str] = []
    leaks = 0
    for q in QUERIES:
        body = json.dumps({"query": q, "user_id": "deploy_probe"}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:5330/api/v1/query",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            lines.append(f"Q: {q}")
            lines.append(f"  FAIL {type(exc).__name__}: {exc}")
            lines.append("")
            continue

        answer = (data.get("answer") or "").strip()
        elapsed = time.time() - t0
        envelope = bool(ENVELOPE_HINT.search(answer))
        md = bool(MD_HINT.search(answer))
        if envelope:
            leaks += 1
        lines.append(f"Q: {q}")
        lines.append(f"  用时 {elapsed:.1f}s  channel={data.get('channel')}")
        env_flag = "是" if envelope else "否"
        md_flag = "是" if md else "否"
        lines.append(f"  信封泄漏={env_flag}  markdown残留={md_flag}")
        lines.append(f"  deliverables={len(data.get('deliverables') or [])}")
        lines.append("  --- 客户气泡原文 ---")
        for ln in answer.splitlines() or [""]:
            lines.append(f"  | {ln}")
        lines.append("")

    lines.append(f"=== 汇总: {len(QUERIES)} 题, 信封泄漏 {leaks} 题 ===")
    Path("_live_query.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"leaks={leaks}/{len(QUERIES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
