"""frp 切换前的只读实况探针。不修改任何东西，不打印任何密钥明文。"""
import json
import os
import re
import subprocess
import urllib.request

ROOT = r"D:\瀚思AI客服"
CLIENT = os.path.join(ROOT, "frc配置", "client")
MASK = ("TOKEN", "PWD", "PASSWORD", "SECRET", "KEY")


def run(args, timeout=40):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out.strip()
    except FileNotFoundError:
        return -1, "<不可执行/不存在>"
    except Exception as e:  # noqa: BLE001
        return -1, f"<error {e}>"


def sect(t):
    print("\n" + "=" * 68)
    print(t)
    print("=" * 68)


sect("1. Docker 是否已可用")
cands = [
    r"C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    r"C:\ProgramData\chocolatey\bin\docker.exe",
    r"C:\Users\THINK\AppData\Local\Microsoft\WindowsApps\docker.exe",
]
found = None
for c in cands:
    if os.path.exists(c):
        found = c
        break
print("  常见路径 docker.exe:", found or "全部不存在")
rc, out = run(["docker", "version", "--format", "{{.Server.Version}}"])
print("  裸 docker 命令:", (out[:200] or "<空>"))
if found:
    rc, out = run([found, "version"])
    print(f"  {found} ->", out[:300])

sect("2. Docker 容器 / compose")
docker_exe = found or "docker"
for label, args in [
    ("docker ps -a", [docker_exe, "ps", "-a", "--format", "{{.Names}}|{{.Image}}|{{.Status}}|{{.Ports}}"]),
    ("docker compose ls", [docker_exe, "compose", "ls"]),
    ("Docker 服务/进程", ["powershell", "-NoProfile", "-Command",
                          "Get-Process | Where-Object {$_.ProcessName -match 'docker|com.docker'} | "
                          "Select-Object -First 8 ProcessName,Id | Format-Table -AutoSize | Out-String"]),
]:
    rc, out = run(args)
    print(f"  -- {label} (exit={rc})")
    print("     " + (out.replace("\n", "\n     ") if out else "<空>"))

sect("3. WSL 状态")
for label, args in [
    ("wsl --status", ["wsl", "--status"]),
    ("wsl -l -v", ["wsl", "-l", "-v"]),
]:
    rc, out = run(args, timeout=25)
    print(f"  -- {label} (exit={rc}): {out[:400] or '<空>'}")

sect("4. Windows 可选功能（frp/Docker 前置）")
rc, out = run(["powershell", "-NoProfile", "-Command",
               "Get-ItemProperty -Path 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\PackageDetect\\*' "
               "-ErrorAction SilentlyContinue | Out-Null; "
               "'（需管理员才能读 dism，跳过）'"])
print("  " + out[:200])

sect("5. frc配置/client/.env（值脱敏）")
envp = os.path.join(CLIENT, ".env")
print("  路径:", envp, "| 存在:", os.path.exists(envp))
server_addr = None
server_port = None
if os.path.exists(envp):
    for line in open(envp, encoding="utf-8-sig"):
        t = line.rstrip("\n")
        if not t.strip() or t.strip().startswith("#"):
            continue
        if "=" not in t:
            continue
        k, v = t.split("=", 1)
        k, v = k.strip(), v.strip()
        if any(s in k.upper() for s in MASK):
            print(f"    {k} = <已填 长度{len(v)}>" if v else f"    {k} = <空>")
        else:
            print(f"    {k} = {v or '<空>'}")
        if k.upper() == "FRP_SERVER_ADDR":
            server_addr = v
        if k.upper() == "FRP_SERVER_PORT":
            server_port = v

sect("6. 本机监听端口")
rc, out = run([r"C:\Windows\System32\netstat.exe", "-ano"])
hits = [ln.strip() for ln in out.splitlines()
        if "LISTENING" in ln and re.search(r":(5330|7400|9481|4040)\s", ln)]
print("\n".join("  " + h for h in hits) if hits else "  （5330/7400/9481/4040 均无监听）")

sect("7. Windows 服务")
rc, out = run(["powershell", "-NoProfile", "-Command",
               "Get-Service | Where-Object {$_.Name -match 'frp|cpolar|FastGpt|docker|com.docker'} | "
               "Select-Object Name,Status,StartType | Format-Table -AutoSize | Out-String -Width 200"])
print(out or "  <空>")

sect("8. 本机 5330 /health")
try:
    with urllib.request.urlopen("http://127.0.0.1:5330/health", timeout=6) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
        print("  HTTP", r.status, "| status =", d.get("status"))
except Exception as e:  # noqa: BLE001
    print("  失败:", e)

sect("9. cpolar 当前公网地址（本机 API）")
try:
    with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=6) as r:
        html = r.read().decode("utf-8", "replace")
    m = re.search(r"window\.data\s*=\s*JSON\.parse\(\"(.*?)\"\)", html, re.S)
    if m:
        raw = m.group(1).encode().decode("unicode_escape")
        data = json.loads(raw)
        urls = []
        for t in (data.get("UiState", {}).get("Tunnels") or data.get("Txns") or []):
            pu = t.get("PublicUrl") or (t.get("ConnCtx", {}).get("Tunnel", {}) or {}).get("PublicUrl")
            if pu:
                urls.append(pu)
        print("  公网地址:", sorted(set(urls)) or "<未解析到>")
    else:
        print("  未匹配到 window.data")
except Exception as e:  # noqa: BLE001
    print("  失败:", e)

sect("10. frp 服务端可达性（从本机测）")
if server_addr:
    for port, name in [(7000, "frps 接入"), (9481, "映射的 5330"), (80, "http"), (443, "https")]:
        try:
            import socket
            s = socket.create_connection((server_addr, port), timeout=5)
            s.close()
            print(f"  {name:14s} {server_addr}:{port}  →  TCP 可达")
        except Exception as e:  # noqa: BLE001
            print(f"  {name:14s} {server_addr}:{port}  →  不可达 ({type(e).__name__}: {e})")
    for path in ["/health"]:
        for scheme in ["http", "https"]:
            url = f"{scheme}://{server_addr}:9481{path}"
            try:
                req = urllib.request.Request(url, headers={"Host": server_addr})
                with urllib.request.urlopen(req, timeout=8) as r:
                    print(f"  GET {url} → HTTP {r.status} {r.read(120)!r}")
            except Exception as e:  # noqa: BLE001
                print(f"  GET {url} → 失败 ({type(e).__name__}: {e})")
else:
    print("  client/.env 中没有 FRP_SERVER_ADDR，跳过")
