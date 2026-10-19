"""只读探针：查看 frp / cpolar 的本机现状。不修改任何东西。"""
import os
import subprocess

def run(args):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:  # noqa: BLE001
        return -1, f"<error {e}>"

print("=== 1. D:\\tools\\frp 目录 ===")
for d in [r"D:\tools\frp", r"D:\tools"]:
    print(f"-- {d}")
    if os.path.isdir(d):
        for n in sorted(os.listdir(d)):
            p = os.path.join(d, n)
            kind = "DIR " if os.path.isdir(p) else "FILE"
            size = "" if os.path.isdir(p) else f"{os.path.getsize(p):>10}"
            print(f"   {kind} {size}  {n}")
    else:
        print("   <不存在>")

print("\n=== 2. frpc.toml 是否存在（D:\\tools\\frp）===")
cfg = r"D:\tools\frp\frpc.toml"
print("exists:", os.path.exists(cfg))
if os.path.exists(cfg):
    with open(cfg, encoding="utf-8") as f:
        for line in f:
            s = line.rstrip("\n")
            low = s.lower()
            # 不回显密钥类内容
            if "token" in low or "password" in low:
                s = s.split("=")[0] + "= <redacted>"
            print("   " + s)

print("\n=== 3. 相关 Windows 服务 ===")
rc, out = run(["powershell", "-NoProfile", "-Command",
               "Get-Service | Where-Object {$_.Name -match 'frp|cpolar|FastGpt'} | "
               "Select-Object Name,Status,StartType | Format-Table -AutoSize | Out-String -Width 200"])
print(out.strip())

print("\n=== 4. 监听端口 (5330 / 7000 / 7500 / 7400 / 4040) ===")
rc, out = run([r"C:\Windows\System32\netstat.exe", "-ano"])
for line in out.splitlines():
    if "LISTENING" in line and any(f":{p} " in line for p in (5330, 7000, 7500, 7400, 4040)):
        print("  " + line.strip())

print("\n=== 5. 到 frps 服务器的连通性（如配置里有 serverAddr）===")
srv = None
if os.path.exists(cfg):
    for line in open(cfg, encoding="utf-8"):
        if line.strip().startswith("serverAddr"):
            srv = line.split("=", 1)[1].strip().strip('"')
            break
print("serverAddr in config:", srv or "<无配置文件/未设置>")

print("\n=== 6. cpolar 本机 API 是否有隧道 ===")
try:
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=5) as r:
        print(r.read().decode("utf-8", "replace")[:1200])
except Exception as e:  # noqa: BLE001
    print("  取不到（cpolar 未运行或无该 API）:", e)

print("\n=== 7. 本机 5330 /health ===")
try:
    import urllib.request
    with urllib.request.urlopen("http://127.0.0.1:5330/health", timeout=5) as r:
        print("  HTTP", r.status, r.read().decode("utf-8", "replace")[:300])
except Exception as e:  # noqa: BLE001
    print("  失败:", e)
