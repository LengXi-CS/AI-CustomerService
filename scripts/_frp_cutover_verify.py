"""验收：从公网 frp 入口 POST 一次空回调，确认请求真的到达应用并被正确忽略。

空 JSON 会被 parse_gewe_callback 判为无效事件 -> 返回 {"ret":200,"msg":"ignored"}，
不写状态库、不向客户发消息（沿用既往会话已验证的探针方式）。
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request

LOG = r"D:\瀚思AI客服\logs\5330.err.log"
FRP_CB = "http://115.190.250.104:9481/api/wechat/callback"


def run(args, timeout=30):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        return (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return f"<error {e}>"


def tail_log(path, n=25):
    if not os.path.exists(path):
        return "<日志不存在>"
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 12000))
            raw = f.read().decode("utf-8", "replace")
        return "\n".join(raw.splitlines()[-n:])
    except Exception as e:  # noqa: BLE001
        return f"<读取失败 {e}>"


def sect(t):
    print("\n" + "=" * 66)
    print(t)
    print("=" * 66)


sect("A. 探针前的 media_base_url（从日志取最近一次记录）")
before = tail_log(LOG, 400)
last = [ln for ln in before.splitlines() if "current_media_base_url" in ln]
print("  " + (last[-1] if last else "<近期无记录>"))

sect("B. 从公网 frp 入口 POST 空回调")
req = urllib.request.Request(
    FRP_CB,
    data=b"{}",
    headers={"Content-Type": "application/json",
             "User-Agent": "frp-cutover-probe/1.0"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(f"  POST {FRP_CB}")
        print(f"  -> HTTP {r.status}  {r.read(200).decode('utf-8', 'replace')}")
except urllib.error.HTTPError as e:
    print(f"  -> HTTP {e.code}  {e.read(200).decode('utf-8', 'replace')}")
except Exception as e:  # noqa: BLE001
    print(f"  -> 失败 ({type(e).__name__}: {e})")

time.sleep(3)

sect("C. 应用日志新增行（关键：WeChat callback headers）")
after = tail_log(LOG, 400)
new = [ln for ln in after.splitlines() if ln not in before.splitlines()]
print("\n".join("  " + ln for ln in new) if new else "  <无新增日志行>")

sect("D. 结论行提取")
hdr = [ln for ln in after.splitlines() if "WeChat callback headers" in ln]
print("  " + (hdr[-1] if hdr else "<未找到 WeChat callback headers 行>"))

sect("E. 会话状态库未被写入（探针无副作用确认）")
py = r"D:\瀚思AI客服\.venv\Scripts\python.exe"
if os.path.exists(py):
    out = run([py, r"D:\瀚思AI客服\scripts\check_pending_tasks.py"])
    print("\n".join("  " + ln for ln in out.splitlines()[:20]))
else:
    print("  未找到 venv python，跳过")

sect("F. 新公网 origin 下，附件外链基址是否合法（代码口径复核）")
fh = re.search(r"host=([^,\s]+)", hdr[-1]) if hdr else None
print("  回调到达时 Host =", fh.group(1) if fh else "<未知>")
print("  _public_base_url 只拒绝 127./localhost/0.0.0.0/[::1]，IP:端口 会被接受 ->", end=" ")
print("合法 ✓" if fh and not fh.group(1).startswith(("127.", "localhost", "0.0.0.0")) else "需人工确认")
