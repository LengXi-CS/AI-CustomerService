"""确认容器该用什么地址访问 Windows 宿主机上的 5330。"""
import os
import re
import socket
import subprocess


def run(args, timeout=30):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        out = (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out.strip()
    except Exception as e:  # noqa: BLE001
        return -1, f"<error {e}>"


def sect(t):
    print("\n" + "=" * 66)
    print(t)
    print("=" * 66)


sect("A. Windows 出口 IP（容器的宿主机在它眼里就是这个）")
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("115.190.250.104", 80))
    print("  出口 IP =", s.getsockname()[0])
    s.close()
except Exception as e:  # noqa: BLE001
    print("  取不到:", e)
try:
    print("  hostname =", socket.gethostname(), "->", socket.gethostbyname_ex(socket.gethostname()))
except Exception as e:  # noqa: BLE001
    print("  gethostbyname_ex 失败:", e)

sect("B. 容器内解析 host.docker.internal")
rc, out = run(["docker", "exec", "frpc", "sh", "-c",
               "echo '--- getent ---'; getent hosts host.docker.internal || echo '(getent 无结果)'; "
               "echo '--- /etc/hosts ---'; cat /etc/hosts; "
               "echo '--- resolv.conf ---'; cat /etc/resolv.conf"])
print("\n".join("  " + ln for ln in out.splitlines()))

sect("C. 容器内路由 / 网卡")
rc, out = run(["docker", "exec", "frpc", "sh", "-c",
               "echo '--- ip route ---'; ip route 2>/dev/null || route -n 2>/dev/null; "
               "echo '--- ip addr ---'; ip -4 addr 2>/dev/null | grep -E 'inet |^[0-9]'"])
print("\n".join("  " + ln for ln in out.splitlines()))

sect("D. 容器内逐个候选地址探测 :5330")
cands = ["127.0.0.1", "host.docker.internal", "gateway.docker.internal"]
rc, out = run(["docker", "exec", "frpc", "sh", "-c", "ip route 2>/dev/null | awk '/default/{print $3}'"])
gw = out.strip().splitlines()[0].strip() if out.strip() else ""
if gw:
    cands.append(gw)
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("115.190.250.104", 80))
    host_ip = s.getsockname()[0]
    s.close()
    cands.append(host_ip)
except Exception:  # noqa: BLE001
    pass

for c in dict.fromkeys(cands):
    rc, out = run(["docker", "exec", "frpc", "sh", "-c",
                   f"wget -q -T 4 -O - http://{c}:5330/health 2>&1 | head -c 200 || "
                   f"echo '(wget 失败)'"], timeout=25)
    ok = "healthy" in out
    print(f"  {c:26s} -> {'可达 ✓ ' + out[:90] if ok else out[:110]}")

sect("E. 本机 .env 里的公网地址相关键")
envp = r"D:\瀚思AI客服\.env"
print("  .env 存在:", os.path.exists(envp))
if os.path.exists(envp):
    for line in open(envp, encoding="utf-8-sig"):
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        k = t.split("=", 1)[0]
        if re.search(r"PUBLIC|CALLBACK|BASE_URL|GEWE", k, re.I):
            v = t.split("=", 1)[1] if "=" in t else ""
            if re.search(r"TOKEN|KEY|APPID|APP_ID", k, re.I):
                v = f"<已填 长度{len(v)}>"
            print(f"    {k} = {v}")

sect("F. 应用是否有 Host / 来源限制（会不会对 IP:端口 的 Host 返 403）")
for pat, label in [
    (r"TrustedHost", "TrustedHost 中间件"),
    (r"403", "显式 403"),
    (r"allowed_hosts|ALLOWED_HOSTS", "allowed_hosts 配置"),
    (r"observe_public_origin", "公网 origin 学习点"),
]:
    rc, out = run(["powershell", "-NoProfile", "-Command",
                   f"Select-String -Path 'D:\\瀚思AI客服\\src\\*.py','D:\\瀚思AI客服\\src\\**\\*.py' "
                   f"-Pattern '{pat}' -SimpleMatch:$false | "
                   f"Select-Object -First 6 Path,LineNumber,Line | Format-Table -AutoSize | Out-String -Width 200"])
    print(f"  -- {label}")
    print("     " + (out.replace("\n", "\n     ") if out.strip() else "(无匹配)"))
