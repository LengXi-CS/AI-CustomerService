"""从 cpolar 本机 Web API 读取当前隧道的公网 https 地址。

背景：cpolar 免费版域名每次重连都会漂移（本次部署已见证 704cba75 → 764c924 → 2d7230d6），
需要有一个**权威来源**把它同步进 .env。日志不可靠（nssm 会截断/轮转 logs\\cpolar.out.log，
容易出现新旧记录交织），cpolar 自己暴露的 Web API 才是准确答案。

用法：
    python scripts/get_public_url.py

输出：
    成功时向 stdout 打印一行 https 地址（纯 ASCII，便于被 PowerShell 安全捕获）
退出码：
    0 成功；1 取不到
"""

from __future__ import annotations

import re
import sys
import urllib.request

API_URL = "http://127.0.0.1:4040/api/tunnels"
HOST_RE = re.compile(r"https://([0-9A-Za-z\-]+\.cpolar\.top)")


def _extract_urls(body: str) -> list[str]:
    """优先按 JSON 结构取 PublicUrl，失败则退回正则扫描。"""
    # 页面里是 window.data = JSON.parse("{\"Txns\":...}")
    unescaped = body.replace('\\"', '"')
    urls = re.findall(r'"PublicUrl"\s*:\s*"(https://[^"]+)"', unescaped)
    if not urls:
        urls = [f"https://{h}" for h in HOST_RE.findall(body)]
    return list(dict.fromkeys(urls))  # 去重且保持顺序


def main() -> int:
    try:
        req = urllib.request.Request(API_URL, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: 无法访问 cpolar 本机 API {API_URL}: {exc}", file=sys.stderr)
        return 1

    urls = _extract_urls(body)
    if not urls:
        print("ERROR: cpolar API 响应里没有找到隧道地址", file=sys.stderr)
        return 1

    print(urls[0])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
