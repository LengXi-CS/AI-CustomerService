# -*- coding: utf-8 -*-
"""下载 frp windows_amd64 发行包到 D:\\tools\\frp（多镜像 + zip 完整性校验）。

只需 C 端 frpc.exe，但整包便于对照版本。下载后校验 zip 中央目录，失败自动换镜像。
"""
from __future__ import annotations

import io
import sys
import time
import zipfile
import urllib.request
from pathlib import Path

NAME = "frp_0.71.0_windows_amd64"
GH = f"https://github.com/fatedier/frp/releases/download/v0.71.0/{NAME}.zip"
MIRRORS = [
    "https://gh-proxy.com/",
    "https://ghfast.top/",
    "https://hub.gitmirror.com/",
    "https://ghproxy.cc/",
    "https://ghproxy.net/",
    "https://gh.llkk.cc/",
]
DEST_DIR = Path(r"D:\tools\frp")
TMP = Path(r"D:\tools\_frp_dl")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def log(msg: str) -> None:
    print(msg, flush=True)


def try_url(url: str, out: Path) -> tuple[bool, str]:
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=120) as r, open(out, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
        size = out.stat().st_size
        head = out.read_bytes()[:2]
        if head != b"PK":
            return False, f"非 zip（头部 {head!r}，{size} bytes）"
        try:
            with zipfile.ZipFile(out) as z:
                bad = z.testzip()
                names = z.namelist()
            if bad:
                return False, f"zip 损坏于 {bad}"
            if not any(n.endswith("frpc.exe") for n in names):
                return False, f"zip 内无 frpc.exe（{names[:3]}）"
        except zipfile.BadZipFile as e:
            return False, f"zip 不完整（{size} bytes{'' if not total else f'，期望 {total}'}）：{e}"
        return True, f"{size/1048576:.2f}MB / {time.time()-t0:.1f}s"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def extract(zip_path: Path) -> Path:
    import shutil

    if DEST_DIR.exists():
        shutil.rmtree(DEST_DIR, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        root = z.namelist()[0].split("/")[0]
        z.extractall(TMP)
    src = TMP / root
    shutil.move(str(src), str(DEST_DIR))
    shutil.rmtree(TMP, ignore_errors=True)
    return DEST_DIR


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    zip_path = TMP / f"{NAME}.zip"
    ok = False
    for base in MIRRORS:
        url = base + GH if not base.endswith("github.com") else base
        log(f"[try] {url}")
        ok, msg = try_url(url, zip_path)
        log(f"      -> {'OK' if ok else 'FAIL'} {msg}")
        if ok:
            break
    if not ok:
        log("!! 所有镜像均失败")
        log(f"   最后产物: {zip_path} ({zip_path.stat().st_size if zip_path.exists() else 0} bytes)")
        return 1
    dest = extract(zip_path)
    log(f"[ok] 已解压到 {dest}")
    for p in sorted(dest.iterdir()):
        log(f"     {p.name}  {p.stat().st_size/1048576:.2f}MB")
    frpc = dest / "frpc.exe"
    if frpc.exists():
        import subprocess

        r = subprocess.run([str(frpc), "-v"], capture_output=True, text=True)
        log(f"[ver] frpc {r.stdout.strip() or r.stderr.strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
