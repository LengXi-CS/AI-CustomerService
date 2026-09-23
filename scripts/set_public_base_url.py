"""把当前有效的公网回调根地址写入 .env。

cpolar 免费版的二级域名每次重启都会漂移，GeWe 控制台里的回调地址和
``GEWE_PUBLIC_BASE_URL`` 必须同步更新，否则微信附件外链会指向失效地址。

本脚本只改写 3 个键，且**不回显 .env 里的任何其它内容**，避免凭证泄漏。

用法::

    .venv\\Scripts\\python.exe scripts/set_public_base_url.py https://xxxx.r11.cpolar.top
"""

from __future__ import annotations

import sys
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

TARGET_KEYS = ("PUBLIC_BASE_URL", "GEWE_PUBLIC_BASE_URL", "GEWE_CALLBACK_URL")


def build_values(base: str) -> dict[str, str]:
    normalized = base.strip().rstrip("/")
    if not normalized.startswith(("http://", "https://")):
        raise ValueError("公网地址必须以 http:// 或 https:// 开头")
    return {
        "PUBLIC_BASE_URL": normalized,
        "GEWE_PUBLIC_BASE_URL": normalized,
        "GEWE_CALLBACK_URL": f"{normalized}/api/wechat/callback",
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法: set_public_base_url.py <公网根地址>", file=sys.stderr)
        return 2
    values = build_values(argv[1])

    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    replaced: set[str] = set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in values:
            out.append(f"{key}={values[key]}")
            replaced.add(key)
        else:
            out.append(line)
    for key, value in values.items():
        if key not in replaced:
            out.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("已更新:", ", ".join(f"{k}({'覆盖' if k in replaced else '新增'})" for k in values))
    print("回调地址:", values["GEWE_CALLBACK_URL"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
